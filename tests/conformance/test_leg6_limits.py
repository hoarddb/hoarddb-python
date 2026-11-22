# SPDX-License-Identifier: Apache-2.0
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false, reportUnknownLambdaType=false
"""Leg 6: the server's size limits and the driver's, against the real thing (``wire.md`` 10).

Each test that needs an operator limit starts the server that has it (``servers.node(env)``), and
the large payloads go straight to the node: a recording tap would only copy them.
"""

from __future__ import annotations

import hashlib
import struct
from typing import Any

import pytest

import faketls
import hoarddb
from clientkit import PC
from hoarddb import errors
from hoarddb._bson import serialize
from hoarddb._core import frame, opcodes
from hoarddb._core.failure import Context, error_from_payload
from realkit import RESULT_LINES, Servers, authenticate, connect_direct, decode, wait_for
from serverkit import Node
from tapkit import RawConn

pytestmark = pytest.mark.server

MIB = 1024 * 1024
DOCUMENT_LIMIT = 24 * MIB
BLOB_LIMIT = 16 * MIB
FRAME_LIMIT = 32 * MIB


def document_of(size: int) -> dict[str, str]:
    """A document whose own encoded bytes are exactly ``size``."""
    doc = {"v": "x" * (size - 13)}  # 4 length + 1 type + 2 key + 4 string length + 1 NUL + 1 end
    assert len(serialize(doc)) == size
    return doc


@pytest.fixture
def direct(mode: str, servers: Servers, tap_identity: faketls.Identity) -> Any:
    clients: list[PC] = []

    def make(node: Node, **options: Any) -> PC:
        pc = connect_direct(mode, node, tap_identity, **options)
        clients.append(pc)
        return pc

    yield make
    for pc in clients:
        pc.close()


def test_a_document_just_under_24_mib_is_stored_and_just_over_is_refused(
    mode: str, servers: Servers, direct: Any
) -> None:
    node = servers.node()
    pc = direct(node)
    pc.call(lambda c: c.create_database("big"))
    pc.call(lambda c: c.database("big").create_bucket("docs", "hash"))
    under = document_of(DOCUMENT_LIMIT - 1024)
    pc.call(lambda c: c.database("big").bucket("docs").put("under", under))
    back = pc.call(lambda c: c.database("big").bucket("docs").get("under"))
    assert back is not None and back["v"] == under["v"]
    with pytest.raises(errors.InvalidArgumentError) as refused:
        pc.call(
            lambda c: (
                c.database("big").bucket("docs").put("over", document_of(DOCUMENT_LIMIT + 1024))
            )
        )
    assert refused.value.message.startswith('Document "over" is')
    assert refused.value.wire_code == 4
    assert pc.call(lambda c: c.database("big").bucket("docs").exists("over")) is False
    pc.call(lambda c: c.drop_database("big"))


