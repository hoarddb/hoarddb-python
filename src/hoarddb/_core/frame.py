# SPDX-License-Identifier: Apache-2.0
"""The frame codec (wire.md section 2).

A frame is ``Ver(1) | Length(4, little-endian) | Op(1) | RequestID(16) | Payload(N)`` where
``Length`` counts opcode + request ID + payload. The decoder validates ``Ver`` and ``Length`` from
the five-byte prefix *before* a body buffer exists, so a hostile length word allocates nothing.

This module raises two plain exceptions. The connection layer turns :class:`FrameRefusal` into a
``ProtocolError`` (it knows the host) and the operation layer turns :class:`FrameTooLarge` into a
``FrameTooLargeError`` (it knows the verb), so neither message is written here, half-informed.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from typing import Final, Literal, cast

from hoarddb import errors

VERSION: Final = 1
PREFIX_SIZE: Final = 5  # Ver + Length
HEADER_SIZE: Final = 22  # Ver + Length + Op + RequestID
REQUEST_ID_SIZE: Final = 16
MIN_LENGTH: Final = 1 + REQUEST_ID_SIZE
MAX_LENGTH: Final = 32 * 1024 * 1024

RefusalReason = Literal[
    "bad-version", "length-too-small", "length-too-large", "truncated", "trailing-bytes"
]

_PREFIX = struct.Struct("<BI")
_HEADER = struct.Struct("<BIB16s")


class FrameRefusal(Exception):
    """The bytes are not a frame this driver accepts. ``reason`` is the vectors' word for why."""

    def __init__(self, reason: RefusalReason, detail: str) -> None:
        super().__init__(detail)
        self.reason: RefusalReason = reason
        self.detail = detail


class FrameTooLarge(Exception):
    """An outgoing frame would exceed the 32 MiB limit; nothing was built or written."""

    def __init__(self, size: int, limit: int = MAX_LENGTH) -> None:
        super().__init__(f"frame of {size} bytes is over the {limit} limit")
        self.size = size
        self.limit = limit


@dataclass(frozen=True, slots=True)
class Frame:
    opcode: int
    request_id: bytes
    payload: bytes


def new_request_id() -> bytes:
    """Sixteen random bytes, unique among the requests in flight on a connection."""
    return os.urandom(REQUEST_ID_SIZE)


def encode_header(opcode: int, request_id: bytes, payload_length: int) -> bytes:
    """The 22-byte header for a payload of ``payload_length`` bytes.

    Refuses before building anything when the frame would pass 32 MiB, so a caller can send
    ``header`` and ``payload`` separately without ever joining a 30 MiB document to a header.
    """
    # The arguments are re-read as ``object``: a caller that ignores the annotations gets the
    # driver's error, never a struct.error or a frame no peer accepts.
    op = cast(object, opcode)
    rid = cast(object, request_id)
    size = cast(object, payload_length)
    if isinstance(op, bool) or not isinstance(op, int) or not 0 <= op <= 255:
        raise errors.InvalidArgumentError(
            f"opcode {op!r} is not a whole number from 0 to 255; pass one of the opcodes "
            "in hoarddb._core.opcodes",
            operation="encode",
        )
    if not isinstance(rid, bytes | bytearray) or len(rid) != REQUEST_ID_SIZE:
        raise errors.InvalidArgumentError(
            f"the request id must be {REQUEST_ID_SIZE} bytes; use new_request_id()",
            operation="encode",
        )
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise errors.InvalidArgumentError(
            f"the payload length {size!r} is not a size (a whole number of bytes, 0 or more); "
            "pass len(payload)",
            operation="encode",
        )
    length = MIN_LENGTH + payload_length
    if length > MAX_LENGTH:
        raise FrameTooLarge(length)
    return _HEADER.pack(VERSION, length, opcode, request_id)


def encode_frame(opcode: int, request_id: bytes, payload: bytes | bytearray | memoryview) -> bytes:
    return encode_header(opcode, request_id, len(payload)) + payload


