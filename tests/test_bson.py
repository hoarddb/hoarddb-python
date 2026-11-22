# SPDX-License-Identifier: Apache-2.0
"""P4: the in-tree BSON codec: hand-computed bytes, the Python mapping, the hard limits."""

from __future__ import annotations

import contextlib
import enum
import math
import random
import struct
import time
import tracemalloc
import uuid
from collections import OrderedDict
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any

import pytest

import bsonvec
from hoarddb import Opaque, errors
from hoarddb._bson import MAX_DEPTH, deserialize, serialize

# --- hand-computed bytes -----------------------------------------------------------------------
# Layout: int32 length | elements | 00. An element is type byte | key cstring | body.
# Every literal below was computed by hand from the BSON format, not produced by the codec.

HAND: list[tuple[str, dict[str, Any], str]] = [
    ("empty document", {}, "0500000000"),
    # 0c = 4 length + 1 type + 2 "a\0" + 4 body + 1 terminator
    ("int32 zero", {"a": 0}, "0c0000001061000000000000"),
    ("int32 max", {"a": 2**31 - 1}, "0c000000106100ffffff7f00"),
    ("int32 min", {"a": -(2**31)}, "0c0000001061000000008000"),
    ("int32 -1", {"a": -1}, "0c000000106100ffffffff00"),
    # 2**31 no longer fits int32: type 12, eight bytes little-endian 00 00 00 80 00 00 00 00
    ("int64 just over int32", {"a": 2**31}, "10000000126100000000800000000000"),
    ("int64 just under int32", {"a": -(2**31) - 1}, "10000000126100ffffff7fffffffff00"),
    ("int64 max", {"a": 2**63 - 1}, "10000000126100ffffffffffffff7f00"),
    ("int64 min", {"a": -(2**63)}, "10000000126100000000000000008000"),
    ("double 1.5", {"a": 1.5}, "10000000016100000000000000f83f00"),  # 0x3ff8000000000000
    ("double nan", {"a": math.nan}, "10000000016100000000000000f87f00"),  # 0x7ff8000000000000
    ("double -0.0", {"a": -0.0}, "10000000016100000000000000008000"),
    ("double inf", {"a": math.inf}, "10000000016100000000000000f07f00"),
    ("string hi", {"a": "hi"}, "0f0000000261000300000068690000"),  # length 3 counts the NUL
    ("string with NUL inside", {"a": "x\x00y"}, "10000000026100040000007800790000"),
    ("string empty", {"a": ""}, "0d000000026100010000000000"),
    ("string 2-byte char", {"a": "é"}, "0f00000002610003000000c3a90000"),
    ("bool True", {"a": True}, "090000000861000100"),
    ("bool False", {"a": False}, "090000000861000000"),
    ("null", {"a": None}, "080000000a610000"),
    # binary: type 05, length 2, subtype 00, bytes 01 02
    ("bytes", {"a": b"\x01\x02"}, "0f0000000561000200000000010200"),
    (
        "uuid is subtype 4",
        {"a": uuid.UUID(int=1)},
        "1d00000005610010000000040000000000000000000000000000000100",
    ),
    ("datetime epoch", {"a": datetime(1970, 1, 1, tzinfo=UTC)}, "10000000096100000000000000000000"),
    (
        "datetime one ms before the epoch",
        {"a": datetime(1969, 12, 31, 23, 59, 59, 999000, tzinfo=UTC)},
        "10000000096100ffffffffffffffff00",
    ),
    (
        "datetime 0001-01-01",
        {"a": datetime(1, 1, 1, tzinfo=UTC)},
        "100000000961000028d3ed7cc7ffff00",
    ),  # -62135596800000
    (
        "datetime 9999-12-31T23:59:59.999",
        {"a": datetime(9999, 12, 31, 23, 59, 59, 999000, tzinfo=UTC)},
        "10000000096100ffdb1fd277e6000000",
    ),  # 253402300799999
    ("embedded document", {"a": {"b": 1}}, "140000000361000c000000106200010000000000"),
    ("array", {"a": [1, 2]}, "1b0000000461001300000010300001000000103100020000000000"),
    ("tuple is an array", {"a": (1, 2)}, "1b0000000461001300000010300001000000103100020000000000"),
    ("key order is insertion order", {"b": 1, "a": 2}, "13000000106200010000001061000200000000"),
    ("empty key", {"": 1}, "0b00000010000100000000"),
    ("array of 11 uses a 2-digit key for index 10", {"a": list(range(11))}, "_ARRAY11_"),
]


