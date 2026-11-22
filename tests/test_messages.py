# SPDX-License-Identifier: Apache-2.0
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false
"""P5: wire messages: explicit wire names, and leg 1 of the conformance harness for every
``exchange`` vector (request encode, response decode), testing.md section 1.1."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

import bsonvec
import ejson
import vectors
from hoarddb import errors
from hoarddb._bson import deserialize, serialize
from hoarddb._core import frame, messages, opcodes, schema
from hoarddb._secret import Secret

EXCHANGES = vectors.driver_vectors(kind="exchange")
SRC = Path(__file__).resolve().parent.parent / "src" / "hoarddb"


def _opcode_ok(name: str, number: int) -> bool:
    return name in ("UNKNOWN",) or opcodes.CODES[name] == number


def test_every_opcode_name_and_number_in_the_vectors_agrees_with_the_table() -> None:
    seen = 0
    for vec in EXCHANGES:
        for side in ("request", "response"):
            part = vec[side]
            assert _opcode_ok(part["opcode_name"], part["opcode"]), part["opcode_name"]
            seen += 1
    assert seen > 350
    assert opcodes.NAMES[0xA2] == "ERROR"
    assert len(set(opcodes.CODES.values())) == len(opcodes.CODES)


def test_every_request_class_has_a_response_and_opcodes_are_unique() -> None:
    assert len({cls.OPCODE for cls in messages.REQUESTS.values()}) == len(messages.REQUESTS)
    for opcode, cls in messages.REQUESTS.items():
        assert opcode == cls.OPCODE
        assert cls.RESPONSE_OPCODES, cls
        assert cls.RESPONSE is not None


# --- leg 1, request encode ---------------------------------------------------------------------


def _client_exchanges() -> list[vectors.Vector]:
    return [
        v
        for v in EXCHANGES
        if "payload_json" in v["request"] and v["request"]["opcode"] in messages.REQUESTS
    ]


def test_the_replay_covers_most_of_the_exchange_vectors() -> None:
    assert len(_client_exchanges()) >= 170


def _non_empty(value: Any) -> bool:
    return value not in (None, "", 0, False, [], {}, b"") and value != 0.0


@pytest.mark.parametrize("vec", _client_exchanges(), ids=lambda v: v["name"])
def test_request_encodes_to_the_allowed_fields_and_carries_every_non_empty_one(
    vec: vectors.Vector,
) -> None:
    request = vec["request"]
    cls = messages.REQUESTS[request["opcode"]]
    wanted = ejson.to_python(request["payload_json"])
    typed = schema.from_wire(cls, wanted)
    payload = messages.request_payload(typed)
    got = deserialize(payload)
    assert set(got) <= set(request["allowed_fields"]), set(got) - set(request["allowed_fields"])
    for name, value in wanted.items():
        if _non_empty(value):
            assert name in got, name
            assert bsonvec.canon(got[name]) == bsonvec.canon(value), name
        elif name in got:
            assert bsonvec.canon(got[name]) == bsonvec.canon(value), name


# --- leg 1, response decode --------------------------------------------------------------------


def _success_exchanges() -> list[vectors.Vector]:
    return [
        v for v in _client_exchanges() if v["response"]["opcode_name"] not in ("ERROR", "AUTH_FAIL")
    ]


def _walk(doc: Any, parts: list[str]) -> list[tuple[Any, str]]:
    """The (container, key) pairs a ``vary`` path such as ``nodes[].joinedat`` names."""
    head, rest = parts[0], parts[1:]
    is_list = head.endswith("[]")
    key = head[:-2] if is_list else head
    if not isinstance(doc, dict) or key not in doc:
        return []
    if not is_list and not rest:
        return [(doc, key)]
    inner = doc[key]
    if is_list:
        found: list[tuple[Any, str]] = []
        for item in inner:
            found.extend(_walk(item, rest) if rest else [])
        return found
    return _walk(inner, rest)


def _strip_vary(doc: Any, paths: list[str]) -> None:
    for path in paths:
        for container, key in _walk(doc, path.split(".")):
            if _non_empty(container[key]):
                container[key] = bsonvec.canon(container[key])[0]  # keep the type tag only


def _drop_empty(value: Any) -> Any:
    if isinstance(value, dict):  # the server keeps no field order, so neither does the comparison
        kept = {k: _drop_empty(v) for k, v in value.items() if _non_empty(_drop_empty(v))}
        return dict(sorted(kept.items()))
    if isinstance(value, list):
        return [_drop_empty(v) for v in value]
    return value


@pytest.mark.parametrize("vec", _success_exchanges(), ids=lambda v: v["name"])
def test_response_decodes_into_the_typed_response_and_back(vec: vectors.Vector) -> None:
    request, response = vec["request"], vec["response"]
    cls = messages.REQUESTS[request["opcode"]]
    raw = bytes.fromhex(response["payload_hex"]) if response.get("payload_hex") else b""
    reply = frame.Frame(response["opcode"], bytes.fromhex(response["request_id_hex"]), raw)
    typed = messages.parse_response(cls, reply)
    assert isinstance(typed, cls.RESPONSE)
    expected = ejson.to_python(response["payload_json"]) if "payload_json" in response else {}
    again = schema.to_wire(typed)
    vary = response.get("vary", [])
    _strip_vary(expected, vary)
    _strip_vary(again, vary)
    assert bsonvec.canon(_drop_empty(again)) == bsonvec.canon(_drop_empty(expected))
    # every wire name the answer's type can carry is one this response type knows
    known, raw_paths = schema.wire_paths(cls.RESPONSE)
    for path in response.get("fields", []):
        covered = path in known or any(path.startswith((r + ".", r + "[]")) for r in raw_paths)
        assert covered, f"{path} is not modelled by {cls.RESPONSE.__name__}"


# --- explicit wire names -----------------------------------------------------------------------


def test_the_wire_names_that_are_easy_to_get_wrong_are_pinned() -> None:
    assert (
        messages.PutRequest(
            database="d", bucket="b", key="k", data={}, write_concern="majority"
        ).__class__
        is messages.PutRequest
    )
    doc = messages.request_document(
        messages.PutRequest(database="d", bucket="b", key="k", data={}, write_concern="all")
    )
    assert doc == {"database": "d", "bucket": "b", "key": "k", "data": {}, "writeconcern": "all"}
    upd = messages.request_document(
        messages.UpdateRequest(database="d", bucket="b", key="k", add_to_set={"t": 1})
    )
    assert "addtoset" in upd
    assert "add_to_set" not in upd
    create = messages.request_document(
        messages.CreateBucketRequest(database="d", name="n", type="hash", schema_json="{}")
    )
    assert create["schemajson"] == "{}"
    blob = schema.from_wire(
        messages.BlobWriteResponse, {"blobid": "x", "size": 3, "key": "k", "success": True}
    )
    assert (blob.blob_id, blob.size) == ("x", 3)
    field_response = schema.from_wire(messages.FieldResponse, {"blob_id": "y", "entry_id": "e"})
    assert (field_response.blob_id, field_response.entry_id) == ("y", "e")
    dbs = schema.from_wire(
        messages.ListDatabasesResponse, {"databases": [{"name": "a", "bucketcount": 3}]}
    )
    assert dbs.databases[0].bucket_count == 3
    nodes = schema.from_wire(
        messages.ListNodesResponse,
        {"nodes": [{"nodeid": "n1", "addr": "a:1", "joinedat": None, "status": "up"}]},
    )
    assert nodes.nodes[0].node_id == "n1"
    assert schema.from_wire(messages.HealthResponse, {"bucketcount": 4}).bucket_count == 4
    assert schema.from_wire(messages.HealthResponse, {"nodeid": "n"}).node_id == "n"


def test_a_push_always_sends_priority_as_int64_even_zero() -> None:
    for cls in (messages.FifoPushRequest, messages.LifoPushRequest, messages.HeapPushRequest):
        payload = messages.request_payload(cls(database="d", bucket="b", data={"x": 1}))
        assert b"\x12priority\x00" + bytes(8) in payload
        assert deserialize(payload)["priority"] == 0
    heap = messages.request_payload(
        messages.HeapPushRequest(database="d", bucket="b", data={}, priority=5)
    )
    assert b"\x12priority\x00\x05" in heap


def test_inc_is_int64_and_mul_is_double_on_the_wire() -> None:
    payload = messages.request_payload(
        messages.UpdateRequest(
            database="d", bucket="b", key="k", inc={"n": 1}, mul={"x": 2}, set={"s": 1}
        )
    )
    assert b"\x12n\x00\x01\x00\x00\x00\x00\x00\x00\x00" in payload  # int64, not int32
    assert b"\x01x\x00\x00\x00\x00\x00\x00\x00\x00@" in payload  # the double 2.0
    assert b"\x10s\x00" in payload  # $set values are "as given": int32 here


def test_omitted_when_empty_but_kept_where_the_server_needs_them() -> None:
    doc = messages.request_document(messages.GetRequest(database="d", bucket="b", key="k"))
    assert doc == {"database": "d", "bucket": "b", "key": "k"}
    put = messages.request_document(messages.PutRequest(database="d", bucket="b", data={}))
    assert put["data"] == {}  # an empty document is a document
    assert "key" not in put
    scan = messages.request_document(
        messages.ScanRequest(database="d", bucket="b", expect_bucket_id=7, skip=0)
    )
    assert deserialize(serialize(scan))["expect_bucket_id"] == 7
    assert "skip" not in scan
    assert "expect_bucket_id" not in messages.request_document(
        messages.ScanRequest(database="d", bucket="b")
    )


# --- AUTH and secrets --------------------------------------------------------------------------


def test_auth_request_carries_the_documented_fields_and_hides_the_secret() -> None:
    req = messages.AuthRequest(
        username="admin",
        password=Secret("pw-sentinel"),
        client="hoarddb-python/0.0.0",
    )
    assert "pw-sentinel" not in repr(req)
    assert "pw-sentinel" not in str(req.__dict__ if hasattr(req, "__dict__") else req)
    doc = messages.request_document(req)
    assert doc == {
        "username": "admin",
        "password": "pw-sentinel",
        "protocol_version": 1,
        "capabilities": [],
        "client": "hoarddb-python/0.0.0",
    }
    token = messages.AuthRequest(username="a", token=Secret("tok-sentinel"), client="c")
    assert messages.request_document(token)["token"] == "tok-sentinel"
    assert "password" not in messages.request_document(token)
    assert "tok-sentinel" not in repr(token)


def test_auth_ok_keeps_the_token_secret_and_decodes_any_integer_width() -> None:
    ok = schema.from_wire(
        messages.AuthOkResponse,
        {
            "success": True,
            "token": "tok-sentinel",
            "token_expires_at": 1_700_000_900,
            "token_deadline": 1_700_043_200,
            "protocol_version": 1,
            "server_version": "1.2.3",
            "capabilities": ["auth_token", "future_thing"],
            "password_min_length": 12,
        },
    )
    assert ok.token is not None
    assert ok.token.reveal() == "tok-sentinel"
    assert "tok-sentinel" not in repr(ok)
    assert ok.capabilities == [
        "auth_token",
        "future_thing",
    ]  # unknown strings are kept, not an error


def test_the_secret_is_revealed_in_exactly_one_place() -> None:
    sites = [
        p.relative_to(SRC).as_posix()
        for p in sorted(SRC.rglob("*.py"))
        if re.search(r"\.reveal\(", p.read_text(encoding="utf-8"))
    ]
    assert sites == ["_core/schema.py"]


# --- decoding rules ----------------------------------------------------------------------------


def test_unknown_fields_are_ignored_and_absent_means_default() -> None:
    got = schema.from_wire(messages.GetResponse, {"found": True, "future_field": [1, 2]})
    assert got.found is True
    assert (got.data, got.message) == (None, "")
    empty = schema.from_wire(messages.GetResponse, {})
    assert (empty.found, empty.data, empty.message) == (False, None, "")


def test_any_integer_width_is_accepted() -> None:
    from hoarddb._bson import Opaque

    raw = serialize({"count": Opaque("int64", (5).to_bytes(8, "little")), "message": ""})
    assert schema.from_wire(messages.CountResponse, deserialize(raw)).count == 5
    raw = serialize({"count": 5})
    assert schema.from_wire(messages.CountResponse, deserialize(raw)).count == 5


@pytest.mark.parametrize(
    ("cls", "doc", "field", "where"),
    [
        (messages.GetResponse, {"found": "yes"}, "found", "GetResponse"),
        (messages.CountResponse, {"count": "5"}, "count", "CountResponse"),
        (messages.CountResponse, {"count": True}, "count", "CountResponse"),
        (messages.ScanResponse, {"documents": {"a": 1}}, "documents", "ScanResponse"),
        (messages.ListDatabasesResponse, {"databases": [{"name": 3}]}, "name", "DatabaseRow"),
    ],
)
def test_a_known_field_of_the_wrong_type_is_a_protocol_error_naming_it(
    cls: type[Any], doc: dict[str, Any], field: str, where: str
) -> None:
    with pytest.raises(errors.ProtocolError) as info:
        schema.from_wire(cls, doc)
    assert field in info.value.message
    assert where in info.value.message


def test_parse_response_checks_the_opcode() -> None:
    good = frame.Frame(opcodes.GET_RESPONSE, bytes(16), serialize({"found": True}))
    assert messages.parse_response(messages.GetRequest, good).found is True
    for wrong in (opcodes.PUT, opcodes.ERROR, opcodes.PONG):
        with pytest.raises(errors.ProtocolError, match="opcode"):
            messages.parse_response(
                messages.GetRequest, frame.Frame(wrong, bytes(16), serialize({}))
            )


def test_health_check_and_list_nodes_answer_get_response() -> None:
    assert opcodes.GET_RESPONSE in messages.HealthCheckRequest.RESPONSE_OPCODES
    assert opcodes.GET_RESPONSE in messages.ListNodesRequest.RESPONSE_OPCODES
    assert frozenset({opcodes.PONG}) == messages.PingRequest.RESPONSE_OPCODES


def test_ping_and_the_argument_less_requests_send_what_the_vectors_send() -> None:
    assert messages.request_payload(messages.PingRequest()) == b""
    assert messages.request_payload(messages.ListNodesRequest()) == b""
    for cls in (messages.ListDatabasesRequest, messages.HealthCheckRequest):
        assert messages.request_payload(cls()) == bytes.fromhex("0500000000")  # the empty document
    assert deserialize(messages.request_payload(messages.ListNodesRequest(history=True))) == {
        "history": True
    }
    wire = messages.request_frame(messages.PingRequest(), bytes(range(16)))
    decoded = frame.decode_frame(wire)
    assert (decoded.opcode, decoded.payload) == (opcodes.PING, b"")


def test_request_frame_refuses_a_too_large_payload_before_building_it() -> None:
    big = messages.PutRequest(database="d", bucket="b", key="k", data={"v": "x" * (33 * 2**20)})
    with pytest.raises(frame.FrameTooLarge):
        messages.request_frame(big, bytes(16))


# --- GET_FAST: the one payload that is not BSON -------------------------------------------------


def test_get_fast_requests_match_the_vectors_byte_for_byte() -> None:
    fast = [v for v in EXCHANGES if v["request"]["opcode_name"] == "GET_FAST"]
    assert len(fast) == 4
    checked = 0
    for vec in fast:
        raw = bytes.fromhex(vec["request"]["payload_hex"])
        if vec["name"].startswith("get_fast with a malformed"):
            continue  # deliberately two bytes
        db_len = raw[0]
        database = raw[1 : 1 + db_len].decode()
        rest = raw[1 + db_len :]
        bucket = rest[1 : 1 + rest[0]].decode()
        key_part = rest[1 + rest[0] :]
        key = key_part[2 : 2 + int.from_bytes(key_part[:2], "little")].decode()
        req = messages.GetFastRequest(database=database, bucket=bucket, key=key)
        assert messages.request_payload(req) == raw
        checked += 1
    assert checked >= 1


def test_get_fast_limits_are_enforced_locally() -> None:
    for kwargs in (
        {"database": "d" * 256, "bucket": "b", "key": "k"},
        {"database": "d", "bucket": "b" * 256, "key": "k"},
        {"database": "d", "bucket": "b", "key": "k" * 65536},
    ):
        with pytest.raises(errors.InvalidArgumentError, match="GET_FAST"):
            messages.request_payload(messages.GetFastRequest(**kwargs))
    ok = messages.GetFastRequest(database="d" * 255, bucket="b" * 255, key="k" * 65535)
    assert len(messages.request_payload(ok)) == 1 + 255 + 1 + 255 + 2 + 65535


def test_get_fast_response_is_a_status_byte_then_a_document() -> None:
    found = messages.GetFastResponse.from_payload(b"\x00" + serialize({"a": 1}))
    assert found.found is True
    assert found.data == {"a": 1}
    miss = messages.GetFastResponse.from_payload(b"\x01")
    assert (miss.found, miss.data) == (False, None)
    for bad in (b"", b"\x02", b"\x01junk", b"\x00"):
        with pytest.raises(errors.ProtocolError):
            messages.GetFastResponse.from_payload(bad)
