# SPDX-License-Identifier: Apache-2.0
"""P6: trust policy and ``_trust.connect_pinned``: nothing is written before the key is trusted.

Every refusal is checked against a fake TLS server that records the application bytes it
receives: zero (spec 5.1 item 6). P7 adds the other half, end to end through the connection.
"""

from __future__ import annotations

import ast
import itertools
import logging
import os
import ssl
import subprocess
import sys
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

import faketls
from hoarddb import accept_first_sight, errors
from hoarddb._core.uri import HostPort
from hoarddb._trust import connect, known_servers, policy
from hoarddb.options import ServerKey, TlsOptions

SRC = Path(__file__).resolve().parent.parent / "src" / "hoarddb"


@pytest.fixture(scope="module")
def ids(tmp_path_factory: pytest.TempPathFactory) -> tuple[faketls.Identity, faketls.Identity]:
    d = tmp_path_factory.mktemp("ids")
    return faketls.make_identity(d, "one"), faketls.make_identity(d, "two")


@pytest.fixture
def server(ids: tuple[faketls.Identity, faketls.Identity]) -> Any:
    with faketls.FakeTlsServer(ids[0]) as s:
        yield s


def recorder(seen: list[ServerKey]) -> Callable[[ServerKey], bool]:
    def callback(key: ServerKey) -> bool:
        seen.append(key)
        return True

    return callback


def accept_all(key: ServerKey) -> bool:
    return True


def decline(key: ServerKey) -> bool:
    return False


def ks_path(tmp_path: Path) -> Path:
    directory = tmp_path / "home" / ".hoarddb"
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    return directory / "known_servers"


def dial(
    server: faketls.FakeTlsServer,
    tmp_path: Path,
    *,
    pin: str | None = None,
    **tls: Any,
) -> ssl.SSLSocket:
    options = TlsOptions(known_servers_path=tls.pop("path", ks_path(tmp_path)), **tls)
    return connect.connect_pinned(HostPort("127.0.0.1", server.port), pin, options, timeout=5.0)


# --- the pure policy ---------------------------------------------------------------------------


def test_the_policy_order_is_pin_then_file_then_unknown() -> None:
    k1, k2 = bytes(32), bytes([1]) * 32
    fp1 = policy.fingerprint_of(k1)
    assert policy.evaluate(k1, fp1, None) is policy.Verdict.PINNED
    assert policy.evaluate(k1, fp1, k2) is policy.Verdict.PINNED  # the pin is authoritative
    assert policy.evaluate(k2, fp1, None) is policy.Verdict.PIN_MISMATCH
    assert policy.evaluate(k2, fp1, k2) is policy.Verdict.PIN_MISMATCH  # the file cannot rescue it
    assert policy.evaluate(k1, None, k1) is policy.Verdict.RECORDED
    assert policy.evaluate(k2, None, k1) is policy.Verdict.KEY_CHANGED
    assert policy.evaluate(k1, None, None) is policy.Verdict.UNKNOWN


def test_accept_first_sight_trusts_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    key = ServerKey("db", 7433, policy.fingerprint_of(bytes(32)), bytes(32))
    with caplog.at_level(logging.WARNING, logger="hoarddb"):
        assert accept_first_sight(key) is True
    assert len(caplog.records) == 1
    text = caplog.records[0].getMessage()
    assert "db:7433" in text
    assert key.fingerprint in text
    assert "first" in text


# --- connect_pinned against a fake TLS server ---------------------------------------------------


def test_a_matching_pin_connects_and_the_client_has_sent_nothing(
    server: faketls.FakeTlsServer, ids: Any, tmp_path: Path
) -> None:
    sock = dial(server, tmp_path, pin=ids[0].fingerprint)
    assert sock.selected_alpn_protocol() == "hoarddb/1"
    assert sock.version() == "TLSv1.3"
    sock.close()
    server.settle()
    assert server.bytes_received == 0
    assert not ks_path(tmp_path).exists()  # a pin never writes the shared file


def test_a_pin_mismatch_sends_zero_bytes_and_names_both_fingerprints(
    server: faketls.FakeTlsServer, ids: Any, tmp_path: Path
) -> None:
    called: list[ServerKey] = []
    with pytest.raises(errors.TlsTrustError) as info:
        dial(
            server,
            tmp_path,
            pin=ids[1].fingerprint,
            on_unknown_server=recorder(called),
        )
    server.settle()
    assert server.bytes_received == 0
    assert info.value.code == "TLS_FINGERPRINT_MISMATCH"
    assert ids[0].fingerprint in info.value.message
    assert ids[1].fingerprint in info.value.message
    assert "not retrying" in info.value.message
    assert called == []  # a mismatch is never a prompt


