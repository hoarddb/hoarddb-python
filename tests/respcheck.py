# SPDX-License-Identifier: Apache-2.0
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false
"""Compare a real server's answer with a vector's response rules (testing.md section 1.1).

The vector's ``payload_json`` holds the answer as captured and normalised. A real answer must carry
the same values, except the ``vary`` paths: for those the PRESENCE and the BSON TYPE must agree
(a normalised string is a string, an int64 is an int64). Fields the answer's type may carry are
``fields`` plus ``additive_fields``; ``absent_fields`` must not appear.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from typing import Any

import bsonvec
import ejson
from hoarddb._bson import deserialize

Rewrite = Callable[[str], str]


def rewritten(value: Any, rewrite: Rewrite) -> Any:
    """``value`` with ``rewrite`` applied to every string in it (the ports and keys of this run)."""
    if isinstance(value, str):
        return rewrite(value)
    if isinstance(value, dict):
        return {k: rewritten(v, rewrite) for k, v in value.items()}
    if isinstance(value, list):
        return [rewritten(v, rewrite) for v in value]
    return value


class Wild:
    """Equal to anything: stands for a varying value in an expected result."""

    def __eq__(self, other: object) -> bool:
        return True

    def __hash__(self) -> int:
        return 0

    def __repr__(self) -> str:
        return "<varies>"


def typemap(payload: bytes) -> dict[str, set[int]]:
    """Every element's BSON type byte by path: ``a.b`` for fields, ``a[]`` for array elements."""
    found: dict[str, set[int]] = {}
    _walk(payload, 0, "", found)
    return found


_FIXED = {0x01: 8, 0x07: 12, 0x08: 1, 0x09: 8, 0x0A: 0, 0x10: 4, 0x11: 8, 0x12: 8, 0x13: 16}


def _walk(data: bytes, pos: int, prefix: str, found: dict[str, set[int]]) -> int:
    length: int
    (length,) = struct.unpack_from("<i", data, pos)
    end = pos + length
    pos += 4
    while pos < end - 1:
        kind = data[pos]
        stop = data.index(b"\x00", pos + 1)
        name = data[pos + 1 : stop].decode("utf-8")
        pos = stop + 1
        path = prefix if prefix.endswith("[]") else f"{prefix}{name}"  # array elements share a path
        found.setdefault(path, set()).add(kind)
        if kind in _FIXED:
            pos += _FIXED[kind]
        elif kind == 0x02:
            (n,) = struct.unpack_from("<i", data, pos)
            pos += 4 + n
        elif kind == 0x05:
            (n,) = struct.unpack_from("<i", data, pos)
            pos += 5 + n
        elif kind == 0x03:
            pos = _walk(data, pos, path + ".", found)
        elif kind == 0x04:
            pos = _walk(data, pos, path + "[]", found)
        else:
            raise AssertionError(f"respcheck: BSON type {kind:#04x} at {path} is not handled")
    return end


def _empty(value: Any) -> bool:
    return value in (None, "", 0, False, [], {}, b"")


def _value_equal(expected: Any, actual: Any) -> bool:
    if isinstance(expected, bool) or isinstance(actual, bool):
        return expected is actual
    if isinstance(expected, int) and isinstance(actual, int):
        return expected == actual
    return bool(expected == actual)


def _compare(
    expected: Any, actual: Any, path: str, vary: set[str], additive: set[str], problems: list[str]
) -> None:
    if path in vary:
        return  # presence is checked by the caller; the type check is separate
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            problems.append(f"{path or '<root>'}: expected a document, got {actual!r}")
            return
        for key, want in expected.items():
            sub = f"{path}.{key}" if path else key
            if key in actual:
                _compare(want, actual[key], sub, vary, additive, problems)
            elif sub in vary or not _empty(want):
                problems.append(f"{sub}: missing (expected {want!r})")
        for key, got in actual.items():
            sub = f"{path}.{key}" if path else key
            if key not in expected and not (sub in additive or _empty(got)):
                problems.append(f"{sub}: not in the vector (got {got!r})")
        return
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            problems.append(f"{path}: expected {expected!r}, got {actual!r}")
            return
        for want, got in zip(expected, actual, strict=True):
            _compare(want, got, f"{path}[]", vary, additive, problems)
        return
    if not _value_equal(expected, actual):
        problems.append(f"{path}: expected {expected!r}, got {actual!r}")


