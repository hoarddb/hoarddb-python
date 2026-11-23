# SPDX-License-Identifier: Apache-2.0
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false, reportUnknownLambdaType=false
"""Leg 5: the same bad requests give the same code and error class on both surfaces, and the
server's sentence verbatim (the sentence is read off the wire by the tap, not from the driver)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

import hoarddb
import replay
from hoarddb import errors
from hoarddb._bson import serialize
from hoarddb._core import frame, opcodes
from hoarddb._core.failure import Context, error_from_payload
from realkit import Rig, Servers, decode
from tapkit import RawConn

pytestmark = pytest.mark.server

TUNED = {"HOARDDB_MAX_BLOB_BYTES": "1024", "HOARDDB_MAX_DOCUMENT_BYTES": "4096"}
DB = "leg5"

Case = tuple[str, Callable[[Any], Any], "int | None", type[errors.HoardDBError], list[str]]

CASES: list[Case] = [
    (
        "missing database",
        lambda c: c.database("leg5_nowhere").bucket("b").get("k"),
        1,
        errors.NotFoundError,
        ['database "leg5_nowhere" not found'],
    ),
    (
        "missing bucket",
        lambda c: c.database(DB).bucket("nope").get("k"),
        1,
        errors.NotFoundError,
        ['bucket "nope" not found in database'],
    ),
    (
        "duplicate database",
        lambda c: c.create_database(DB),
        2,
        errors.AlreadyExistsError,
        [f'database "{DB}" already exists'],
    ),
    (
        "bucket of another type: create",
        lambda c: c.database(DB).create_bucket("hashes", "btree"),
        2,
        errors.AlreadyExistsError,
        ["hash", "btree"],
    ),
    (
        "bucket of another type: read a queue by key",
        lambda c: c.database(DB).bucket("queue").get("k"),
        # v1.1.0 answers a verb the bucket's type does not take with an ERROR frame,
        # INVALID_ARGUMENT (wire code 4). An earlier server refused in band, in a success frame.
        4,
        errors.InvalidArgumentError,
        ["get is not supported for bucket type fifo", "A fifo bucket takes:"],
    ),
    (
        "bucket of another type: range on a hash bucket",
        lambda c: c.database(DB).bucket("hashes").range().to_list(),
        4,
        errors.InvalidArgumentError,
        ["is type hash", "requires type btree"],
    ),
    (
        "bucket of another type: update a queue",
        lambda c: c.database(DB).bucket("queue").update("k", {"$set": {"a": 1}}),
        4,
        errors.InvalidArgumentError,
        ["is type fifo", "requires type hash or btree"],
    ),
    (
        "oversized value: a document",
        lambda c: c.database(DB).bucket("hashes").put("big", {"Text": "x" * 5000}),
        4,
        errors.InvalidArgumentError,
        ['Document "big" is'],
    ),
    (
        "oversized value: a blob",
        lambda c: c.database(DB).bucket("blobs").put_blob("big.bin", b"x" * 2000),
        4,
        errors.InvalidArgumentError,
        ['Blob "big.bin" is', "this server's limit is"],
    ),
    (
        "mistyped document",
        lambda c: c.database(DB).bucket("typed").put("ada", {"Name": "Ada", "Born": "eighteen"}),
        16,
        errors.FieldTypeError,
        ["Born"],
    ),
]


def prepare(rig: Rig) -> None:
    """The database every case aims at: a hash bucket, a queue, a blob bucket, a typed bucket."""
    rig.call(lambda c: c.create_database(DB))
    rig.call(lambda c: c.database(DB).create_bucket("hashes", "hash"))
    rig.call(lambda c: c.database(DB).create_bucket("queue", "fifo"))
    rig.call(lambda c: c.database(DB).create_bucket("blobs", "blob"))
    types = hoarddb.FieldTypes(
        [hoarddb.FieldSpec("Name", "string", required=True), hoarddb.FieldSpec("Born", "int")]
    )
    rig.call(lambda c: c.database(DB).create_bucket("typed", "hash", field_types=types))


def attempt(rig: Rig, fn: Callable[[Any], Any]) -> tuple[errors.HoardDBError, str]:
    """The error the driver raised and the sentence the server really sent for it."""
    rig.tap.clear()
    raised: errors.HoardDBError | None = None
    try:
        rig.call(fn)
    except errors.HoardDBError as caught:
        raised = caught
    assert raised is not None, "the bad request must not succeed"
    sent = rig.tap.requests()
    assert sent, "the driver must have sent the request (a local refusal proves nothing here)"
    answer = rig.tap.answer_to(sent[-1])
    assert answer is not None
    return raised, str(decode(answer.payload)["message"])


@pytest.mark.parametrize("case", CASES, ids=lambda c: c[0])
def test_a_bad_request_gives_the_same_error_on_both_surfaces(case: Case, servers: Servers) -> None:
    name, fn, code, cls, fragments = case
    node = servers.node(TUNED)
    outcomes: list[tuple[str, int | None, str]] = []
    for mode in ("sync", "async"):
        rig = servers.rig(mode, node)
        try:
            prepare(rig)
            raised, wire_message = attempt(rig, fn)
        finally:
            rig.drop_every_database()
        assert isinstance(raised, cls), f"{name} via {mode}: {type(raised).__name__}, want {cls}"
        assert raised.wire_code == code
        assert raised.message == wire_message, "the server's sentence must pass through verbatim"
        for fragment in fragments:
            assert fragment in raised.message, (name, fragment, raised.message)
        outcomes.append((type(raised).__name__, raised.wire_code, raised.message))
    assert outcomes[0] == outcomes[1], "the two surfaces must raise the same error"


def test_an_unauthenticated_request_is_refused_with_the_servers_sentence(servers: Servers) -> None:
    node = servers.node(TUNED)
    request_id = frame.new_request_id()
    with RawConn(node.driver_port) as conn:
        conn.send(opcodes.GET, request_id, serialize({"database": "d", "bucket": "b", "key": "k"}))
        answer = conn.read_frame()
    assert answer.opcode == opcodes.ERROR and answer.request_id == request_id
    payload = decode(answer.payload)
    raised = error_from_payload(payload, answer.request_id, Context("probe"))
    assert isinstance(raised, errors.NotAuthenticatedError) and raised.wire_code == 12
    assert raised.message == payload["message"] == "connection not authenticated; send AUTH first"


def test_the_error_classes_here_are_the_ones_the_replay_maps() -> None:
    for _, _, code, cls, _ in CASES:
        if code is not None:
            assert replay.ERROR_CLASSES[code] is cls


def test_an_empty_blob_is_refused_by_the_server_and_passed_through(servers: Servers) -> None:
    """A zero-byte blob is refused with ERROR code 4 (INVALID_ARGUMENT) on v1.1.0, on purpose: it
    is the caller's argument that is wrong. An earlier server answered code 5 (INTERNAL). The
    driver reports the code the server sends and does not guess."""
    node = servers.node(TUNED)
    rig = servers.rig("sync", node)
    try:
        rig.call(lambda c: c.create_database(DB))
        rig.call(lambda c: c.database(DB).create_bucket("blobs", "blob"))
        raised, wire_message = attempt(
            rig, lambda c: c.database(DB).bucket("blobs").put_blob("e", b"")
        )
    finally:
        rig.drop_every_database()
    assert raised.wire_code == 4 and isinstance(raised, errors.InvalidArgumentError)
    assert raised.write_outcome == "not_applied"
    assert raised.message == wire_message and "no data to store" in wire_message