def test_an_unknown_key_with_nothing_configured_is_refused_with_the_three_ways(
    server: faketls.FakeTlsServer, ids: Any, tmp_path: Path
) -> None:
    with pytest.raises(errors.TlsTrustError) as info:
        dial(server, tmp_path)
    server.settle()
    assert server.bytes_received == 0
    err = info.value
    assert err.code == "TLS_UNTRUSTED"
    host = f"127.0.0.1:{server.port}"
    assert f"The server at {host} presented key {ids[0].fingerprint}" in err.message
    assert "Nothing was sent: your password never left this process." in err.message
    assert f'`hoarddb.connect("hoarddb://{host}?fingerprint={ids[0].fingerprint}")`' in err.message
    assert f"`python -m hoarddb trust {host}`" in err.message
    assert "tls=`hoarddb.TlsOptions(on_unknown_server=hoarddb.accept_first_sight)`" in err.message


def test_a_callback_that_accepts_connects_and_records_the_key(
    server: faketls.FakeTlsServer, ids: Any, tmp_path: Path
) -> None:
    seen: list[ServerKey] = []

    def accept(key: ServerKey) -> bool:
        seen.append(key)
        return True

    sock = dial(server, tmp_path, on_unknown_server=accept)
    sock.close()
    assert [(k.host, k.port, k.fingerprint, k.key) for k in seen] == [
        ("127.0.0.1", server.port, ids[0].fingerprint, ids[0].public_key)
    ]
    assert ks_path(tmp_path).read_text() == f"127.0.0.1:{server.port} {ids[0].public_key.hex()}\n"
    # the next connection needs no callback: the file already trusts the key
    again = dial(server, tmp_path)
    again.close()
    server.settle()
    assert server.bytes_received == 0


def test_a_callback_that_declines_refuses_and_records_nothing(
    server: faketls.FakeTlsServer, tmp_path: Path
) -> None:
    with pytest.raises(errors.TlsTrustError) as info:
        dial(server, tmp_path, on_unknown_server=decline)
    server.settle()
    assert server.bytes_received == 0
    assert info.value.code == "TLS_UNTRUSTED"
    assert not ks_path(tmp_path).exists()


def test_a_changed_key_for_a_recorded_host_is_a_hard_failure_and_never_a_prompt(
    server: faketls.FakeTlsServer, ids: Any, tmp_path: Path
) -> None:
    path = ks_path(tmp_path)
    path.write_text(f"127.0.0.1:{server.port} {ids[1].public_key.hex()}\n")
    path.chmod(0o600)
    called: list[ServerKey] = []
    with pytest.raises(errors.TlsTrustError) as info:
        dial(server, tmp_path, on_unknown_server=recorder(called))
    server.settle()
    assert server.bytes_received == 0
    assert called == []
    err = info.value
    assert err.code == "TLS_KEY_CHANGED"
    assert ids[0].fingerprint in err.message
    assert ids[1].fingerprint in err.message
    assert str(path) in err.message
    assert "delete the line" in err.message
    assert "known_hosts" not in err.message
    assert path.read_text() == f"127.0.0.1:{server.port} {ids[1].public_key.hex()}\n"


def test_a_pin_is_authoritative_over_a_stale_file_entry(
    server: faketls.FakeTlsServer, ids: Any, tmp_path: Path
) -> None:
    path = ks_path(tmp_path)
    path.write_text(f"127.0.0.1:{server.port} {ids[1].public_key.hex()}\n")
    path.chmod(0o600)
    dial(server, tmp_path, pin=ids[0].fingerprint).close()


def test_use_known_servers_false_neither_reads_nor_writes_the_file(
    server: faketls.FakeTlsServer, ids: Any, tmp_path: Path
) -> None:
    path = ks_path(tmp_path)
    path.write_text(f"127.0.0.1:{server.port} {ids[0].public_key.hex()}\n")
    path.chmod(0o600)
    with pytest.raises(errors.TlsTrustError):
        dial(server, tmp_path, use_known_servers=False)  # the matching entry is not consulted
    dial(server, tmp_path, use_known_servers=False, on_unknown_server=accept_all).close()
    assert path.read_text().count("\n") == 1  # and nothing was appended


def test_an_async_callback_on_the_sync_client_is_a_config_error_with_the_fix(
    server: faketls.FakeTlsServer, tmp_path: Path
) -> None:
    async def accept(key: ServerKey) -> bool:
        return True

    with pytest.raises(errors.ConfigError) as info:
        dial(server, tmp_path, on_unknown_server=accept)
    assert "hoarddb.aio" in info.value.message
    server.settle()
    assert server.bytes_received == 0