def _array11() -> str:
    body = b"".join(b"\x10" + str(i).encode() + b"\x00" + struct.pack("<i", i) for i in range(11))
    inner = struct.pack("<i", 4 + len(body) + 1) + body + b"\x00"
    outer = b"\x04a\x00" + inner
    return (struct.pack("<i", 4 + len(outer) + 1) + outer + b"\x00").hex()


@pytest.mark.parametrize(("name", "doc", "hexed"), HAND, ids=[h[0] for h in HAND])
def test_hand_computed_bytes(name: str, doc: dict[str, Any], hexed: str) -> None:
    if hexed == "_ARRAY11_":
        hexed = _array11()
    assert serialize(doc).hex() == hexed
    expected: dict[str, Any] = {
        k: list(v) if isinstance(v, tuple) else v  # pyright: ignore[reportUnknownArgumentType]
        for k, v in doc.items()
    }
    assert bsonvec.canon(deserialize(bytes.fromhex(hexed))) == bsonvec.canon(expected)


def test_the_opaque_types_are_written_back_byte_for_byte() -> None:
    cases = {
        "objectid": ("07", bytes(range(12))),
        "timestamp": ("11", struct.pack("<II", 7, 1700000000)),
        "regex": ("0b", b"^a.*b$\x00im\x00"),
        "javascript": ("0d", struct.pack("<i", 4) + b"x+1\x00"),
        "symbol": ("0e", struct.pack("<i", 2) + b"s\x00"),
        "undefined": ("06", b""),
        "minkey": ("ff", b""),
        "maxkey": ("7f", b""),
        "dbpointer": ("0c", struct.pack("<i", 3) + b"ns\x00" + bytes(range(12))),
        "binary": ("05", struct.pack("<i", 1) + b"\x80" + b"u"),
    }
    for name, (type_hex, body) in cases.items():
        element = bytes.fromhex(type_hex) + b"a\x00" + body
        raw = struct.pack("<i", 4 + len(element) + 1) + element + b"\x00"
        assert serialize({"a": Opaque(name, body)}) == raw
        assert deserialize(raw) == {"a": Opaque(name, body)}


def test_opaque_is_a_value() -> None:
    a = Opaque("objectid", bytes(12))
    assert a == Opaque("objectid", bytes(12))
    assert a != Opaque("objectid", bytes(11) + b"\x01")
    assert hash(a) == hash(Opaque("objectid", bytes(12)))
    assert repr(a).startswith("Opaque(")


def test_an_unknown_opaque_type_is_refused() -> None:
    with pytest.raises(errors.InvalidArgumentError, match="Opaque"):
        serialize({"a": Opaque("nonsense", b"")})


def test_a_datetime_past_year_9999_reads_as_opaque_never_overflow() -> None:
    body = struct.pack("<q", 253402300800000)
    raw = struct.pack("<i", 4 + 1 + 2 + 8 + 1) + b"\x09a\x00" + body + b"\x00"
    assert deserialize(raw) == {"a": Opaque("datetime", body)}
    assert serialize({"a": Opaque("datetime", body)}) == raw


# --- the Python mapping ------------------------------------------------------------------------


def test_bool_is_written_as_a_boolean_never_an_integer() -> None:
    assert serialize({"ok": True}) == bytes.fromhex("0a000000086f6b000100")
    assert serialize({"ok": 1}) == bytes.fromhex("0d000000106f6b000100000000")
    assert deserialize(serialize({"ok": True}))["ok"] is True


def test_a_naive_datetime_is_refused_with_the_fix() -> None:
    with pytest.raises(errors.InvalidArgumentError) as info:
        serialize({"when": datetime(2026, 10, 13, 12, 0)})
    message = info.value.message
    assert "when" in message
    assert "pass datetime.now(timezone.utc), or datetime.fromisoformat(...)" in message
    assert ".replace(tzinfo=timezone.utc)" in message
    assert "datetime.utcnow()" in message


def test_a_date_is_refused() -> None:
    with pytest.raises(errors.InvalidArgumentError, match="wrap it in a datetime"):
        serialize({"d": date(2026, 10, 13)})


def test_another_zone_is_converted_to_utc_first() -> None:
    plus_one = timezone(timedelta(hours=1))
    assert serialize({"a": datetime(1970, 1, 1, 1, tzinfo=plus_one)}) == serialize(
        {"a": datetime(1970, 1, 1, tzinfo=UTC)}
    )
    assert deserialize(serialize({"a": datetime(2026, 1, 1, 5, tzinfo=plus_one)}))["a"] == datetime(
        2026, 1, 1, 4, tzinfo=UTC
    )


