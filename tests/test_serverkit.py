# SPDX-License-Identifier: Apache-2.0
"""The server fixture's own rules, against a stand-in program (no real server needed): readiness
is the log line, a stuck server is killed after the grace period, and every exit path kills the
process and removes its directory (a stray server is a defect)."""

from __future__ import annotations

import os
import stat
import sys
import textwrap
from pathlib import Path
from typing import cast

import pytest

import serverkit

READY = (
    '{"msg":"TCP driver listener listening","address":"127.0.0.1:1",'
    '"fingerprint":"SHA256:' + "ab" * 32 + '"}'
)


def standin(directory: Path, body: str) -> Path:
    path = directory / "fake-server"
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_missing_variable_says_what_to_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(serverkit.BIN_VARIABLE, raising=False)
    with pytest.raises(serverkit.ServerUnavailableError, match=r"scripts/fetch-server\.sh"):
        serverkit.server_binary()


def test_a_path_that_is_not_executable_says_what_to_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(serverkit.BIN_VARIABLE, str(tmp_path / "nothing"))
    with pytest.raises(serverkit.ServerUnavailableError, match=r"not an executable file"):
        serverkit.server_binary()


def test_a_ring_port_comes_with_a_free_successor() -> None:
    import socket

    port = serverkit.free_ring_port()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port + 1))


def test_ready_is_the_log_line_and_it_carries_the_fingerprint(tmp_path: Path) -> None:
    binary = standin(tmp_path, f"import time\nprint({READY!r}, flush=True)\ntime.sleep(60)\n")
    with serverkit.running(binary) as node:
        assert node.fingerprint == "SHA256:" + "ab" * 32
        assert node.startup_seconds < serverkit.READY_TIMEOUT
        assert node.running


def test_the_environment_is_the_handbooks_and_nothing_else(tmp_path: Path) -> None:
    binary = standin(
        tmp_path,
        f"""
        import json, os, time
        print(json.dumps({{"env": sorted(os.environ)}}), flush=True)
        print({READY!r}, flush=True)
        time.sleep(60)
        """,
    )
    with serverkit.running(binary, {"HOARDDB_MAX_BLOB_BYTES": "1024"}) as node:
        (seen,) = [e["env"] for e in node.log_lines() if "env" in e]
    names = [
        str(name) for name in cast("list[object]", seen) if name != "LC_CTYPE"
    ]  # python adds it
    assert names == sorted(
        [
            "HOME",
            "HOARDDB_DATA_DIR",
            "HOARDDB_TCP_LISTEN",
            "HOARDDB_LISTEN",
            "HOARDDB_TCP_INTERNODE_LISTEN",
            "HOARDDB_METRICS_LISTEN",
            "HOARDDB_ROOT_USER",
            "HOARDDB_ROOT_PASSWORD",
            "HOARDDB_MAX_BLOB_BYTES",
        ]
    )


def test_a_server_that_exits_before_ready_is_reported_with_its_log(tmp_path: Path) -> None:
    binary = standin(tmp_path, "import sys\nprint('boom: port in use', flush=True)\nsys.exit(3)\n")
    with (
        pytest.raises(serverkit.ServerStartError, match=r"(?s)status 3.*boom: port in use"),
        serverkit.running(binary),
    ):
        pytest.fail("the body must not run")


def test_a_server_that_never_gets_ready_is_killed_at_the_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(serverkit, "READY_TIMEOUT", 0.3)
    binary = standin(tmp_path, "import time\ntime.sleep(60)\n")
    made: list[serverkit.Node] = []
    real_init = serverkit.Node.__init__

    def spy(self: serverkit.Node, *args: object, **kw: object) -> None:
        real_init(self, *args, **kw)  # type: ignore[arg-type]
        made.append(self)

    monkeypatch.setattr(serverkit.Node, "__init__", spy)
    with (
        pytest.raises(serverkit.ServerStartError, match=r"not ready after"),
        serverkit.running(binary),
    ):
        pytest.fail("the body must not run")
    (node,) = made
    assert node.process is not None and node.process.poll() is not None
    assert not node.root.exists()