# --- the traps of spec 15 ----------------------------------------------------------------------


def test_the_empty_dict_peer_certificate_is_never_consulted(
    server: faketls.FakeTlsServer, ids: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[bool] = []
    real = ssl.SSLSocket.getpeercert

    def spy(self: ssl.SSLSocket, binary_form: bool = False) -> Any:
        calls.append(binary_form)
        return real(self, binary_form)

    monkeypatch.setattr(ssl.SSLSocket, "getpeercert", spy)
    dial(server, tmp_path, pin=ids[0].fingerprint).close()
    assert calls
    assert all(calls), "the dict form returns {} under CERT_NONE and must never be used"
    # and the trap is real: with verification off, the dict form is empty
    monkeypatch.undo()
    raw = ssl.create_default_context()
    raw.check_hostname = False
    raw.verify_mode = ssl.CERT_NONE
    import socket

    with socket.create_connection(("127.0.0.1", server.port)) as s, raw.wrap_socket(s) as tls:
        assert tls.getpeercert() == {}
        assert tls.getpeercert(binary_form=True) == ids[0].cert_der


def test_an_empty_der_is_a_refusal_not_a_pass(
    server: faketls.FakeTlsServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def empty(self: ssl.SSLSocket, binary_form: bool = False) -> Any:
        return b"" if binary_form else {}

    monkeypatch.setattr(ssl.SSLSocket, "getpeercert", empty)
    with pytest.raises(errors.TlsTrustError, match="no certificate"):
        dial(server, tmp_path, on_unknown_server=accept_all)
    server.settle()
    assert server.bytes_received == 0


def test_no_alpn_is_a_failure_not_close_enough(ids: Any, tmp_path: Path) -> None:
    for alpn in (None, ("h2",)):
        with faketls.FakeTlsServer(ids[0], alpn=alpn) as server:
            with pytest.raises(errors.UnsupportedVersionError) as info:
                dial(server, tmp_path, pin=ids[0].fingerprint)
            server.settle()
            assert server.bytes_received == 0
            assert "hoarddb/1" in info.value.message


def test_tls_below_1_3_is_refused(ids: Any, tmp_path: Path) -> None:
    with faketls.FakeTlsServer(ids[0], max_version=ssl.TLSVersion.TLSv1_2) as server:
        with pytest.raises(errors.ConnectionFailedError, match=r"TLS 1\.3"):
            dial(server, tmp_path, pin=ids[0].fingerprint)
        assert server.bytes_received == 0


def test_a_refused_connection_is_an_oserror_for_the_connection_layer_to_wrap(
    tmp_path: Path,
) -> None:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(OSError):  # noqa: PT011 - any connect failure; the layer above names it
        connect.connect_pinned(
            HostPort("127.0.0.1", port),
            None,
            TlsOptions(known_servers_path=ks_path(tmp_path)),
            timeout=2.0,
        )


def test_the_certificate_of_another_key_type_is_refused_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ids: Any
) -> None:
    rsa_cert = tmp_path / "rsa.crt"
    rsa_key = tmp_path / "rsa.key"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(rsa_key),
         "-out", str(rsa_cert), "-subj", "/CN=x", "-days", "2"],
        check=True, capture_output=True,
    )  # fmt: skip
    identity = faketls.Identity(rsa_cert, rsa_key, bytes(32), b"")
    with faketls.FakeTlsServer(identity) as server:
        with pytest.raises(errors.TlsTrustError) as info:
            dial(server, tmp_path, on_unknown_server=accept_all)
        server.settle()
        assert server.bytes_received == 0
        assert info.value.code == "TLS_KEY_TYPE"
        assert "RSA" in info.value.message


# --- insecure mode -----------------------------------------------------------------------------


def test_insecure_mode_skips_the_check_warns_on_every_dial_and_still_sends_nothing(
    server: faketls.FakeTlsServer, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        warnings.catch_warnings(record=True) as caught,
        caplog.at_level(logging.WARNING, logger="hoarddb"),
    ):
        warnings.simplefilter("always")
        dial(server, tmp_path, insecure_reason="local test rig").close()
        dial(server, tmp_path, insecure_reason="local test rig").close()
    assert any(issubclass(w.category, errors.InsecureTlsWarning) for w in caught)
    texts = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(texts) == 2  # the log line is the every-time channel
    assert all("local test rig" in t and "verification is off" in t for t in texts)
    server.settle()
    assert server.bytes_received == 0


def test_insecure_with_a_pin_is_refused_here_too_as_defence_in_depth(
    server: faketls.FakeTlsServer, ids: Any, tmp_path: Path
) -> None:
    with pytest.raises(errors.ConfigError, match="pin"):
        dial(server, tmp_path, pin=ids[0].fingerprint, insecure_reason="rig")
    assert server.bytes_received == 0