def test_datetime_is_floored_not_truncated() -> None:
    instant = datetime(1969, 12, 31, 23, 59, 59, 999500, tzinfo=UTC)
    assert serialize({"a": instant}) == serialize(
        {"a": datetime(1969, 12, 31, 23, 59, 59, 999000, tzinfo=UTC)}
    )
    assert struct.unpack_from("<q", serialize({"a": instant}), 7)[0] == -1
    assert int(instant.timestamp() * 1000) == 0  # the wrong answer, for the record


def test_a_decoded_datetime_is_aware_utc() -> None:
    back = deserialize(serialize({"a": datetime(2026, 10, 13, tzinfo=UTC)}))["a"]
    assert back.tzinfo is UTC
    assert back == datetime(2026, 10, 13, tzinfo=UTC)


def test_an_int_past_int64_is_refused_naming_the_field_and_the_range() -> None:
    for bad in (2**63, -(2**63) - 1):
        with pytest.raises(errors.InvalidArgumentError) as info:
            serialize({"big": bad})
        assert "big" in info.value.message
        assert "-9223372036854775808" in info.value.message
        assert "9223372036854775807" in info.value.message


def test_keys_must_be_strings_without_nul() -> None:
    with pytest.raises(errors.InvalidArgumentError, match="key 1"):
        serialize({1: "x"})  # type: ignore[dict-item]
    with pytest.raises(errors.InvalidArgumentError, match="NUL"):
        serialize({"a\x00b": 1})
    assert deserialize(serialize({"": 1, "$x.y": 2, "é": 3})) == {"": 1, "$x.y": 2, "é": 3}


def test_a_lone_surrogate_is_refused_naming_the_field_path() -> None:
    with pytest.raises(errors.InvalidArgumentError) as info:
        serialize({"outer": {"inner": "bad\ud800"}})
    assert "outer.inner" in info.value.message
    with pytest.raises(errors.InvalidArgumentError, match=r"tags\[2\]"):
        serialize({"tags": ["a", "b", "\udfff"]})


def test_other_objects_are_refused_with_the_field_and_the_type() -> None:
    class Thing:
        pass

    bads: list[object] = [{1, 2}, PurePosixPath("/x"), Thing(), frozenset(), 1 + 2j]
    for bad in bads:
        with pytest.raises(errors.InvalidArgumentError) as info:
            serialize({"field": bad})
        assert "field" in info.value.message
        assert type(bad).__name__ in info.value.message
        assert "convert it first" in info.value.message


def test_buffers_and_mappings_and_enums() -> None:
    assert serialize({"a": bytearray(b"\x01")}) == serialize({"a": b"\x01"})
    assert serialize({"a": memoryview(b"\x01")}) == serialize({"a": b"\x01"})
    assert serialize(OrderedDict(a=1)) == serialize({"a": 1})
    assert serialize(MappingProxyType({"a": {"b": MappingProxyType({})}})) == serialize(
        {"a": {"b": {}}}
    )

    class Colour(enum.StrEnum):
        RED = "red"

    class Level(enum.IntEnum):
        HIGH = 3

    assert serialize({"a": Colour.RED, "b": Level.HIGH}) == serialize({"a": "red", "b": 3})
    assert deserialize(serialize({"a": Colour.RED}))["a"] == "red"
    assert type(deserialize(serialize({"b": Level.HIGH}))["b"]) is int


def test_uuid_round_trips() -> None:
    value = uuid.UUID("123e4567-e89b-12d3-a456-426614174000")
    assert deserialize(serialize({"u": value}))["u"] == value


def test_decoded_binary_is_bytes() -> None:
    value = deserialize(serialize({"b": b"\x00\xff"}))["b"]
    assert type(value) is bytes


def test_nesting_to_100_is_written_and_101_is_refused_without_recursion_error() -> None:
    assert MAX_DEPTH == 100

    def nest(depth: int) -> dict[str, Any]:
        doc: dict[str, Any] = {}
        for _ in range(depth - 1):
            doc = {"a": doc}
        return doc

    assert deserialize(serialize(nest(100))) == nest(100)
    with pytest.raises(errors.InvalidArgumentError, match="100"):
        serialize(nest(101))
    cyc: dict[str, Any] = {}
    cyc["self"] = cyc
    with pytest.raises(errors.InvalidArgumentError):
        serialize(cyc)


def test_a_non_mapping_top_level_is_refused() -> None:
    with pytest.raises(errors.InvalidArgumentError):
        serialize([1, 2])  # type: ignore[arg-type]


