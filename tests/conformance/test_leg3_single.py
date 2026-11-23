# SPDX-License-Identifier: Apache-2.0
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false, reportUnknownLambdaType=false
"""Leg 3 on one node: every ``exchange`` vector with ``replay: true`` and ``topology: single``,
sent to a REAL server, through both surfaces.

The public API sends the request where a verb exists; the private raw sender sends the rest; a
bare TLS connection sends what no client connection can (a request before ``AUTH``, a payload that
is not BSON). A recording tap sits between the driver and the server, so the test checks what the
driver REALLY sent (the vector's request rules) and what the server REALLY answered (its response
rules, ``vary`` paths by presence and BSON type), and then that the driver's typed result agrees
with that answer.
"""

from __future__ import annotations

from typing import Any

import pytest

import ejson
import replay_api
from hoarddb._bson import serialize
from hoarddb._core import frame, opcodes
from realkit import Servers, authenticate
from replaykit import check_answer, replay_on_rig, request_document
from tapkit import RawConn
from vectors import driver_vectors

pytestmark = pytest.mark.server

ALL_SINGLE = [
    v for v in driver_vectors(kind="exchange") if v["topology"] == "single" and v["replay"]
]
# The vectors also record opcodes this driver does not send. Several need a value an earlier
# answer minted (a lease id), which a replay of one request cannot supply, so the leg replays the
# rest: what the driver has a request for, and what goes on a bare socket.
NOT_SENT = {
    "BATCH", "BATCH_UPSERT", "QUEUE_CLAIM", "QUEUE_ACK", "QUEUE_RELEASE", "QUEUE_EXTEND",
    "DEAD_LIST", "DEAD_REQUEUE", "DEAD_DISCARD", "ALTER_BUCKET", "USER_CREATE", "USER_ALTER",
    "USER_DROP", "USER_LIST", "USER_GET", "USER_GRANT", "USER_REVOKE",
}  # fmt: skip
SINGLE = [v for v in ALL_SINGLE if v["request"]["opcode_name"] not in NOT_SENT]


def group_env(vec: dict[str, Any]) -> dict[str, str]:
    env: dict[str, str] = dict(vec.get("server_env") or {})
    return env


def needs_raw_socket(vec: dict[str, Any]) -> bool:
    """What no pooled, authenticated client connection can send."""
    request = vec["request"]
    return (
        vec["connection"] == "fresh"
        or request["opcode_name"] == "AUTH"
        or "payload_json" not in request
    )


def params() -> list[Any]:
    out: list[Any] = []
    for vec in SINGLE:
        modes = ("socket",) if needs_raw_socket(vec) else ("sync", "async")
        out.extend(pytest.param(vec, m, id=f"{m}:{vec['name']}") for m in modes)
    return out


# -- the raw-socket path --------------------------------------------------------------------------


def replay_on_a_socket(vec: dict[str, Any], servers: Servers) -> None:
    node = servers.node(group_env(vec))
    with RawConn(node.driver_port) as conn:
        assert conn.alpn == "hoarddb/1"
        if vec["connection"] == "authenticated":
            assert authenticate(conn).opcode == opcodes.AUTH_OK
            for step in vec.get("setup", []):
                conn.send(
                    step["opcode"],
                    frame.new_request_id(),
                    serialize(ejson.to_python(step["payload_json"])),
                )
                answer = conn.read_frame()
                assert answer.opcode != opcodes.ERROR, f"setup {step['opcode_name']} was refused"
        request_id = bytes.fromhex(vec["request"]["request_id_hex"])
        conn.send(vec["request"]["opcode"], request_id, request_document(vec))
        answer = conn.read_frame()
        assert answer.request_id == request_id, "the answer carries the request's ID"
        check_answer(vec, answer.opcode, answer.payload)
    servers.rig("sync", node).drop_every_database()


# -- the public path ------------------------------------------------------------------------------


def replay_through_the_driver(vec: dict[str, Any], mode: str, servers: Servers) -> None:
    rig = servers.rig(mode, servers.node(group_env(vec)))
    try:
        replay_on_rig(vec, mode, rig)
    finally:
        rig.drop_every_database()  # a failing vector must not poison the next one


@pytest.mark.parametrize(("vec", "mode"), params())
def test_the_vector_replays_against_the_real_server(
    vec: dict[str, Any], mode: str, servers: Servers
) -> None:
    try:
        if mode == "socket":
            replay_on_a_socket(vec, servers)
        else:
            replay_through_the_driver(vec, mode, servers)
    except AssertionError as failure:
        if str(vec["status"]).startswith("provisional"):
            pytest.skip(f"provisional vector disagrees ({vec['status']}): {failure}")
        raise


def test_the_replay_set_is_the_204_single_node_vectors_the_driver_sends() -> None:
    assert (len(ALL_SINGLE), len(SINGLE)) == (267, 204)
    assert NOT_SENT.isdisjoint(replay_api.ADAPTERS)
    assert sum(1 for v in SINGLE if needs_raw_socket(v)) >= 9
