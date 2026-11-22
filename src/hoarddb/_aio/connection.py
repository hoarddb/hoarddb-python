# SPDX-License-Identifier: Apache-2.0
"""The asyncio connection (spec 5.3): a small ``asyncio.Protocol``, not a ``StreamReader``.

The same behaviour as :mod:`hoarddb._sync.connection`: the key check finishes before the first
``AUTH`` byte is written, one exchange at a time, a deadline over the whole exchange, and any exit
that did not finish the exchange (a timeout, an error, ``asyncio.CancelledError``) aborts the
transport. ``connection_lost`` is noticed at once, and bytes arriving on an idle connection are a
protocol violation, so the liveness probe is the protocol's own flags plus a look at whether the
descriptor is readable (nothing is read from it).
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import ssl
from collections import deque
from typing import Any

from hoarddb import errors
from hoarddb._bson import deserialize
from hoarddb._core import diagnostics, failure, frame, handshake, messages, opcodes, retry
from hoarddb._core.call import Reply
from hoarddb._core.config import ResolvedOptions
from hoarddb._core.interrupt import Interrupted
from hoarddb._core.messages import AuthOkResponse, AuthRequest, Request
from hoarddb._core.uri import HostPort
from hoarddb._deadline import Deadline, monotonic
from hoarddb._netutil import OVERLOAD_HINT, describe, readable, tune_socket
from hoarddb._trust import connect


class _Protocol(asyncio.Protocol):
    """Decodes frames straight from ``data_received`` and remembers how the connection ended."""

    def __init__(self) -> None:
        self.transport: asyncio.Transport | None = None
        self.decoder = frame.FrameDecoder()
        self.frames: deque[frame.Frame] = deque()
        self.refusal: frame.FrameRefusal | None = None
        self.lost: str | None = None  # why the connection ended, once it has
        self.eof = False
        self._waiter: asyncio.Future[None] | None = None
        self._can_write = asyncio.Event()
        self._can_write.set()

    # -- asyncio.Protocol ----------------------------------------------------------------------

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def data_received(self, data: bytes) -> None:
        if self.refusal is not None:
            return
        try:
            self.frames.extend(self.decoder.feed(data))
        except frame.FrameRefusal as refusal:
            self.refusal = refusal
        else:
            # Frames before a refused byte arrive without the exception; the refusal is stored.
            self.refusal = self.decoder.refusal
        self._wake()

    def eof_received(self) -> bool | None:
        self.eof = True
        self._wake()
        return False  # close the transport

    def connection_lost(self, exc: Exception | None) -> None:
        self.lost = describe(exc) if exc is not None else "the server closed the connection"
        self._can_write.set()
        self._wake()

    def pause_writing(self) -> None:
        self._can_write.clear()

    def resume_writing(self) -> None:
        self._can_write.set()

    # -- for the connection --------------------------------------------------------------------

    def _wake(self) -> None:
        waiter = self._waiter
        if waiter is not None and not waiter.done():
            waiter.set_result(None)

    async def wait_for_change(self) -> None:
        waiter = asyncio.get_running_loop().create_future()
        self._waiter = waiter
        try:
            await waiter
        finally:
            self._waiter = None

    async def drain(self) -> None:
        await self._can_write.wait()


class Connection:
    """An authenticated (or, inside :meth:`open`, authenticating) connection to one host."""

    __slots__ = (
        "_busy",
        "_logger",
        "_loop",
        "_protocol",
        "_transport",
        "authenticated",
        "healthy",
        "host",
        "last_used",
        "server_version",
    )

    def __init__(
        self,
        transport: asyncio.Transport,
        protocol: _Protocol,
        host: HostPort,
        logger: Any,
    ) -> None:
        self._transport = transport
        self._protocol = protocol
        self._loop = asyncio.get_running_loop()
        self._logger = logger
        self._busy = False
        self.host = host
        self.healthy = True
        self.authenticated = False
        self.last_used = monotonic()
        self.server_version = ""

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._loop

    # -- dialling ----------------------------------------------------------------------------

    @classmethod
    async def open(
        cls,
        host: HostPort,
        pin: str | None,
        options: ResolvedOptions,
        auth: AuthRequest,
        deadline: Deadline,
    ) -> tuple[Connection, AuthOkResponse]:
        """Dial, check the key, ``AUTH``; the transport is aborted on any failure."""
        connect.check_options(options.tls, pin)
        dial = deadline.within(options.dial_timeout)
        left = dial.remaining()
        if left is not None and left <= 0:
            raise diagnostics.connection_failed(
                [str(host)], "the deadline passed before the dial began", "connect", "READ"
            )
        loop = asyncio.get_running_loop()
        transport: asyncio.Transport | None = None
        try:
            async with asyncio.timeout(left):
                transport_, protocol = await loop.create_connection(
                    _Protocol,
                    host.host,
                    host.port,
                    ssl=connect.new_context(),
                    server_hostname="",
                )
                transport = transport_
                await cls._vet(transport, host, pin, options)
        except TimeoutError:
            if transport is not None:
                transport.abort()
            raise diagnostics.connection_failed(
                [str(host)], "timed out before the key check and AUTH finished", "connect", "READ"
            ) from None
        except ssl.SSLError as exc:
            raise connect.handshake_failed(host, exc) from None
        except errors.HoardDBError:
            if transport is not None:
                transport.abort()
            raise
        except OSError as exc:
            if transport is not None:
                transport.abort()
            raise diagnostics.connection_failed(
                [str(host)], describe(exc), "connect", "READ"
            ) from None
        except BaseException:
            if transport is not None:
                transport.abort()
            raise
        conn = cls(transport, protocol, host, options.logger)
        try:
            raw = transport.get_extra_info("socket")
            if raw is not None:
                tune_socket(raw)
            async with asyncio.timeout(dial.remaining()):
                ok = await conn._authenticate(auth, dial, options.username)
        except TimeoutError:
            conn.close()
            raise diagnostics.connection_failed(
                [str(host)], "no answer to AUTH before the dial timeout", "connect", "READ"
            ) from None
        except BaseException:
            conn.close()
            raise
        conn.authenticated = True
        conn.server_version = ok.server_version
        return conn, ok

    @staticmethod
    async def _vet(
        transport: asyncio.Transport, host: HostPort, pin: str | None, options: ResolvedOptions
    ) -> None:
        tls_object = transport.get_extra_info("ssl_object")
        unknown = connect.vet(tls_object, host, pin, options.tls, options.logger)
        if unknown is None:
            return
        callback = options.tls.on_unknown_server
        assert callback is not None
        answer: Any = callback(unknown.server_key())
        if inspect.isawaitable(answer):
            answer = await answer  # the async client accepts both forms
        connect.settle(unknown, answer, options.logger)

    async def _authenticate(
        self, auth: AuthRequest, deadline: Deadline, username: str
    ) -> AuthOkResponse:
        rid = frame.new_request_id()
        try:
            # No local holds the encoded frame: it carries the password or token in clear, and a
            # local is visible to anything that renders a traceback with its locals.
            await self._send(messages.request_frame(auth, rid))
            reply = await self._read_frame(deadline)
        except Interrupted as lost:
            reply = self._salvage(lost)
        try:
            return handshake.read_answer(reply, rid, username=username, host_port=str(self.host))
        except errors.HoardDBError:
            self.healthy = False  # one password attempt per connection: never reuse
            raise

    def _salvage(self, lost: Interrupted) -> frame.Frame:
        """The reset trap (spec 8.3): a refusal that arrived before the connection was lost is
        already decoded; use it, else report the likely cause."""
        if self._protocol.frames:
            return self._protocol.frames.popleft()
        err = diagnostics.connection_failed(
            [str(self.host)],
            f"the connection was lost during AUTH ({lost.reason})",
            "connect",
            "READ",
        )
        err.message += OVERLOAD_HINT
        raise err from None

    # -- the exchange ------------------------------------------------------------------------

    async def _send(self, data: bytes) -> None:
        protocol = self._protocol
        try:
            if protocol.lost is not None or self._transport.is_closing():
                raise Interrupted(
                    "lost", sent=False, reason=protocol.lost or "the connection is closed"
                )
            self._transport.write(data)
        finally:
            del data  # an AUTH frame holds the password in clear; keep it out of traceback locals
        await protocol.drain()
        if protocol.lost is not None:
            raise Interrupted("lost", sent=True, reason=protocol.lost)

    async def _read_frame(self, deadline: Deadline) -> frame.Frame:
        protocol = self._protocol
        while True:
            if protocol.frames:
                return protocol.frames.popleft()
            if protocol.refusal is not None:
                raise diagnostics.protocol_error(str(self.host), protocol.refusal)
            if protocol.lost is not None or protocol.eof:
                where = (
                    "in the middle of a frame"
                    if not protocol.decoder.at_boundary
                    else "between frames"
                )
                raise Interrupted(
                    "lost",
                    sent=True,
                    reason=f"{protocol.lost or 'the server closed the connection'} ({where})",
                )
            left = deadline.remaining()
            if left is not None and left <= 0:
                raise Interrupted("timeout", sent=True, reason="no answer before the deadline")
            try:
                async with asyncio.timeout(left):
                    await protocol.wait_for_change()
            except TimeoutError:
                raise Interrupted(
                    "timeout", sent=True, reason="no answer before the deadline"
                ) from None

    async def exchange(self, request: Request, ctx: failure.Context, deadline: Deadline) -> Reply:
        """One request and its answer (see the blocking connection for the contract).

        ``asyncio.CancelledError`` is never swallowed: the transport is aborted (the server cannot
        be told to stop) and the cancellation re-raised; for a write one WARNING says its outcome
        is unknown.
        """
        if self._busy or not self.healthy:
            raise errors.InternalError(
                "Internal error: a connection was used while busy or discarded. "
                "Report it with the stack trace.",
                operation=ctx.operation,
            )
        self._check_loop()
        rid = frame.new_request_id()
        try:
            data = messages.request_frame(request, rid)
        except frame.FrameTooLarge as too_large:
            raise diagnostics.frame_too_large(ctx.operation, too_large) from None
        left = deadline.remaining()
        if left is not None and left <= 0:
            raise Interrupted("timeout", sent=False, reason="the deadline passed before sending")
        self._busy = True
        done = False
        try:
            await self._send(data)
            reply = await self._read_frame(deadline)
            done = True
            return self._interpret(reply, rid, request, ctx)
        except errors.ProtocolError as protocol:
            diagnostics.stamp_protocol_error(protocol, ctx)
            raise
        except asyncio.CancelledError:
            if ctx.kind != "READ":
                self._logger.warning(
                    "The %s on %s was cancelled after it was sent: its outcome is unknown. "
                    "Wrap a call whose outcome you need in asyncio.shield().",
                    ctx.operation,
                    ctx.target or self.host,
                )
            raise
        finally:
            self._busy = False
            if not done:
                self.close()

    def _check_loop(self) -> None:
        running = asyncio.get_running_loop()
        if running is not self._loop:
            raise errors.ConfigError(
                f"This connection belongs to the event loop {self._loop!r} and is being used "
                f"from {running!r}. Create the client inside the loop that uses it.",
                code="WRONG_EVENT_LOOP",
                operation="call",
            )

    def _interpret(
        self, reply: frame.Frame, rid: bytes, request: Request, ctx: failure.Context
    ) -> Reply:
        host_port = str(self.host)
        if reply.request_id != rid:
            zero = failure.is_connection_level(reply.request_id)
            self.close()
            raise errors.ProtocolError(
                f"The server at {host_port} sent a frame with "
                + (
                    "a zero request ID on an authenticated connection"
                    if zero
                    else "a request ID that matches nothing in flight"
                )
                + ". The connection was discarded and the call was not resent.",
                operation=ctx.operation,
                target=host_port,
                write_outcome=None if ctx.kind == "READ" else "unknown",
            )
        if reply.opcode == opcodes.ERROR:
            try:
                payload = deserialize(reply.payload) if reply.payload else {}
            except errors.ProtocolError:
                self.close()  # an ERROR frame that cannot be read is not a connection to reuse
                raise
            err = failure.error_from_payload(payload, rid, ctx)
            if not retry.keeps_connection(err.wire_code or 0, zero_id=False):
                self.close()
            raise err
        try:
            response = messages.parse_response(type(request), reply)
        except errors.ProtocolError:
            self.close()
            raise
        self.last_used = monotonic()
        return Reply(response, rid)

    async def authenticate_again(
        self, auth: AuthRequest, deadline: Deadline, username: str
    ) -> AuthOkResponse:
        """A token ``AUTH`` on this authenticated connection; closed if it fails."""
        if self._busy or not self.healthy:
            raise errors.InternalError("Internal error: re-authenticating a busy connection.")
        self._busy = True
        done = False
        try:
            async with asyncio.timeout(deadline.remaining()):
                ok = await self._authenticate(auth, deadline, username)
            done = True
            return ok
        except TimeoutError:
            raise Interrupted("timeout", sent=True, reason="no answer to AUTH") from None
        finally:
            self._busy = False
            if not done:
                self.close()

    # -- liveness and shutdown ---------------------------------------------------------------

    def probe(self) -> bool:
        """True when the connection is idle and still open: the protocol saw no close, no EOF, no
        refusal and no unsolicited frame (bytes on an idle connection are a violation), and the
        socket has nothing waiting in it."""
        p = self._protocol
        if not self.healthy or self._busy:
            return False
        flags_say_open = (
            p.lost is None
            and not p.eof
            and p.refusal is None
            and not p.frames
            and p.decoder.at_boundary
            and not self._transport.is_closing()
        )
        if not flags_say_open:
            return False
        # The flags change only when the loop runs. A close that arrived since it last did is
        # still unread in the socket; a descriptor that is readable on an idle connection means
        # EOF, a reset or unsolicited bytes, and any of them rules the connection out.
        sock = self._transport.get_extra_info("socket")
        return sock is None or not readable(sock.fileno())

    def close(self, *, forked: bool = False) -> None:
        """Abort the transport. After a ``fork`` only the inherited descriptor is dropped: the
        loop's selector is shared with the parent, so the transport is never closed through it
        (``shutdown`` would reset the parent's connection)."""
        self.healthy = False
        if forked:
            sock = self._transport.get_extra_info("socket")
            if sock is not None:
                with contextlib.suppress(OSError):
                    fd = sock.fileno()
                    null = os.open(os.devnull, os.O_RDONLY)
                    try:
                        os.dup2(null, fd)  # this process's reference to the socket is gone
                    finally:
                        os.close(null)
            return
        self._transport.abort()
