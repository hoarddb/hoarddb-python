# SPDX-License-Identifier: Apache-2.0
"""P6: the shared ``known_servers`` trust store (trust.md section 4, spec 5.1 item 9)."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import vectors
from hoarddb import errors
from hoarddb._core.uri import HostPort
from hoarddb._trust import known_servers

CASES = [v for v in vectors.driver_vectors(kind="trust") if "known_servers" in v]
KEY1 = bytes.fromhex("884b8857f4eaa1613c61504db34d4beaf346517a0e31de3cddd4d9b4201d9d0b")
KEY2 = bytes.fromhex("a09aa5f47a6759802ff955f8dc2d2a14a5c99d23be97f864127ff9383455a4f0")


def store_in(tmp_path: Path) -> tuple[known_servers.KnownServers, Path]:
    directory = tmp_path / "dot-hoarddb"
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    path = directory / "known_servers"
    return known_servers.KnownServers(path), path


@pytest.mark.parametrize("vec", CASES, ids=lambda v: v["name"])
def test_the_vectors_cases(vec: vectors.Vector) -> None:
    case = vec["known_servers"]
    if "entries" in case:
        parsed = known_servers.parse(case["file_text"])
        assert {k: v.hex() for k, v in parsed.items()} == case["entries"]
    else:
        with pytest.raises(errors.TlsTrustError) as info:
            known_servers.parse(case["file_text"])
        assert case["error_contains"] in info.value.message
        assert info.value.code == "TLS_KNOWN_SERVERS"


def test_the_file_name_in_a_message_is_known_servers_never_known_hosts(tmp_path: Path) -> None:
    store, path = store_in(tmp_path)
    path.write_text(f"h:1 {KEY1.hex()}\nh:1 {KEY2.hex()}\n")
    with pytest.raises(errors.TlsTrustError) as info:
        store.load()
    assert "known_servers" in info.value.message
    assert "known_hosts" not in str(info.value)


def test_a_missing_file_is_an_empty_store(tmp_path: Path) -> None:
    store, _ = store_in(tmp_path)
    assert store.load() == {}
    assert store.lookup(HostPort("h", 1)) is None


def test_lookup_finds_by_host_and_port_with_ipv6_in_brackets(tmp_path: Path) -> None:
    store, path = store_in(tmp_path)
    path.write_text(f"[::1]:7433 {KEY1.hex()}\ndb.internal:7433 {KEY2.hex()}\n")
    path.chmod(0o600)
    assert store.lookup(HostPort("::1", 7433)) == KEY1
    assert store.lookup(HostPort("db.internal", 7433)) == KEY2
    assert store.lookup(HostPort("db.internal", 7434)) is None


# --- permissions -------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [0o666, 0o660, 0o606, 0o622])
def test_a_file_other_users_can_write_is_refused_with_the_chmod_to_run(
    tmp_path: Path, mode: int
) -> None:
    store, path = store_in(tmp_path)
    path.write_text(f"h:1 {KEY1.hex()}\n")
    path.chmod(mode)
    with pytest.raises(errors.TlsTrustError) as info:
        store.load()
    assert f"chmod 600 {path}" in str(info.value)


@pytest.mark.parametrize("mode", [0o777, 0o770, 0o707])
def test_a_directory_other_users_can_write_is_refused_with_the_chmod_to_run(
    tmp_path: Path, mode: int
) -> None:
    store, path = store_in(tmp_path)
    path.write_text(f"h:1 {KEY1.hex()}\n")
    path.chmod(0o600)
    path.parent.chmod(mode)
    try:
        with pytest.raises(errors.TlsTrustError) as info:
            store.load()
        assert f"chmod 700 {path.parent}" in str(info.value)
    finally:
        path.parent.chmod(0o700)


@pytest.mark.parametrize("mode", [0o600, 0o644, 0o640, 0o400])
def test_a_file_only_the_owner_can_write_is_fine(tmp_path: Path, mode: int) -> None:
    store, path = store_in(tmp_path)
    path.write_text(f"h:1 {KEY1.hex()}\n")
    path.chmod(mode)
    assert store.load() == {"h:1": KEY1}


# --- writing -----------------------------------------------------------------------------------


def test_record_creates_the_directory_0700_and_the_file_0600_and_appends_one_line(
    tmp_path: Path,
) -> None:
    path = tmp_path / "fresh" / "known_servers"
    store = known_servers.KnownServers(path)
    store.record(HostPort("localhost", 7433), KEY1)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text() == f"localhost:7433 {KEY1.hex()}\n"
    store.record(HostPort("::1", 7433), KEY2)
    assert path.read_text().splitlines() == [
        f"localhost:7433 {KEY1.hex()}",
        f"[::1]:7433 {KEY2.hex()}",
    ]


def test_recording_the_same_key_twice_writes_one_line_and_keeps_comments(tmp_path: Path) -> None:
    store, path = store_in(tmp_path)
    path.write_text(f"# mine\nh:1 {KEY1.hex()}\n")
    path.chmod(0o600)
    store.record(HostPort("h", 1), KEY1)
    assert path.read_text() == f"# mine\nh:1 {KEY1.hex()}\n"


def test_recording_a_different_key_for_a_known_host_is_the_changed_key_error(
    tmp_path: Path,
) -> None:
    store, path = store_in(tmp_path)
    path.write_text(f"h:1 {KEY1.hex()}\n")
    path.chmod(0o600)
    with pytest.raises(errors.TlsTrustError) as info:
        store.record(HostPort("h", 1), KEY2)
    assert info.value.code == "TLS_KEY_CHANGED"
    assert path.read_text() == f"h:1 {KEY1.hex()}\n"


def test_a_line_is_appended_with_a_single_write_on_an_append_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = store_in(tmp_path)
    writes: list[bytes] = []
    flags: list[int] = []
    real_write, real_open = os.write, os.open

    def spy_write(fd: int, data: bytes) -> int:
        writes.append(bytes(data))
        return real_write(fd, data)

    def spy_open(p: str | os.PathLike[str], flag: int, *args: int) -> int:
        flags.append(flag)
        return real_open(p, flag, *args)

    monkeypatch.setattr(os, "write", spy_write)
    monkeypatch.setattr(os, "open", spy_open)
    store.record(HostPort("h", 1), KEY1)
    assert writes == [f"h:1 {KEY1.hex()}\n".encode()]
    assert flags
    assert all(f & os.O_APPEND for f in flags)


def test_concurrent_threads_never_tear_or_lose_a_line(tmp_path: Path) -> None:
    store, path = store_in(tmp_path)
    errors_seen: list[BaseException] = []

    def work(n: int) -> None:
        try:
            for i in range(25):
                key = bytes([n]) * 31 + bytes([i])
                known_servers.KnownServers(path).record(HostPort(f"t{n}-{i}", 1), key)
        except BaseException as exc:
            errors_seen.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors_seen
    assert len(store.load()) == 8 * 25


def test_concurrent_processes_are_serialised_by_flock(tmp_path: Path) -> None:
    store, path = store_in(tmp_path)
    script = (
        "import sys\n"
        "from hoarddb._trust.known_servers import KnownServers\n"
        "from hoarddb._core.uri import HostPort\n"
        "n = int(sys.argv[2])\n"
        "for i in range(20):\n"
        "    key = bytes([n]) * 31 + bytes([i])\n"
        "    KnownServers(sys.argv[1]).record(HostPort(f'p{n}-{i}', 1), key)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src")}
    procs = [
        subprocess.Popen([sys.executable, "-c", script, str(path), str(n)], env=env)
        for n in range(4)
    ]
    assert [p.wait(timeout=60) for p in procs] == [0, 0, 0, 0]
    entries = store.load()
    assert len(entries) == 4 * 20
    assert all(len(line.split()) == 2 for line in path.read_text().splitlines())


def test_the_default_path_is_under_the_home_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert known_servers.default_path() == tmp_path / ".hoarddb" / "known_servers"
    assert (
        known_servers.KnownServers.for_options(None).path == tmp_path / ".hoarddb" / "known_servers"
    )
    explicit = tmp_path / "x" / "ks"
    assert known_servers.KnownServers.for_options(str(explicit)).path == explicit
    assert known_servers.KnownServers.for_options(explicit).path == explicit
