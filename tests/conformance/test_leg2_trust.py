# SPDX-License-Identifier: Apache-2.0
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false, reportUnknownLambdaType=false
"""Leg 2 against a real server's certificate: the fingerprint the server logs is the one the
driver computes; an unknown key is refused before any ``AUTH`` byte; a changed key is refused
naming both keys; a first sight is recorded and then trusted (``trust.md`` sections 2 to 4)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import pytest

import hoarddb
import realkit
from clientkit import PC
from faketls import Identity
from hoarddb import errors
from hoarddb._trust import fingerprint, known_servers
from realkit import Servers
from serverkit import Node
from tapkit import RawConn

pytestmark = pytest.mark.server


def connect(mode: str, node: Node, identity: Identity, tls: Any) -> PC:
    return PC(mode, realkit.Direct(node), identity, uri=node.uri(), tls=tls)


def authenticated_clients(node: Node) -> int:
    return sum(1 for e in node.log_lines() if e.get("msg") == "client connect")


@pytest.fixture
def node(servers: Servers) -> Node:
    return servers.node()


def test_the_fingerprint_the_server_logs_is_the_one_the_driver_computes(node: Node) -> None:
    with RawConn(node.driver_port) as conn:
        assert conn.alpn == "hoarddb/1"
        from_certificate = fingerprint.fingerprint_of_certificate(conn.peer_der)
    assert from_certificate == node.fingerprint
    assert node.fingerprint.startswith("SHA256:") and len(node.fingerprint) == 7 + 64


def test_a_pinned_key_is_trusted_and_answers(mode: str, node: Node, tap_identity: Identity) -> None:
    pc = connect(mode, node, tap_identity, hoarddb.TlsOptions(fingerprints=[node.fingerprint]))
    try:
        assert pc.call(lambda c: c.ping()) is None
    finally:
        pc.close()


def test_an_unknown_key_is_refused_before_any_auth_byte(
    mode: str, node: Node, tap_identity: Identity, tmp_path: Path
) -> None:
    before = authenticated_clients(node)
    wrong = "SHA256:" + "00" * 32
    with pytest.raises(errors.TlsTrustError) as pinned:
        connect(mode, node, tap_identity, hoarddb.TlsOptions(fingerprints=[wrong]))
    assert pinned.value.code == "TLS_FINGERPRINT_MISMATCH"
    assert node.fingerprint in pinned.value.message and wrong in pinned.value.message
    with pytest.raises(errors.TlsTrustError) as unknown:
        connect(
            mode,
            node,
            tap_identity,
            hoarddb.TlsOptions(known_servers_path=tmp_path / "known_servers"),
        )
    assert unknown.value.code == "TLS_UNTRUSTED"
    assert node.fingerprint in unknown.value.message
    assert "never left this process" in unknown.value.message
    assert authenticated_clients(node) == before, "the server must have seen no AUTH at all"


def test_a_changed_key_is_refused_naming_both_keys(
    mode: str, node: Node, tap_identity: Identity, tmp_path: Path
) -> None:
    path = tmp_path / "known_servers"
    old_key = bytes(range(32))
    path.write_text(f"127.0.0.1:{node.driver_port} {old_key.hex()}\n")
    os.chmod(path, 0o600)
    before = authenticated_clients(node)
    with pytest.raises(errors.TlsTrustError) as changed:
        connect(mode, node, tap_identity, hoarddb.TlsOptions(known_servers_path=path))
    assert changed.value.code == "TLS_KEY_CHANGED"
    assert node.fingerprint in changed.value.message
    assert fingerprint.fingerprint_of_key(old_key) in changed.value.message
    assert path.read_text().count("\n") == 1, "a changed key must never be overwritten"
    assert authenticated_clients(node) == before


def test_a_first_sight_is_recorded_and_then_trusted_without_asking(
    mode: str, node: Node, tap_identity: Identity, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "known_servers"
    asked: list[object] = []

    def accept(key: object) -> bool:
        asked.append(key)
        return hoarddb.accept_first_sight(key)  # type: ignore[arg-type]

    options = hoarddb.TlsOptions(known_servers_path=path, on_unknown_server=accept)
    with caplog.at_level(logging.WARNING, logger="hoarddb"):
        first = connect(mode, node, tap_identity, options)
    try:
        assert first.call(lambda c: c.ping()) is None
    finally:
        first.close()
    assert len(asked) == 1
    assert any("Trusting the first key" in r.message for r in caplog.records)
    recorded = known_servers.KnownServers(path).load()
    (host,) = recorded
    assert host == f"127.0.0.1:{node.driver_port}"
    assert fingerprint.fingerprint_of_key(recorded[host]) == node.fingerprint
    assert (path.stat().st_mode & 0o777) == 0o600
    second = connect(mode, node, tap_identity, hoarddb.TlsOptions(known_servers_path=path))
    try:
        assert second.call(lambda c: c.ping()) is None
    finally:
        second.close()
    assert len(asked) == 1, "a recorded key is trusted without asking again"


def test_insecure_mode_needs_a_reason_and_warns_on_every_connect(
    mode: str, node: Node, tap_identity: Identity, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        caplog.at_level(logging.WARNING, logger="hoarddb"),
        pytest.warns(errors.InsecureTlsWarning, match="leg 2 test"),
    ):
        pc = connect(mode, node, tap_identity, hoarddb.TlsOptions(insecure_reason="leg 2 test"))
    try:
        assert pc.call(lambda c: c.ping()) is None
    finally:
        pc.close()
    warned = [r for r in caplog.records if "leg 2 test" in r.getMessage()]
    assert warned, "insecure mode must log its reason at WARNING"