def test_decoder_accepts_any_bytes_like() -> None:
    raw = serialize({"a": [1, "x", None]})
    assert deserialize(bytearray(raw)) == deserialize(memoryview(raw)) == deserialize(raw)


def test_a_duplicate_key_keeps_the_last_value_in_the_first_position() -> None:
    raw = bytes.fromhex("1a00000010610001000000106200020000001061000300000000")
    assert list(deserialize(raw).items()) == [("a", 3), ("b", 2)]


def test_array_keys_are_ignored_on_read() -> None:
    inner = bytes.fromhex("130000001031000a0000001030001400000000")
    raw = struct.pack("<i", 4 + 1 + 2 + len(inner) + 1) + b"\x04a\x00" + inner + b"\x00"
    assert deserialize(raw) == {"a": [10, 20]}


def test_the_decoder_wraps_a_hostile_declared_length_without_allocating_it() -> None:
    raw = bytes.fromhex("ffffff7f1061000100000000")
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        with pytest.raises(errors.ProtocolError):
            deserialize(raw)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 64 * 1024


def test_a_declared_document_length_that_disagrees_is_a_protocol_error_at_every_level() -> None:
    for text in (
        "0d0000001061000100000000",  # one more than given
        "0b0000001061000100000000",  # ends before the element does
        "0c0000001061000100000000ff",  # trailing byte
        "1400000003610006000000106100010000000000",  # nested length lie
    ):
        with pytest.raises(errors.ProtocolError):
            deserialize(bytes.fromhex(text))


# --- properties and fuzz (seeded: deterministic) ------------------------------------------------


def _random_value(rng: random.Random, depth: int) -> Any:
    kinds = ["int32", "int64", "float", "str", "bytes", "bool", "none", "dt", "uuid", "dec"]
    if depth < 5:
        kinds += ["list", "dict", "dict"]
    kind = rng.choice(kinds)
    if kind == "int32":
        return rng.randint(-(2**31), 2**31 - 1)
    if kind == "int64":
        return (
            rng.choice([-1, 1]) * rng.randint(2**31, 2**63 - 1) if rng.random() < 0.9 else 2**63 - 1
        )
    if kind == "float":
        return rng.choice(
            [0.0, -0.0, 1.5, math.inf, -math.inf, math.nan, rng.uniform(-1e300, 1e300)]
        )
    if kind == "str":
        alphabet = "abc é中\U0001f600\x00\n"
        return "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 12)))
    if kind == "bytes":
        return bytes(rng.getrandbits(8) for _ in range(rng.randrange(0, 20)))
    if kind == "bool":
        return rng.random() < 0.5
    if kind == "none":
        return None
    if kind == "dt":
        ms = rng.randint(-62135596800000, 253402300799999)
        return bsonvec.EPOCH + timedelta(milliseconds=ms)
    if kind == "uuid":
        return uuid.UUID(int=rng.getrandbits(128))
    if kind == "dec":
        return Decimal(rng.choice(["0", "-0", "1.50", "NaN", "Infinity", "-1E+10", "123456.789"]))
    if kind == "list":
        return [_random_value(rng, depth + 1) for _ in range(rng.randrange(0, 5))]
    return {
        f"k{i}{rng.choice('xyz')}": _random_value(rng, depth + 1)
        for i in range(rng.randrange(0, 5))
    }


def test_property_serialize_then_deserialize_is_the_identity() -> None:
    rng = random.Random(0xB50)
    for _ in range(600):
        doc = {f"f{i}": _random_value(rng, 0) for i in range(rng.randrange(0, 6))}
        raw = serialize(doc)
        assert bsonvec.canon(deserialize(raw)) == bsonvec.canon(doc)
        assert serialize(deserialize(raw)) == raw


def test_fuzz_decoding_arbitrary_bytes_only_ever_raises_protocol_error() -> None:
    rng = random.Random(0xF022)
    seeds = [
        v["hex"]
        for v in bsonvec.corpus()
        if v["kind"] in ("encode", "decode") and len(v["hex"]) < 600
    ]
    assert len(seeds) > 50
    for _ in range(6000):
        data = bytearray(bytes.fromhex(rng.choice(seeds)))
        for _ in range(rng.randrange(1, 4)):
            roll = rng.random()
            if roll < 0.5 and data:
                data[rng.randrange(len(data))] = rng.getrandbits(8)
            elif roll < 0.7 and data:
                del data[rng.randrange(len(data)) :]
            elif roll < 0.85:
                data.insert(rng.randrange(len(data) + 1), rng.getrandbits(8))
            else:
                data.extend(rng.getrandbits(8) for _ in range(rng.randrange(1, 5)))
        try:
            result = deserialize(bytes(data))
        except errors.ProtocolError:
            continue
        assert isinstance(result, dict)
    for _ in range(3000):
        blob = bytes(rng.getrandbits(8) for _ in range(rng.randrange(0, 60)))
        with contextlib.suppress(errors.ProtocolError):
            deserialize(blob)