def _unordered(value: Any) -> Any:
    """``canon`` of a value with every document's fields sorted: the server holds a document as a
    map and returns its fields in an order of its own (``wire.md`` section 3), so order is no part
    of what two answers must agree on."""
    if isinstance(value, dict):
        return {key: _unordered(value[key]) for key in sorted(value)}
    if isinstance(value, list | tuple):
        return [_unordered(entry) for entry in value]
    return value


def _fast_problems(wanted: bytes, payload: bytes) -> list[str]:
    """GET_FAST's answer is a status byte and then the stored document, so it is not a plain
    document: the status must be equal and the document is compared field by field."""
    if not payload:
        return [f"payload {payload!r} is empty, the vector has {wanted!r}"]
    if payload[:1] != wanted[:1]:
        return [f"status byte {payload[:1]!r} != {wanted[:1]!r}"]
    if len(wanted) == 1:  # a miss: the status byte alone, with no document behind it
        return [] if payload == wanted else [f"payload {payload!r} != {wanted!r}"]
    try:
        got = bsonvec.canon(_unordered(deserialize(payload[1:])))
    except Exception as failure:  # a driver test: whatever the decoder says is the problem
        return [f"the document after the status byte does not decode: {failure}"]
    want = bsonvec.canon(_unordered(deserialize(wanted[1:])))
    return [] if got == want else [f"document {payload[1:]!r} != {wanted[1:]!r}, field by field"]


def response_problems(
    vec: dict[str, Any], payload: bytes, rewrite: Rewrite | None = None
) -> list[str]:
    """Everything that disagrees between the real ``payload`` and the vector's response rules."""
    response = vec["response"]
    if "payload_json" not in response:
        wanted = bytes.fromhex(response.get("payload_hex", ""))
        if response.get("opcode_name") == "GET_FAST_RESPONSE" and wanted:
            return _fast_problems(wanted, payload)
        return [] if payload == wanted else [f"payload {payload!r} != {wanted!r}"]
    actual = deserialize(payload) if payload else {}
    expected = ejson.to_python(response["payload_json"])
    if rewrite is not None:
        expected = rewritten(expected, rewrite)
    vary = set(response.get("vary", []))
    additive = set(response.get("additive_fields", []))
    problems: list[str] = []
    names = {p.split(".")[0].split("[")[0] for p in response.get("fields", [])} | additive
    for key in actual:
        if key not in names:
            problems.append(f"{key}: a field the vector's `fields` does not list")
    for key in response.get("absent_fields", []):
        if key in actual:
            problems.append(f"{key}: listed in absent_fields but present")
    _compare(expected, actual, "", vary, additive, problems)
    want_types = (
        typemap(bytes.fromhex(response["payload_hex"])) if response.get("payload_hex") else {}
    )
    got_types = typemap(payload) if payload else {}
    for path in sorted(vary):
        if path in want_types:
            if path not in got_types:
                problems.append(f"{path}: varies, but is missing from the answer")
            elif got_types[path] != want_types[path]:
                problems.append(
                    f"{path}: BSON type {sorted(got_types[path])}, vector has "
                    f"{sorted(want_types[path])}"
                )
    return problems


def wild(expected: Any, vary: list[str], path: str = "") -> Any:
    """A copy of ``expected`` with every ``vary`` path replaced by :class:`Wild`."""
    if path in vary:
        return Wild()
    if isinstance(expected, dict):
        return {k: wild(v, vary, f"{path}.{k}" if path else k) for k, v in expected.items()}
    if isinstance(expected, list):
        return [wild(v, vary, f"{path}[]") for v in expected]
    return expected