def test_a_warning_reaches_stderr_with_no_logging_configured() -> None:
    code = (
        "import logging\n"
        "from hoarddb._core.uri import HostPort\n"
        "from hoarddb._trust.connect import note_insecure\n"
        "note_insecure(logging.getLogger('hoarddb'), HostPort('h', 1), 'local test rig')\n"
    )
    env = {**os.environ, "PYTHONPATH": str(SRC.parent)}
    done = subprocess.run(
        [sys.executable, "-W", "ignore", "-c", code],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "local test rig" in done.stderr  # through logging.lastResort: no NullHandler hides it


def test_the_package_installs_no_null_handler() -> None:
    assert logging.getLogger("hoarddb").handlers == []
    assert not [p for p in SRC.rglob("*.py") if "NullHandler" in p.read_text(encoding="utf-8")]


# --- the check is in every combination of options ----------------------------------------------


def test_every_combination_of_trust_options_installs_the_key_check(
    server: faketls.FakeTlsServer, ids: Any, tmp_path: Path
) -> None:
    """Against a key that no option trusts, every non-insecure combination refuses."""
    wrong_pin = ids[1].fingerprint
    for pin, use_file, callback in itertools.product(
        [None, wrong_pin], [True, False], [None, decline]
    ):
        with pytest.raises(errors.TlsTrustError):
            dial(server, tmp_path, pin=pin, use_known_servers=use_file, on_unknown_server=callback)
    server.settle()
    assert server.bytes_received == 0


def test_there_is_exactly_one_ssl_context_and_cert_none_lives_beside_the_check() -> None:
    contexts: list[str] = []
    cert_none: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "SSLContext":
                contexts.append(rel)
            if isinstance(node, ast.Attribute) and node.attr == "CERT_NONE":
                cert_none.append(rel)
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") in (
                "create_default_context",
                "load_default_certs",
            ):
                raise AssertionError(f"{rel} must never load the system CA store")
    assert contexts == ["_trust/connect.py"]
    assert cert_none == ["_trust/connect.py"]


def test_the_known_servers_store_is_the_one_the_policy_uses(tmp_path: Path) -> None:
    store = known_servers.KnownServers.for_options(ks_path(tmp_path))
    assert store.path == ks_path(tmp_path)


# --- security review L9: only True trusts a key ---------------------------------------------------


@pytest.mark.parametrize("answer", ["no", "false", 1, 2, [0], object(), (), 0, None, "yes"])
def test_a_callback_answer_other_than_true_does_not_trust_the_key(
    server: faketls.FakeTlsServer, tmp_path: Path, answer: Any
) -> None:
    def callback(key: ServerKey) -> bool:
        return cast(bool, answer)

    with pytest.raises(errors.TlsTrustError):
        dial(server, tmp_path, on_unknown_server=callback)
    server.settle()
    assert server.bytes_received == 0


def test_a_callback_that_is_not_callable_fails_at_resolve_time() -> None:
    from hoarddb._core import config

    bad_callbacks: list[Any] = ["accept", True, 1, ["x"]]
    for bad in bad_callbacks:
        with pytest.raises(errors.ConfigError, match=r"on_unknown_server.*callable"):
            config.resolve_options(
                "hoarddb://u:p@h", {"tls": TlsOptions(on_unknown_server=bad)}, {}
            )


# --- security review L10: first-sight trust needs a file to remember it in -----------------------


def test_accept_first_sight_without_the_known_servers_file_is_refused_at_resolve_time() -> None:
    from hoarddb._core import config

    tls = TlsOptions(use_known_servers=False, on_unknown_server=accept_first_sight)
    with pytest.raises(errors.ConfigError, match="accept_first_sight") as caught:
        config.resolve_options("hoarddb://u:p@h", {"tls": tls}, {})
    assert "use_known_servers" in str(caught.value)
    assert "insecure_reason" in str(caught.value)  # says how to get the behaviour on purpose


def test_accept_first_sight_without_the_file_is_refused_at_dial_time_too(
    server: faketls.FakeTlsServer, tmp_path: Path
) -> None:
    with pytest.raises(errors.ConfigError, match="accept_first_sight"):
        dial(server, tmp_path, use_known_servers=False, on_unknown_server=accept_first_sight)
    server.settle()
    assert server.bytes_received == 0


def test_a_homemade_callback_without_the_file_is_still_the_programs_decision(
    server: faketls.FakeTlsServer, tmp_path: Path
) -> None:
    dial(server, tmp_path, use_known_servers=False, on_unknown_server=accept_all).close()