def parse_prefix(prefix: bytes | bytearray | memoryview) -> int:
    """Validate the five-byte prefix and return ``Length`` (the body size to read)."""
    version: int
    length: int
    version, length = _PREFIX.unpack_from(prefix)
    if version != VERSION:
        raise FrameRefusal("bad-version", f"a frame version of {version}, expected {VERSION}")
    if length < MIN_LENGTH:
        raise FrameRefusal(
            "length-too-small", f"a frame length of {length}, under the minimum of {MIN_LENGTH}"
        )
    if length > MAX_LENGTH:
        raise FrameRefusal(
            "length-too-large", f"a frame length of {length}, over the {MAX_LENGTH} limit"
        )
    return length


def _frame_from_body(body: bytearray | memoryview) -> Frame:
    view = memoryview(body)
    return Frame(view[0], bytes(view[1:MIN_LENGTH]), bytes(view[MIN_LENGTH:]))


def decode_frame(data: bytes | bytearray | memoryview) -> Frame:
    """Decode exactly one whole frame (for fixtures and tests; connections use the decoder)."""
    if len(data) < PREFIX_SIZE:
        raise FrameRefusal("truncated", f"{len(data)} bytes is shorter than a frame prefix")
    length = parse_prefix(data)
    end = PREFIX_SIZE + length
    if len(data) < end:
        raise FrameRefusal("truncated", f"frame promises {length} bytes, {len(data) - 5} present")
    if len(data) > end:
        raise FrameRefusal("trailing-bytes", f"{len(data) - end} bytes follow the frame")
    return _frame_from_body(memoryview(data)[PREFIX_SIZE:end])


class FrameDecoder:
    """Incremental decoder. Two ways in, one state machine.

    ``feed(data)`` takes whatever arrived. ``next_buffer()`` / ``commit(n)`` let a socket
    ``recv_into`` the exact remaining bytes of the current stage (the prefix, then one pre-sized
    body buffer), so no ``recv(n)`` ever trusts an unchecked ``n``. After a refusal the decoder
    stays refused: the connection it belongs to is to be discarded.
    """

    __slots__ = ("_body", "_filled", "_prefix", "_refusal")

    def __init__(self) -> None:
        self._prefix = bytearray(PREFIX_SIZE)
        self._body: bytearray | None = None
        self._filled = 0
        self._refusal: FrameRefusal | None = None

    @property
    def at_boundary(self) -> bool:
        """True when no partial frame is buffered: EOF here is clean, anywhere else truncation."""
        return self._body is None and self._filled == 0

    def next_buffer(self) -> memoryview:
        if self._refusal is not None:
            raise self._refusal
        target = self._prefix if self._body is None else self._body
        return memoryview(target)[self._filled :]

    def commit(self, n: int) -> Frame | None:
        """``n`` bytes were written into the last ``next_buffer()``; return a frame if complete."""
        if self._refusal is not None:
            raise self._refusal
        target = self._prefix if self._body is None else self._body
        if not 0 <= n <= len(target) - self._filled:
            raise ValueError(f"committed {n} bytes into a buffer with {len(target) - self._filled}")
        self._filled += n
        if self._filled < len(target):
            return None
        if self._body is None:
            try:
                length = parse_prefix(self._prefix)
            except FrameRefusal as refusal:
                self._refusal = refusal
                raise
            self._body = bytearray(length)
            self._filled = 0
            return None
        done = _frame_from_body(self._body)
        self._body = None
        self._filled = 0
        return done

    @property
    def refusal(self) -> FrameRefusal | None:
        """The stored refusal, once the bytes fed so far included something refused."""
        return self._refusal

    def feed(self, data: bytes | bytearray | memoryview) -> list[Frame]:
        """Decode whatever arrived. Frames completed before a refused byte in the same chunk are
        returned, and the refusal is raised by the next call: a valid answer that arrives in the
        same read as garbage is still an answer."""
        if self._refusal is not None:
            raise self._refusal
        frames: list[Frame] = []
        source = memoryview(data)
        pos = 0
        while pos < len(source):
            view = self.next_buffer()
            n = min(len(view), len(source) - pos)
            view[:n] = source[pos : pos + n]
            pos += n
            try:
                done = self.commit(n)
            except FrameRefusal:
                if frames:
                    return frames  # the refusal is stored; the next call raises it
                raise
            if done is not None:
                frames.append(done)
        return frames
