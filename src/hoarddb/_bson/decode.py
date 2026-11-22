# SPDX-License-Identifier: Apache-2.0
"""BSON bytes to Python (spec 7.2). Pure Python, written from the public BSON format.

Hard limits (each has a test): nesting past 100, invalid UTF-8 in a string or a key, a declared
length that is not exactly the bytes consumed, and an unknown element type are all
:class:`~hoarddb.errors.ProtocolError`; a datetime outside Python's range is an ``Opaque``. Nothing
is ever allocated from a declared length: every length is checked against the buffer first.
"""

from __future__ import annotations

import struct
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from hoarddb import errors
from hoarddb._bson import decimal128
from hoarddb._bson.values import Opaque

MAX_DEPTH: Final = 100

_I32 = struct.Struct("<i").unpack_from
_I64 = struct.Struct("<q").unpack_from
_F64 = struct.Struct("<d").unpack_from
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_MS: Final = timedelta(milliseconds=1)
_NO_BYTES: Final = b""


class _Malformed(Exception):
    pass


def deserialize(data: bytes | bytearray | memoryview) -> dict[str, Any]:
    buf = data if type(data) is bytes else bytes(data)
    size = len(buf)
    try:
        if size < 5:
            raise _Malformed("a document is at least 5 bytes" if size else "no bytes at all")
        (declared,) = _I32(buf, 0)
        if declared < 5:
            raise _Malformed(f"declared length {declared} is below the minimum of 5")
        if declared > size:
            raise _Malformed(f"declared length {declared} is more than the {size} bytes given")
        if declared < size:
            raise _Malformed(f"{size - declared} bytes follow the {declared}-byte document")
        if buf[size - 1] != 0:
            raise _Malformed("the document does not end in a 0 byte")
        return _document(buf, 4, size - 1, 1)
    except _Malformed as bad:
        raise errors.ProtocolError(
            f"The server sent a malformed BSON document: {bad}. The connection was discarded.",
            operation="receive",
        ) from None
    except (UnicodeDecodeError, struct.error, IndexError) as bad:
        raise errors.ProtocolError(
            f"The server sent a malformed BSON document ({type(bad).__name__}). "
            "The connection was discarded.",
            operation="receive",
        ) from None


def body_problem(kind: int, data: bytes, depth: int) -> str | None:
    """Read ``data`` as the body of one element of type ``kind``, with the reader a decode uses.

    None when the bytes are exactly one well-formed body; otherwise what is wrong with them. The
    encoder uses it to check an ``Opaque`` before writing it, so an ``Opaque`` cannot carry bytes
    the decoder would read as something else (a second element, say).
    """
    try:
        _, end = _value(kind, data, 0, len(data), depth)
    except _Malformed as bad:
        return str(bad)
    except (UnicodeDecodeError, struct.error, IndexError):
        return "the bytes are not a well-formed value of that type"
    if end != len(data):
        return f"{len(data) - end} extra bytes follow the value"
    return None


def _utf8(raw: bytes, what: str) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _Malformed(f"{what} is not valid UTF-8") from None


def _string_end(buf: bytes, pos: int, limit: int) -> int:
    """Validate a BSON string at ``pos`` (int32 length, bytes, NUL); return the index after it."""
    if pos + 4 > limit:
        raise _Malformed("element truncated before a string length")
    length: int = _I32(buf, pos)[0]
    end = pos + 4 + length
    if length < 1 or end > limit:
        raise _Malformed(f"string length {length} does not fit the document")
    if buf[end - 1] != 0:
        raise _Malformed("a string is not terminated by a 0 byte")
    _utf8(buf[pos + 4 : end - 1], "a string")
    return end


def _cstring_end(buf: bytes, pos: int, limit: int) -> int:
    stop = buf.find(b"\x00", pos, limit)
    if stop == -1:
        raise _Malformed("a C string is not terminated")
    _utf8(buf[pos:stop], "a C string")
    return stop + 1


def _document(buf: bytes, pos: int, limit: int, depth: int) -> dict[str, Any]:
    """Elements from ``pos`` up to the terminator at ``limit``; returns the dict."""
    if depth > MAX_DEPTH:
        raise _Malformed(f"nested deeper than {MAX_DEPTH} levels")
    out: dict[str, Any] = {}
    find = buf.find
    while pos < limit:
        kind = buf[pos]
        pos += 1
        stop = find(b"\x00", pos, limit)
        if stop == -1:
            raise _Malformed("an element key is not terminated")
        key = buf[pos:stop].decode("utf-8")
        pos = stop + 1
        value, pos = _value(kind, buf, pos, limit, depth)
        out[key] = value
    return out


def _array(buf: bytes, pos: int, limit: int, depth: int) -> list[Any]:
    if depth > MAX_DEPTH:
        raise _Malformed(f"nested deeper than {MAX_DEPTH} levels")
    out: list[Any] = []
    find = buf.find
    while pos < limit:
        kind = buf[pos]
        pos += 1
        stop = find(b"\x00", pos, limit)
        if stop == -1:
            raise _Malformed("an element key is not terminated")
        # An array element's key is its index, and a reader ignores it: wire order is the order.
        pos = stop + 1
        value, pos = _value(kind, buf, pos, limit, depth)
        out.append(value)
    return out


def _embedded(buf: bytes, pos: int, limit: int) -> int:
    """Check an embedded document's length prefix and terminator; return its end index."""
    if pos + 4 > limit:
        raise _Malformed("element truncated before a document length")
    length: int = _I32(buf, pos)[0]
    end = pos + length
    if length < 5 or end > limit:
        raise _Malformed(f"embedded document length {length} does not fit its parent")
    if buf[end - 1] != 0:
        raise _Malformed("an embedded document does not end in a 0 byte")
    return end


