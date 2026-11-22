# SPDX-License-Identifier: Apache-2.0
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false
# The typed JSON of the corpus is untyped by nature; these helpers interpret it.
"""Helpers for the BSON corpus (testdata/bson-vectors/v1.json): its typed JSON form to Python.

``to_python`` builds the value the codec is documented to write for a typed value, so a test can
say "writing this gives exactly these bytes". A type with no Python form, or an int64 that fits an
int32 (a Python ``int`` in that range is written int32 by design), is built as an ``Opaque`` whose
body this module computes with ``struct`` alone. ``decoded=True`` builds what a decoder is
documented to *return* instead (an int64 is just an ``int``).
"""

from __future__ import annotations

import json
import math
import struct
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from hoarddb._bson import Opaque, serialize
from vectors import BSON_CORPUS, Vector

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
INT32_MIN, INT32_MAX = -(2**31), 2**31 - 1


def corpus() -> list[Vector]:
    data = json.loads(BSON_CORPUS.read_text(encoding="utf-8"))
    out: list[Vector] = data["vectors"]
    return out


def corpus_header() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(BSON_CORPUS.read_text(encoding="utf-8"))
    return data


def _string_body(text: str) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack("<i", len(raw) + 1) + raw + b"\x00"


def to_python(value: Any, *, decoded: bool = False, last_wins: bool = True) -> Any:
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, list):
        return [to_python(v, decoded=decoded) for v in value]
    assert isinstance(value, dict) and len(value) == 1, value
    ((tag, inner),) = value.items()
    if tag == "$doc":
        out: dict[str, Any] = {}
        for key, item in inner:
            out[key] = to_python(item, decoded=decoded)  # a repeated key: the last wins
        return out
    if tag == "$numberInt":
        return int(inner)
    if tag == "$numberLong":
        n = int(inner)
        if not decoded and INT32_MIN <= n <= INT32_MAX:
            return Opaque("int64", struct.pack("<q", n))
        return n
    if tag == "$numberDouble":
        if inner == "0":
            return 0.0
        return (
            {"NaN": math.nan, "Infinity": math.inf, "-Infinity": -math.inf, "-0": -0.0}.get(inner)
            if inner in ("NaN", "Infinity", "-Infinity", "-0")
            else float(inner)
        )
    if tag == "$numberDecimal":
        return Decimal(inner)
    if tag == "$date":
        millis = int(inner["$numberLong"])
        try:
            return EPOCH + timedelta(milliseconds=millis)
        except OverflowError:
            return Opaque("datetime", struct.pack("<q", millis))
    if tag == "$binary":
        sub = int(inner["subType"], 16)
        data = bytes.fromhex(inner["hex"])
        if sub == 0:
            return data
        if sub == 4 and len(data) == 16:
            return uuid.UUID(bytes=data)
        if sub == 2:
            data = struct.pack("<i", len(data)) + data
        return Opaque("binary", struct.pack("<i", len(data)) + bytes([sub]) + data)
    if tag == "$oid":
        return Opaque("objectid", bytes.fromhex(inner))
    if tag == "$timestamp":
        return Opaque("timestamp", struct.pack("<II", inner["i"], inner["t"]))
    if tag == "$regularExpression":
        body = inner["pattern"].encode() + b"\x00" + inner["options"].encode() + b"\x00"
        return Opaque("regex", body)
    if tag == "$code":
        return Opaque("javascript", _string_body(inner))
    if tag == "$codeWithScope":
        scope = serialize(to_python(inner["scope"]))
        code = _string_body(inner["code"])
        return Opaque(
            "javascript_with_scope", struct.pack("<i", 4 + len(code) + len(scope)) + code + scope
        )
    if tag == "$symbol":
        return Opaque("symbol", _string_body(inner))
    if tag == "$undefined":
        return Opaque("undefined", b"")
    if tag == "$minKey":
        return Opaque("minkey", b"")
    if tag == "$maxKey":
        return Opaque("maxkey", b"")
    if tag == "$dbPointer":
        return Opaque("dbpointer", _string_body(inner["namespace"]) + bytes.fromhex(inner["oid"]))
    raise AssertionError(f"unknown typed form {tag}")


def canon(value: Any) -> Any:
    """A structure that is equal exactly when two values are the same BSON value: types matter
    (bool is not int), NaN equals NaN, -0.0 is not 0.0, a Decimal keeps its exponent."""
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int):
        return ("int", value)
    if isinstance(value, float):
        return ("nan",) if math.isnan(value) else ("float", struct.pack("<d", value))
    if isinstance(value, Decimal):
        if value.is_nan():
            return ("dec-nan", value.is_signed())
        return ("dec", value.is_signed(), value.as_tuple().digits, value.as_tuple().exponent)
    if isinstance(value, dict):
        return ("doc", [(k, canon(v)) for k, v in value.items()])
    if isinstance(value, list | tuple):
        return ("arr", [canon(v) for v in value])
    if isinstance(value, datetime):
        return ("dt", value.utcoffset(), value)
    return (type(value).__name__, value)