def die_inside(binary: Path, error: BaseException, seen: list[tuple[int, Path]]) -> None:
    """Start a server, note its pid and directory, then leave the block by raising ``error``."""
    with serverkit.running(binary) as node:
        assert node.process is not None
        seen.append((node.process.pid, node.root))
        raise error


def test_an_exception_in_the_body_kills_the_server_and_removes_the_directory(
    tmp_path: Path,
) -> None:
    binary = standin(tmp_path, f"import time\nprint({READY!r}, flush=True)\ntime.sleep(60)\n")
    seen: list[tuple[int, Path]] = []
    with pytest.raises(RuntimeError, match="the test failed"):
        die_inside(binary, RuntimeError("the test failed"), seen)
    ((pid, root),) = seen
    assert not alive(pid)
    assert not root.exists()


def test_a_keyboard_interrupt_kills_the_server_and_removes_the_directory(tmp_path: Path) -> None:
    binary = standin(tmp_path, f"import time\nprint({READY!r}, flush=True)\ntime.sleep(60)\n")
    seen: list[tuple[int, Path]] = []
    with pytest.raises(KeyboardInterrupt):
        die_inside(binary, KeyboardInterrupt(), seen)
    ((pid, root),) = seen
    assert not alive(pid)
    assert not root.exists()