def _value(kind: int, buf: bytes, pos: int, limit: int, depth: int) -> tuple[Any, int]:
    if kind == 0x02:  # string
        if pos + 4 > limit:
            raise _Malformed("element truncated before a string length")
        (length,) = _I32(buf, pos)
        end = pos + 4 + length
        if length < 1 or end > limit:
            raise _Malformed(f"string length {length} does not fit the document")
        if buf[end - 1] != 0:
            raise _Malformed("a string is not terminated by a 0 byte")
        return buf[pos + 4 : end - 1].decode("utf-8"), end
    if kind == 0x10:  # int32
        if pos + 4 > limit:
            raise _Malformed("an int32 element is truncated")
        return _I32(buf, pos)[0], pos + 4
    if kind == 0x01:  # double
        if pos + 8 > limit:
            raise _Malformed("a double element is truncated")
        return _F64(buf, pos)[0], pos + 8
    if kind == 0x03:  # embedded document
        end = _embedded(buf, pos, limit)
        return _document(buf, pos + 4, end - 1, depth + 1), end
    if kind == 0x04:  # array
        end = _embedded(buf, pos, limit)
        return _array(buf, pos + 4, end - 1, depth + 1), end
    if kind == 0x08:  # bool
        if pos + 1 > limit:
            raise _Malformed("a boolean element is truncated")
        byte = buf[pos]
        if byte > 1:
            raise _Malformed(f"a boolean holds {byte}; only 0 and 1 are legal")
        return byte == 1, pos + 1
    if kind == 0x12:  # int64
        if pos + 8 > limit:
            raise _Malformed("an int64 element is truncated")
        return _I64(buf, pos)[0], pos + 8
    if kind == 0x0A:  # null
        return None, pos
    if kind == 0x09:  # datetime
        if pos + 8 > limit:
            raise _Malformed("a datetime element is truncated")
        millis = _I64(buf, pos)[0]
        try:
            return _EPOCH + millis * _MS, pos + 8
        except OverflowError:
            return Opaque("datetime", buf[pos : pos + 8]), pos + 8
    if kind == 0x05:  # binary
        return _binary(buf, pos, limit)
    if kind == 0x13:  # decimal128
        if pos + 16 > limit:
            raise _Malformed("a decimal128 element is truncated")
        return decimal128.decode(buf[pos : pos + 16]), pos + 16
    return _rare(kind, buf, pos, limit, depth)


def _binary(buf: bytes, pos: int, limit: int) -> tuple[Any, int]:
    if pos + 5 > limit:
        raise _Malformed("a binary element is truncated")
    (length,) = _I32(buf, pos)
    end = pos + 5 + length
    if length < 0 or end > limit:
        raise _Malformed(f"binary length {length} does not fit the document")
    subtype = buf[pos + 4]
    if subtype == 0:
        return buf[pos + 5 : end], end
    if subtype == 4 and length == 16:
        return uuid.UUID(bytes=buf[pos + 5 : end]), end
    if subtype == 2 and (length < 4 or _I32(buf, pos + 5)[0] != length - 4):
        raise _Malformed("an old-style binary (subtype 2) has an inner length that disagrees")
    return Opaque("binary", buf[pos:end]), end


def _fixed(name: str, size: int, buf: bytes, pos: int, limit: int) -> tuple[Opaque, int]:
    if pos + size > limit:
        raise _Malformed(f"a {name} element is truncated")
    return Opaque(name, buf[pos : pos + size]), pos + size


def _rare(kind: int, buf: bytes, pos: int, limit: int, depth: int) -> tuple[Any, int]:
    if kind == 0x07:
        return _fixed("objectid", 12, buf, pos, limit)
    if kind == 0x11:
        return _fixed("timestamp", 8, buf, pos, limit)
    if kind == 0x06:
        return Opaque("undefined", _NO_BYTES), pos
    if kind == 0xFF:
        return Opaque("minkey", _NO_BYTES), pos
    if kind == 0x7F:
        return Opaque("maxkey", _NO_BYTES), pos
    if kind == 0x0D:
        end = _string_end(buf, pos, limit)
        return Opaque("javascript", buf[pos:end]), end
    if kind == 0x0E:
        end = _string_end(buf, pos, limit)
        return Opaque("symbol", buf[pos:end]), end
    if kind == 0x0B:
        end = _cstring_end(buf, _cstring_end(buf, pos, limit), limit)
        return Opaque("regex", buf[pos:end]), end
    if kind == 0x0C:
        end = _string_end(buf, pos, limit)
        if end + 12 > limit:
            raise _Malformed("a DBPointer element is truncated")
        return Opaque("dbpointer", buf[pos : end + 12]), end + 12
    if kind == 0x0F:
        if pos + 4 > limit:
            raise _Malformed("a code-with-scope element is truncated")
        (total,) = _I32(buf, pos)
        end = pos + total
        if total < 14 or end > limit:
            raise _Malformed(f"code-with-scope length {total} does not fit the document")
        code_end = _string_end(buf, pos + 4, end)
        scope_end = _embedded(buf, code_end, end)
        if scope_end != end:
            raise _Malformed("a code-with-scope length disagrees with its parts")
        _document(buf, code_end + 4, scope_end - 1, depth + 1)  # validate; the bytes are kept
        return Opaque("javascript_with_scope", buf[pos:end]), end
    raise _Malformed(f"unknown element type {kind:#04x}")