# --- the performance gate (budget written in docs/python-build.md before P4) --------------------

BUDGET_SECONDS = 1.5
CEILING_SECONDS = 3.0


def _benchmark_document(i: int) -> dict[str, Any]:
    return {
        "_id_": f"user-{i:08d}",
        "name": "Ada Lovelace " * 3,
        "email": f"ada{i}@example.org",
        "age": 36 + i % 40,
        "visits": 2**33 + i,
        "score": 1234.5678 + i,
        "active": i % 2 == 0,
        "born": datetime(1815, 12, 10, tzinfo=UTC) + timedelta(days=i),
        "bio": "x" * 200,
        "city": "London",
        "country": "United Kingdom",
        "tags": [f"tag{n}" for n in range(8)],
        "scores": list(range(8)),
        "address": {"street": "10 St James's Square", "city": "London", "zip": "SW1Y 4LB", "n": i},
        "nick": None,
        "uuid": uuid.UUID(int=i),
        "blob": b"\x00\x01\x02\x03" * 8,
        "f1": 1.5,
        "f2": 2.5,
        "f3": 3,
        "f4": 4,
        "f5": "five",
        "f6": "six",
        "f7": "seven",
    }


def test_decoding_10000_documents_of_about_1_kib_is_within_budget() -> None:
    page = [serialize(_benchmark_document(i)) for i in range(100)]
    size = sum(len(p) for p in page) / len(page)
    assert 800 <= size <= 1400, size
    start = time.perf_counter()
    for _ in range(100):
        for raw in page:
            deserialize(raw)
    elapsed = time.perf_counter() - start
    print(f"\nBSON decode: 10,000 documents of {size:.0f} bytes in {elapsed:.3f} s")
    assert elapsed < CEILING_SECONDS, f"{elapsed:.2f} s is over the {CEILING_SECONDS} s ceiling"
    if elapsed > BUDGET_SECONDS:
        pytest.fail(f"{elapsed:.2f} s is over the {BUDGET_SECONDS} s budget")


# --- the driver vectors' own payloads, and the independent test-only reader ----------------------


def _payloads() -> list[tuple[str, bytes]]:
    import vectors

    out: list[tuple[str, bytes]] = []
    for vec in vectors.driver_vectors(kind="exchange"):
        for side in ("request", "response"):
            part = vec[side]
            raw = part.get("payload_hex")
            if not raw or part["opcode_name"] in ("GET_FAST", "GET_FAST_RESPONSE"):
                continue
            if "payload_json" not in part:
                continue  # raw bytes on purpose (testing.md section 1.1)
            out.append((f"{vec['name']} ({side})", bytes.fromhex(raw)))
    return out


def _plain(value: Any) -> Any:
    """Reduce the codec's richer values to what the test-only reader can produce."""
    if isinstance(value, datetime):
        return int((value - bsonvec.EPOCH) // timedelta(milliseconds=1))
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, list):
        return [_plain(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, uuid.UUID):
        return value.bytes
    if isinstance(value, Decimal):
        return value.as_tuple()
    return value


def test_every_payload_in_the_driver_vectors_decodes_and_agrees_with_the_test_reader() -> None:
    import minibson

    payloads = _payloads()
    assert len(payloads) > 300
    compared = 0
    for name, raw in payloads:
        decoded = deserialize(raw)  # must not raise: these are the server's own bytes
        assert serialize(deserialize(serialize(decoded))) == serialize(decoded), name
        try:
            reference = minibson.loads(raw)
        except AssertionError:
            continue  # a type the small reference reader does not know
        if any(isinstance(v, Opaque) for v in _walk(decoded)):
            continue
        assert bsonvec.canon(_plain(decoded)) == bsonvec.canon(_plain(reference)), name
        compared += 1
    assert compared > 250


def _walk(value: Any) -> list[Any]:
    if isinstance(value, dict):
        return [x for v in value.values() for x in _walk(v)]  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, list):
        return [x for v in value for x in _walk(v)]  # pyright: ignore[reportUnknownVariableType]
    return [value]
