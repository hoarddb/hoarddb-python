# SPDX-License-Identifier: Apache-2.0
"""A tiny, test-only BSON reader, written before the real codec exists (P2 replays the ``decode``
vectors, whose payloads are BSON, four steps ahead of P4). It reads only what those vectors use,
so it stays an independent check on the in-tree codec later."""

from __future__ import annotations

import struct
from typing import Any


def loads(data: bytes) -> dict[str, Any]:
    doc, end = _document(data, 0)
    assert end == len(data), "trailing bytes after the document"
    return doc


def _cstring(data: bytes, pos: int) -> tuple[str, int]:
    stop = data.index(b"\x00", pos)
    return data[pos:stop].decode("utf-8"), stop + 1


def _document(data: bytes, pos: int) -> tuple[dict[str, Any], int]:
    (length,) = struct.unpack_from("<i", data, pos)
    end = pos + length
    assert data[end - 1] == 0
    pos += 4
    out: dict[str, Any] = {}
    while pos < end - 1:
        kind = data[pos]
        name, pos = _cstring(data, pos + 1)
        value, pos = _value(kind, data, pos)
        out[name] = value
    return out, end


def _value(kind: int, data: bytes, pos: int) -> tuple[Any, int]:
    if kind == 0x01:
        return struct.unpack_from("<d", data, pos)[0], pos + 8
    if kind == 0x02:
        (n,) = struct.unpack_from("<i", data, pos)
        return data[pos + 4 : pos + 4 + n - 1].decode("utf-8"), pos + 4 + n
    if kind == 0x03:
        return _document(data, pos)
    if kind == 0x04:
        doc, end = _document(data, pos)
        return [doc[str(i)] for i in range(len(doc))], end
    if kind == 0x05:
        (n,) = struct.unpack_from("<i", data, pos)
        return bytes(data[pos + 5 : pos + 5 + n]), pos + 5 + n
    if kind == 0x07:
        return bytes(data[pos : pos + 12]), pos + 12
    if kind in (0x09, 0x11):
        return struct.unpack_from("<q", data, pos)[0], pos + 8
    if kind == 0x13:
        return bytes(data[pos : pos + 16]), pos + 16
    if kind == 0x08:
        return data[pos] == 1, pos + 1
    if kind == 0x0A:
        return None, pos
    if kind == 0x10:
        return struct.unpack_from("<i", data, pos)[0], pos + 4
    if kind == 0x12:
        return struct.unpack_from("<q", data, pos)[0], pos + 8
    raise AssertionError(f"minibson: unsupported element type {kind:#x}")