def test_a_blob_at_16_mib_is_stored_and_just_over_is_refused(
    mode: str, servers: Servers, direct: Any
) -> None:
    node = servers.node()
    pc = direct(node)
    pc.call(lambda c: c.create_database("filedb"))
    pc.call(lambda c: c.database("filedb").create_bucket("files", "blob"))
    payload = bytes(range(256)) * (BLOB_LIMIT // 256)
    assert len(payload) == BLOB_LIMIT
    written = pc.call(lambda c: c.database("filedb").bucket("files").put_blob("exact", payload))
    assert written.size == BLOB_LIMIT
    back = pc.call(lambda c: c.database("filedb").bucket("files").get_blob("exact"))
    assert (
        back is not None and hashlib.sha256(back.data).digest() == hashlib.sha256(payload).digest()
    )
    with pytest.raises(errors.InvalidArgumentError) as refused:
        pc.call(lambda c: c.database("filedb").bucket("files").put_blob("over", payload + b"x"))
    assert refused.value.message.startswith('Blob "over" is')
    assert "this server's limit is" in refused.value.message
    pc.call(lambda c: c.drop_database("filedb"))


def test_the_driver_refuses_a_frame_over_32_mib_before_writing_a_byte(
    mode: str, servers: Servers
) -> None:
    rig = servers.rig(mode, servers.node())
    try:
        rig.call(lambda c: c.create_database("frames"))
        rig.call(lambda c: c.database("frames").create_bucket("files", "blob"))
        rig.call(lambda c: c.ping())  # warm: the lanes this test uses are dialled before the check
        rig.tap.clear()
        before = rig.tap.bytes_from_client
        with pytest.raises(errors.FrameTooLargeError) as refused:
            rig.call(
                lambda c: c.database("frames").bucket("files").put_blob("huge", b"x" * FRAME_LIMIT)
            )
        assert "33554432" in refused.value.message and "Nothing was sent" in refused.value.message
        handshakes = sum(
            len(f.payload) + frame.HEADER_SIZE
            for f in rig.tap.frames("c2s")
            if f.opcode == opcodes.AUTH
        )
        assert rig.tap.bytes_from_client - before == handshakes, (
            "the only bytes written may be a new connection's AUTH, never any of the frame"
        )
        assert rig.tap.requests() == []
        assert rig.call(lambda c: c.ping()) is None, "the client is still usable"
    finally:
        rig.drop_every_database()


def oversize_header(opcode: int, request_id: bytes, length: int) -> bytes:
    return struct.pack("<BIB16s", 1, length, opcode, request_id)


def test_the_server_answers_an_oversize_header_by_its_request_id(servers: Servers) -> None:
    node = servers.node()
    request_id = frame.new_request_id()
    claimed = FRAME_LIMIT + 1
    with RawConn(node.driver_port) as conn:
        assert authenticate(conn).opcode == opcodes.AUTH_OK
        body = bytes(claimed - 17)
        conn.send_bytes(oversize_header(opcodes.PUT, request_id, claimed) + body)
        answer = conn.read_frame()
        assert answer.opcode == opcodes.ERROR and answer.request_id == request_id
        error = error_from_payload(decode(answer.payload), answer.request_id, Context("probe"))
        assert isinstance(error, errors.InvalidArgumentError) and error.wire_code == 4
        assert "32 MiB" in error.message or "33554432" in error.message
        # an authenticated connection whose length word is at most 64 MiB keeps the connection
        ping_id = frame.new_request_id()
        conn.send(opcodes.PING, ping_id, b"")
        pong = conn.read_frame()
        assert pong.opcode == opcodes.PONG and pong.request_id == ping_id


def test_the_server_answers_an_oversize_header_before_auth_and_then_closes(
    servers: Servers,
) -> None:
    node = servers.node()
    request_id = frame.new_request_id()
    with RawConn(node.driver_port) as conn:
        conn.send_bytes(oversize_header(opcodes.PUT, request_id, FRAME_LIMIT + 1))
        answer = conn.read_frame()
        assert answer.opcode == opcodes.ERROR and answer.request_id == request_id
        assert conn.read_until_closed() == b"", "nothing after the answer: the server closes"


@pytest.mark.parametrize(
    ("what", "header"),
    [
        ("a bad version byte", struct.pack("<BIB16s", 2, 17, opcodes.PING, bytes(16))),
        ("a length under the minimum", struct.pack("<BIB16s", 1, 16, opcodes.PING, bytes(16))),
    ],
)
def test_a_malformed_header_is_closed_without_an_answer(
    what: str, header: bytes, servers: Servers
) -> None:
    node = servers.node()
    with RawConn(node.driver_port) as conn:
        assert authenticate(conn).opcode == opcodes.AUTH_OK
        conn.send_bytes(header)
        assert conn.read_until_closed() == b"", f"{what}: the server must close without a word"


def fill_two_big_documents(pc: PC, database: str, bucket: str, size: int) -> None:
    pc.call(lambda c: c.create_database(database))
    pc.call(lambda c: c.database(database).create_bucket(bucket, "hash"))
    for key in ("one", "two"):

        def put(client: Any, key: str = key) -> Any:
            return client.database(database).bucket(bucket).put(key, {"pad": "x" * size})

        pc.call(put)


CEILING = {"HOARDDB_QUERY_MAX_RESULT_BYTES": str(MIB)}


def test_a_query_over_the_result_ceiling_is_halved_and_the_connection_survives(
    mode: str, servers: Servers
) -> None:
    node = servers.node(CEILING)
    rig = servers.rig(mode, node)
    try:
        fill_two_big_documents(rig.pc, "ceiling", "big", 700_000)
        rig.call(lambda c: c.database("ceiling").bucket("big").find(limit=1).to_list())  # warm
        dials = rig.call(lambda c: c.pool_stats().dials)
        walked = rig.call(lambda c: c.database("ceiling").bucket("big").find().to_list())
        assert sorted(len(d["pad"]) for d in walked) == [700_000, 700_000]
        stats = rig.call(lambda c: c.pool_stats())
        assert stats.dials == dials and stats.discarded == 0, "RESULT_TOO_LARGE is not fatal"
        assert rig.call(lambda c: c.ping()) is None
    finally:
        rig.drop_every_database()


def test_the_same_query_unhalved_is_result_too_large_and_the_same_connection_answers_ping(
    mode: str, servers: Servers
) -> None:
    node = servers.node(CEILING)
    rig = servers.rig(mode, node)
    try:
        fill_two_big_documents(rig.pc, "ceiling2", "big", 700_000)
        rig.tap.clear()
        scan = {"database": "ceiling2", "bucket": "big", "limit": 0}
        with pytest.raises(errors.ResultTooLargeError) as refused:
            rig.raw(opcodes.SCAN, "SCAN", scan)
        assert refused.value.max_result_bytes == MIB and refused.value.retained_bytes > 0
        assert refused.value.wire_code == 14
        connections_before = rig.tap.connections
        assert rig.call(lambda c: c.ping()) is None
        assert rig.tap.connections == connections_before, "the same connection answered PING"
        # one document over the ceiling on its own: both numbers, and no endless halving
        rig.call(
            lambda c: c.database("ceiling2").bucket("big").put("huge", {"pad": "x" * 1_500_000})
        )
        with pytest.raises(errors.ResultTooLargeError) as alone:
            rig.call(lambda c: c.database("ceiling2").bucket("big").find().to_list())
        assert alone.value.max_result_bytes == MIB
        assert rig.call(lambda c: c.ping()) is None
    finally:
        rig.drop_every_database()


def test_a_batch_the_server_can_hold_is_stored_in_one_request_and_a_bigger_one_is_refused_locally(
    mode: str, servers: Servers
) -> None:
    """The handbook says a batch over ``memtable.batch_bytes`` is refused in-band and must be
    halved; the pinned server splits it itself, so halving is not reachable here (Q17). What the
    driver owes: send it whole, and refuse a batch that cannot fit a frame without writing it."""
    rig = servers.rig(mode, servers.node())
    try:
        bound = rig.call(lambda c: c.health()).memtable["batch_bytes"]
        docs = [{"n": i, "pad": "x" * MIB} for i in range(int(bound // MIB) + 12)]
        assert sum(MIB for _ in docs) > bound
        rig.call(lambda c: c.create_database("batch"))
        rig.call(lambda c: c.database("batch").create_bucket("rows", "hash"))
        rig.tap.clear()
        result = rig.call(lambda c: c.database("batch").bucket("rows").insert_many(docs))
        sent = [f for f in rig.tap.requests() if f.opcode == opcodes.BATCH_PUT]
        assert result.count == len(docs) and len(sent) == 1
        assert rig.call(lambda c: c.database("batch").bucket("rows").count()) == len(docs)
        RESULT_LINES.append(
            f"leg 6 batch {rig.mode:5} {len(docs)} MiB over a {bound} byte bound: "
            f"{len(sent)} BATCH_PUT request, all {result.count} stored"
        )
        rig.tap.clear()
        too_big = [{"n": i, "pad": "x" * MIB} for i in range(40)]
        with pytest.raises(errors.FrameTooLargeError):
            rig.call(lambda c: c.database("batch").bucket("rows").insert_many(too_big))
        assert [f for f in rig.tap.requests() if f.opcode == opcodes.BATCH_PUT] == []
        assert rig.call(lambda c: c.database("batch").bucket("rows").count()) == len(docs)
    finally:
        rig.drop_every_database()


# -- a document at the operator's maximum ---------------------------------------------------------


def test_a_document_at_the_operators_maximum_is_stored_even_with_the_longest_key(
    mode: str, servers: Servers, direct: Any
) -> None:
    """The handbook says a ~31 MiB document with a long key can miss the replication fit; the
    operator maximum is 30 MiB, so on one node the refusal cannot be provoked (Q18)."""
    node = servers.node({"HOARDDB_MAX_DOCUMENT_BYTES": str(30 * MIB)})
    pc = direct(node)
    pc.call(lambda c: c.create_database("fit"))
    pc.call(lambda c: c.database("fit").create_bucket("docs", "hash"))
    key = "k" * 250
    biggest = document_of(30 * MIB - 1024)
    pc.call(lambda c: c.database("fit").bucket("docs").put(key, biggest))
    assert pc.call(lambda c: c.database("fit").bucket("docs").exists(key)) is True
    with pytest.raises(errors.InvalidArgumentError) as refused:
        pc.call(lambda c: c.database("fit").bucket("docs").put(key, document_of(30 * MIB + 1024)))
    assert "this server's limit is" in refused.value.message
    pc.call(lambda c: c.drop_database("fit"))


# -- the per-address connection cap ---------------------------------------------------------------

CAP = {"HOARDDB_TCP_MAX_CONNS_PER_IP": "2"}


def refusals(node: Node) -> int:
    """How many connections the server has refused for the per-address cap, from its own log."""
    return sum(1 for e in node.log_lines() if e.get("reason") == "per-IP cap reached")


def hold_slots(node: Node, count: int) -> list[RawConn]:
    """``count`` authenticated bare connections: they take a share of the per-address cap."""
    held: list[RawConn] = []
    for _ in range(count):
        conn = RawConn(node.driver_port)
        assert authenticate(conn).opcode == opcodes.AUTH_OK
        held.append(conn)
    return held


def test_a_connection_over_the_cap_is_refused_with_a_zero_id_overloaded_frame(
    own: Any,
) -> None:
    node = own(CAP)
    held = hold_slots(node, 2)
    intact = 0
    try:
        for _ in range(10):
            with RawConn(node.driver_port) as refused:
                # the reset trap (spec 8.3): our AUTH may still be unread when the server closes
                refused.send(0x01, frame.new_request_id(), serialize({"username": "admin"}))
                try:
                    answer = refused.read_frame()
                except (OSError, EOFError):
                    continue
                assert answer.opcode == opcodes.ERROR and answer.request_id == bytes(16)
                error = error_from_payload(decode(answer.payload), answer.request_id, Context("p"))
                assert isinstance(error, errors.OverloadedError) and error.wire_code == 13
                intact += 1
    finally:
        for conn in held:
            conn.close()
    RESULT_LINES.append(
        f"leg 6 cap: the zero-ID OVERLOADED frame arrived intact {intact} of 10 (Linux)"
    )
    assert intact == 10


def test_the_driver_surfaces_overloaded_and_a_later_connect_succeeds(
    mode: str, own: Any, tap_identity: faketls.Identity
) -> None:
    node = own(CAP)
    held = hold_slots(node, 2)
    try:
        with pytest.raises(errors.OverloadedError) as refused:
            connect_direct(mode, node, tap_identity, dial_timeout=2.0, request_timeout=3.0)
        assert refused.value.wire_code == 13 and refused.value.retryable
    finally:
        held.pop().close()
    try:
        pc = connect_direct(mode, node, tap_identity, dial_timeout=5.0, request_timeout=10.0)
        try:
            assert pc.call(lambda c: c.ping()) is None
        finally:
            pc.close()
    finally:
        for conn in held:
            conn.close()


def test_a_pool_survives_the_cap_and_a_write_is_resent_once_when_it_clears(
    mode: str, own: Any, tap_identity: faketls.Identity
) -> None:
    """One slot is the client's own connection, held busy by a lease; the other is a bare
    connection. A call that needs a second connection meets OVERLOADED on the dial (zero request
    ID): it is retried with backoff, the first connection is untouched, and a push (a write) that
    finally gets its connection is applied exactly once."""
    from hoarddb._deadline import Deadline

    node = own(CAP)
    database = f"capdb_{mode}"
    pc = connect_direct(mode, node, tap_identity, pool=hoarddb.PoolOptions(size=2))
    held: list[RawConn] = []
    try:
        pc.call(lambda c: c.create_database(database))
        pc.call(lambda c: c.database(database).create_bucket("q", "fifo"))
        held = hold_slots(node, 1)
        refused_before = refusals(node)

        def blocking(c: Any) -> Any:
            import threading

            pool = c._ctx.pool
            lease = pool.checkout("point", Deadline(5.0), op="hold", kind="READ")
            outcome: dict[str, Any] = {}

            def push() -> None:
                try:
                    outcome["result"] = c.database(database).bucket("q").push({"n": 1}, timeout=20)
                except errors.HoardDBError as caught:
                    outcome["error"] = caught

            worker = threading.Thread(target=push)
            worker.start()
            try:
                wait_for(lambda: refusals(node) > refused_before, "the second dial to be refused")
                held.pop().close()  # the cap clears while the push is backing off
                worker.join(timeout=20)
            finally:
                pool.release(lease, reusable=True)
            assert not worker.is_alive()
            return outcome

        async def asynchronous(c: Any) -> Any:
            import asyncio

            pool = c._ctx.pool
            lease = await pool.checkout("point", Deadline(5.0), op="hold", kind="READ")
            task = asyncio.ensure_future(
                c.database(database).bucket("q").push({"n": 1}, timeout=20)
            )
            outcome: dict[str, Any] = {}
            try:
                deadline = asyncio.get_running_loop().time() + 10
                while refusals(node) <= refused_before:
                    assert asyncio.get_running_loop().time() < deadline, "no refused dial"
                    await asyncio.sleep(0.02)
                held.pop().close()
                try:
                    outcome["result"] = await asyncio.wait_for(task, 20)
                except errors.HoardDBError as caught:
                    outcome["error"] = caught
            finally:
                pool.release(lease, reusable=True)
            return outcome

        outcome = pc.call(blocking if mode == "sync" else asynchronous)
        assert "error" not in outcome, f"the push must succeed once the cap clears: {outcome}"
        assert pc.call(lambda c: c.database(database).bucket("q").count()) == 1, "applied once"
        assert refusals(node) > refused_before, "the dial really was refused by the cap"
        stats = pc.call(lambda c: c.pool_stats())
        assert not stats.closed and stats.discarded == 0
        assert pc.call(lambda c: c.ping()) is None
    finally:
        for conn in held:
            conn.close()
        try:
            pc.call(lambda c: c.drop_database(database))
        finally:
            pc.close()