def test_a_server_that_ignores_sigterm_is_killed_after_the_grace_period(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(serverkit, "STOP_GRACE", 0.5)
    binary = standin(
        tmp_path,
        f"""
        import signal, time
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        print({READY!r}, flush=True)
        time.sleep(60)
        """,
    )
    with serverkit.running(binary) as node:
        process = node.process
        assert process is not None
        node.stop()
        assert process.returncode == -9  # SIGKILL, because SIGTERM was ignored
    assert not node.root.exists()


def test_a_restart_keeps_the_directory_and_waits_for_a_new_ready_line(tmp_path: Path) -> None:
    binary = standin(tmp_path, f"import time\nprint({READY!r}, flush=True)\ntime.sleep(60)\n")
    with serverkit.running(binary) as node:
        first = node.process
        marker = node.data_dir / "kept"
        marker.write_text("x")
        node.restart()
        assert node.process is not first and node.running
        assert marker.read_text() == "x"
        assert sum(1 for e in node.log_lines() if e.get("msg") == serverkit.READY_MESSAGE) == 2


# --- a port taken between choosing it and binding it -------------------------------------------
# free_port() closes its probe before the server binds, so another process can win the port in
# between. The fixtures must start again on fresh ports, a bounded number of times, and say what
# to do when that is not enough.

BINDS = f"""
import os, socket, sys, time
held = []
for name in ("HOARDDB_TCP_LISTEN", "HOARDDB_LISTEN"):
    host, port = os.environ[name].rsplit(":", 1)
    sock = socket.socket()
    try:
        sock.bind((host, int(port)))
    except OSError:
        print("listen tcp " + os.environ[name] + ": bind: address already in use", flush=True)
        sys.exit(1)
    sock.listen()
    held.append(sock)
print({READY!r}, flush=True)
time.sleep(60)
"""


class Thief:
    """Holds a listening port, as a process that wins the race for it would."""

    def __init__(self) -> None:
        import socket

        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port: int = self.sock.getsockname()[1]

    def __enter__(self) -> Thief:
        return self

    def __exit__(self, *exc: object) -> None:
        self.sock.close()


def steal_first(monkeypatch: pytest.MonkeyPatch, name: str, port: int, times: int = 1) -> list[int]:
    """Make ``serverkit.<name>`` hand out ``port`` (held by someone else) ``times`` times, then
    real ports. Returns the ports it handed out."""
    real = getattr(serverkit, name)
    handed: list[int] = []

    def choose() -> int:
        chosen = port if len(handed) < times else int(real())
        handed.append(chosen)
        return chosen

    monkeypatch.setattr(serverkit, name, choose)
    return handed


def test_a_stolen_port_is_retried_with_a_fresh_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binary = standin(tmp_path, BINDS)
    with Thief() as thief:
        steal_first(monkeypatch, "free_port", thief.port)
        with serverkit.running(binary) as node:
            assert node.running, "the second start, on fresh ports, is the one that is running"
            assert node.driver_port != thief.port
            assert node.env["HOARDDB_TCP_LISTEN"] == f"127.0.0.1:{node.driver_port}"
            refused = [
                line for line in node.log_path.read_text().splitlines() if "already in use" in line
            ]
            assert len(refused) == 1, "one refused start, then one that bound"


def test_a_port_that_stays_stolen_gives_up_after_a_bounded_number_of_tries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binary = standin(tmp_path, BINDS)
    with Thief() as thief:
        steal_first(monkeypatch, "free_port", thief.port, times=10_000)
        node = serverkit.Node(binary)
        try:
            with pytest.raises(serverkit.PortStolenError) as given_up:
                node.start()
            message = str(given_up.value)
            assert f"{serverkit.START_ATTEMPTS} attempts" in message
            assert "ss -ltnp" in message, "the message names the command that finds the thief"
            assert not node.running
        finally:
            node.cleanup()


def test_a_start_that_fails_for_another_reason_is_not_retried(tmp_path: Path) -> None:
    binary = standin(tmp_path, "import sys\nprint('boom: bad config', flush=True)\nsys.exit(3)\n")
    node = serverkit.Node(binary)
    try:
        with pytest.raises(serverkit.ServerStartError) as failed:
            node.start()
        assert not isinstance(failed.value, serverkit.PortStolenError)
        assert node.log_path.read_text().count("boom: bad config") == 1, "started exactly once"
    finally:
        node.cleanup()


def test_a_stolen_ring_port_rebuilds_the_cluster_on_fresh_ports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A ring port is in every member's seed list, so one node cannot simply move it: the whole
    cluster is made again."""
    binary = standin(tmp_path, BINDS)
    with Thief() as thief:
        handed = steal_first(monkeypatch, "free_ring_port", thief.port)
        group, seconds = serverkit.start_cluster(binary, lambda g: 0.5)
        try:
            assert seconds == 0.5, "the result of the confirmation is passed on"
            assert len(handed) > 4, "the cluster was made twice (four nodes each time)"
            assert thief.port not in [n.ring_port for n in group.nodes]
            assert all(n.running for n in group.nodes)
        finally:
            group.cleanup()


def test_a_cluster_that_keeps_losing_its_ring_port_gives_up_and_leaves_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binary = standin(tmp_path, BINDS)
    with Thief() as thief:
        steal_first(monkeypatch, "free_ring_port", thief.port, times=10_000)
        before = len(serverkit._live)  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(serverkit.PortStolenError, match=r"ss -ltnp"):
            serverkit.start_cluster(binary, lambda g: 0.0)
        assert len(serverkit._live) == before, "no node or directory is left behind"  # pyright: ignore[reportPrivateUsage]


def test_a_member_that_died_after_it_was_ready_is_named_at_once(tmp_path: Path) -> None:
    """The real server prints its ready line and may still fail to bind its ring port; the others
    then wait for a membership that cannot come. check_alive is what the confirmation calls."""
    dies_late = f"""
import os, sys, time
print({READY!r}, flush=True)
time.sleep(0.2)
print("listen tcp " + os.environ["HOARDDB_LISTEN"] + ": bind: address already in use", flush=True)
sys.exit(1)
"""
    group = serverkit.make_cluster(standin(tmp_path, dies_late), newcomer=False)
    try:
        group.start()
        for node in group.nodes:
            assert node.process is not None
            node.process.wait(timeout=5)
        with pytest.raises(serverkit.PortStolenError, match=r"node1"):
            group.check_alive()
    finally:
        group.cleanup()
