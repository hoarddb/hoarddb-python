# SPDX-License-Identifier: Apache-2.0
"""Python to BSON bytes (spec 7.2). Pure Python; one ``bytearray`` with back-patched lengths.

Everything unwritable is an :class:`~hoarddb.errors.InvalidArgumentError` naming the field
path (``outer.inner``, ``tags[2]``) and raised before a byte leaves the process.
"""

from __future__ import annotations

import struct
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from hoarddb import errors
from hoarddb._bson import decimal128
from hoarddb._bson.decode import MAX_DEPTH, body_problem
from hoarddb._bson.values import TYPE_BYTES, Opaque

_I32 = struct.Struct("<i").pack
_I64 = struct.Struct("<q").pack
_F64 = struct.Struct("<d").pack
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_MS: Final = timedelta(milliseconds=1)
_INT32: Final = range(-(2**31), 2**31)
_INT64: Final = range(-(2**63), 2**63)
_MAX_DOCUMENT: Final = 2**31 - 1


class _Unencodable(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.path: list[str | int] = []  # innermost first


def _too_long(what: str, size: int) -> str:
    return (
        f"holds {what} of {size} bytes, over the BSON limit of {_MAX_DOCUMENT} (a 32-bit length); "
        "split it across documents or store it outside the database"
    )


def _path(segments: list[str | int]) -> str:
    out = ""
    for part in reversed(segments):
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else part
    return out


def serialize(document: Mapping[str, Any]) -> bytes:
    if not isinstance(document, Mapping):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise errors.InvalidArgumentError(
            f"A document must be a mapping with string keys, not a {type(document).__name__}; "
            "convert it first. Nothing was sent.",
            operation="encode",
        )
    out = bytearray()
    try:
        _write_document(out, document, 1, set())
    except _Unencodable as bad:
        where = f'field "{_path(bad.path)}" ' if bad.path else ""
        raise errors.InvalidArgumentError(
            f"{where}{bad.reason}. Nothing was sent.".strip(), operation="encode"
        ) from None
    except RecursionError:
        raise errors.InvalidArgumentError(
            "The document is nested too deeply for the interpreter's stack to encode "
            "(or the call was already deep). Flatten the structure or call from a shallower "
            "stack. Nothing was sent.",
            operation="encode",
        ) from None
    return bytes(out)


def _enter(active: set[int], container: object) -> None:
    """Note that ``container`` is being written; a container met again while it is still open
    refers back to itself."""
    if id(container) in active:
        raise _Unencodable(
            "holds a document or list that contains itself (a cycle); break the cycle, a "
            "document must be a tree"
        )
    active.add(id(container))


def _write_document(
    out: bytearray, document: Mapping[str, Any], depth: int, active: set[int]
) -> None:
    if depth > MAX_DEPTH:
        raise _Unencodable(
            f"is nested deeper than {MAX_DEPTH} levels; the driver could not read it back, "
            "so it does not write it. Flatten the structure"
        )
    _enter(active, document)
    start = len(out)
    out += b"\x00\x00\x00\x00"
    for key, value in document.items():
        if not isinstance(key, str):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise _Unencodable(f"has the key {key!r}, which is not a string; keys must be strings")
        try:
            encoded_key = key.encode("utf-8")
        except UnicodeEncodeError:
            raise _Unencodable(f"has a key with a lone surrogate: {key!r}") from None
        if b"\x00" in encoded_key:
            raise _Unencodable(
                f"has the key {key!r}, which holds a NUL; a BSON key is a C string and cannot"
            )
        try:
            _write_element(out, encoded_key, value, depth, active)
        except _Unencodable as bad:
            bad.path.append(key)
            raise
    active.discard(id(document))
    out += b"\x00"
    length = len(out) - start
    if length > _MAX_DOCUMENT:
        raise _Unencodable(f"is {length} bytes, over the BSON limit of {_MAX_DOCUMENT}")
    out[start : start + 4] = _I32(length)


def _write_array(out: bytearray, items: Sequence[Any], depth: int, active: set[int]) -> None:
    if depth > MAX_DEPTH:
        raise _Unencodable(
            f"is nested deeper than {MAX_DEPTH} levels; the driver could not read it back, "
            "so it does not write it. Flatten the structure"
        )
    _enter(active, items)
    start = len(out)
    out += b"\x00\x00\x00\x00"
    for index, item in enumerate(items):
        try:
            _write_element(out, str(index).encode("ascii"), item, depth, active)
        except _Unencodable as bad:
            bad.path.append(index)
            raise
    active.discard(id(items))
    out += b"\x00"
    length = len(out) - start
    if length > _MAX_DOCUMENT:
        raise _Unencodable(_too_long("an array", length))
    out[start : start + 4] = _I32(length)


def _string_value(out: bytearray, text: str) -> None:
    try:
        raw = text.encode("utf-8")
    except UnicodeEncodeError as bad:
        raise _Unencodable(
            f"holds a lone surrogate (U+{ord(text[bad.start]):04X}), which is not valid UTF-8"
        ) from None
    if len(raw) + 1 > _MAX_DOCUMENT:
        raise _Unencodable(_too_long("a string", len(raw) + 1))
    out += _I32(len(raw) + 1)
    out += raw
    out += b"\x00"


def _write_element(out: bytearray, key: bytes, value: Any, depth: int, active: set[int]) -> None:
    kind = type(value)  # pyright: ignore[reportUnknownVariableType]
    if kind is str:
        out += b"\x02"
        out += key
        out += b"\x00"
        _string_value(out, value)
    elif kind is bool:
        out += b"\x08"
        out += key
        out += b"\x00\x01" if value else b"\x00\x00"
    elif kind is int:
        _write_int(out, key, value)
    elif kind is float:
        out += b"\x01"
        out += key
        out += b"\x00"
        out += _F64(value)
    elif value is None:
        out += b"\x0a"
        out += key
        out += b"\x00"
    elif kind is dict:
        out += b"\x03"
        out += key
        out += b"\x00"
        _write_document(out, value, depth + 1, active)
    elif kind is list or kind is tuple:
        out += b"\x04"
        out += key
        out += b"\x00"
        _write_array(out, value, depth + 1, active)
    elif kind is bytes:
        _write_binary(out, key, 0, value)
    elif kind is datetime:
        _write_datetime(out, key, value)
    elif kind is Decimal:
        out += b"\x13"
        out += key
        out += b"\x00"
        try:
            out += decimal128.encode(value)
        except decimal128.Decimal128Error as bad:
            raise _Unencodable(f"holds a Decimal that {bad}") from None
    elif kind is uuid.UUID:
        _write_binary(out, key, 4, value.bytes)
    elif kind is Opaque:
        _write_opaque(out, key, value, depth)
    else:
        _write_other(out, key, value, depth, active)


def _write_opaque(out: bytearray, key: bytes, value: Opaque, depth: int) -> None:
    bson_type: object = value.bson_type
    type_byte = TYPE_BYTES.get(bson_type) if isinstance(bson_type, str) else None  # pyright: ignore[reportUnnecessaryIsInstance]
    if type_byte is None:
        raise _Unencodable(f"holds an Opaque of unknown type {bson_type!r}")
    data: object = value.data
    if not isinstance(data, bytes):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise _Unencodable(
            f"holds an Opaque whose data is a {type(data).__name__}, not bytes; an Opaque is "
            "only meant to be a value read back from the server, passed on as it was read"
        )
    problem = body_problem(type_byte, data, depth)
    if problem is not None:
        raise _Unencodable(
            f"holds an Opaque of type {bson_type!r} whose data is not exactly one such value "
            f"({problem}); an Opaque is only meant to be a value read back from the server"
        )
    out.append(type_byte)
    out += key
    out += b"\x00"
    out += data


def _write_int(out: bytearray, key: bytes, value: int) -> None:
    if value in _INT32:
        out += b"\x10"
        out += key
        out += b"\x00"
        out += _I32(value)
    elif value in _INT64:
        out += b"\x12"
        out += key
        out += b"\x00"
        out += _I64(value)
    else:
        raise _Unencodable(
            f"holds {value}, outside the 64-bit range {_INT64.start} to {_INT64.stop - 1}; "
            "store it as a string or a Decimal"
        )


def _write_binary(out: bytearray, key: bytes, subtype: int, data: bytes) -> None:
    if len(data) > _MAX_DOCUMENT:
        raise _Unencodable(_too_long("a binary value", len(data)))
    out += b"\x05"
    out += key
    out += b"\x00"
    out += _I32(len(data))
    out.append(subtype)
    out += data


def _write_datetime(out: bytearray, key: bytes, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise _Unencodable(
            "holds a naive datetime, which would mean local time on one machine and UTC on "
            "another; pass datetime.now(timezone.utc), or "
            "datetime.fromisoformat(...).replace(tzinfo=timezone.utc) "
            "(datetime.utcnow() returns a naive value, so it is the usual way to hit this)"
        )
    out += b"\x09"
    out += key
    out += b"\x00"
    out += _I64((value - _EPOCH) // _MS)  # floor division: never truncation toward zero


def _write_other(out: bytearray, key: bytes, value: Any, depth: int, active: set[int]) -> None:
    """Subclasses (enums, ``OrderedDict``, ...) and the types that need an ``isinstance``."""
    if isinstance(value, bool):
        _write_element(out, key, bool(value), depth, active)
    elif isinstance(value, str):
        _write_element(out, key, str.__str__(value), depth, active)
    elif isinstance(value, int):
        _write_element(out, key, int(value), depth, active)
    elif isinstance(value, float):
        _write_element(out, key, float(value), depth, active)
    elif isinstance(value, bytes | bytearray | memoryview):
        _write_binary(out, key, 0, bytes(value))  # pyright: ignore[reportUnknownArgumentType]
    elif isinstance(value, datetime):
        _write_datetime(out, key, value)
    elif isinstance(value, date):
        raise _Unencodable(
            "holds a date; wrap it in a datetime "
            "(datetime.combine(d, time.min, tzinfo=timezone.utc))"
        )
    elif isinstance(value, Decimal):
        _write_element(out, key, Decimal(value), depth, active)
    elif isinstance(value, uuid.UUID):
        _write_binary(out, key, 4, value.bytes)
    elif isinstance(value, Mapping):
        out += b"\x03"
        out += key
        out += b"\x00"
        _write_document(out, value, depth + 1, active)  # pyright: ignore[reportUnknownArgumentType]
    elif isinstance(value, list | tuple):
        out += b"\x04"
        out += key
        out += b"\x00"
        _write_array(out, value, depth + 1, active)  # pyright: ignore[reportUnknownArgumentType]
    else:
        raise _Unencodable(
            f"holds a {type(value).__name__}, which has no BSON form; convert it first "
            "(the driver is not a serialisation layer)"
        )
