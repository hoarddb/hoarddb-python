# SPDX-License-Identifier: Apache-2.0
"""The blocking connection over ``socket`` and ``ssl`` (spec 5.3, 6.2, 8.3).

One exchange at a time (lockstep), as a request-ID table with a window of one. Every exit that did
not complete an exchange closes the socket: a reply left unread would be read as the answer to the
next caller's request. Deadlines are enforced by setting the socket timeout before every ``send``
and ``recv``. The TLS key check (``_trust.connect``) has finished before the first byte of ``AUTH``
is written.
"""

from __future__ import annotations

import os
import ssl

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


class Connection:
    """An authenticated (or, inside :meth:`open`, authenticating) connection to one host."""

    __slots__ = (
        "_busy",
        "_decoder",
        "_sock",
        "authenticated",
        "healthy",
        "host",
        "last_used",
        "server_version",
    )

    def __init__(self, sock: ssl.SSLSocket, host: HostPort) -> None:
        self._sock: ssl.SSLSocket = sock
        self._decoder = frame.FrameDecoder()
        self._busy = False
        self.host = host
        self.healthy = True
        self.authenticated = False
        self.last_used = monotonic()
        self.server_version = ""

    # -- dialling ----------------------------------------------------------------------------

    @classmethod
    def open(
        cls,
        host: HostPort,
        pin: str | None,
        options: ResolvedOptions,
        auth: AuthRequest,
        deadline: Deadline,
    ) -> tuple[Connection, AuthOkResponse]:
        """Dial, check the key, ``AUTH``; the connection is closed on any failure.

        Raises ``ConnectionFailedError`` for a refused, unreachable or lost dial, the trust errors
        of ``connect_pinned`` unchanged, ``AuthenticationError`` for ``AUTH_FAIL``, and the
        server's own error (``OverloadedError`` with a zero request ID when its cap is full).
        """
        dial = deadline.within(options.dial_timeout)
        left = dial.remaining()
        if left is not None and left <= 0:
            raise diagnostics.connection_failed(
                [str(host)], "the deadline passed before the dial began", "connect", "READ"
            )
        try:
            sock = connect.connect_pinned(
                host, pin, options.tls, timeout=left or options.dial_timeout, logger=options.logger
            )
        except errors.HoardDBError:
            raise  # a trust refusal or a failed handshake: already the driver's own error
        except OSError as exc:
            raise diagnostics.connection_failed(
                [str(host)], describe(exc), "connect", "READ"
            ) from None
        conn = cls(sock, host)
        try:
            tune_socket(sock)
            ok = conn._authenticate(auth, dial, options.username)
        except BaseException:
            conn.close()
            raise
        conn.authenticated = True
        conn.server_version = ok.server_version
        return conn, ok

    def _authenticate(self, auth: AuthRequest, deadline: Deadline, username: str) -> AuthOkResponse:
        host_port = str(self.host)
        rid = frame.new_request_id()
        try:
            # No local holds the encoded frame: it carries the password or token in clear, and a
            # local is visible to anything that renders a traceback with its locals.
            self._send(messages.request_frame(auth, rid), deadline)
            reply = self._read_frame(deadline)
        except Interrupted as lost:
            reply = self._salvage(lost)
        try:
            return handshake.read_answer(reply, rid, username=username, host_port=host_port)
        except errors.HoardDBError:
            self.healthy = False  # one password attempt per connection: never reuse
            raise

    def _salvage(self, lost: Interrupted) -> frame.Frame:
        """The reset trap (spec 8.3): the server may have written its refusal and closed, and the
        operating system may have reset the connection when our unread AUTH arrived. One
        non-blocking attempt to read a pending frame; else a ``ConnectionFailedError``."""
        if lost.cause == "lost":
            try:
                self._sock.settimeout(0.0)
                self._sock.setblocking(False)
                pending = self._decoder.next_buffer()
                n = self._sock.recv_into(pending)
                done = self._decoder.commit(n) if n else None
                if done is not None:
                    return done
            except (OSError, ssl.SSLError, frame.FrameRefusal):
                pass
        message = f"the connection was lost during AUTH ({lost.reason})"
        if lost.cause == "timeout":
            raise diagnostics.connection_failed(
                [str(self.host)], "no answer to AUTH before the dial timeout", "connect", "READ"
            ) from None
        err = diagnostics.connection_failed([str(self.host)], message, "connect", "READ")
        err.message += OVERLOAD_HINT
        raise err from None

    # -- the exchange ------------------------------------------------------------------------

    def _send(self, data: bytes, deadline: Deadline) -> None:
        try:
            left = deadline.remaining()
            if left is not None and left <= 0:
                raise Interrupted(
                    "timeout", sent=False, reason="the deadline passed before sending"
                )
            self._sock.settimeout(left)
            lost: Interrupted | None = None
            try:
                self._sock.sendall(data)
            except TimeoutError:
                lost = Interrupted("timeout", sent=True, reason="the send timed out")
            except (OSError, ssl.SSLError) as exc:
                lost = Interrupted("lost", sent=True, reason=describe(exc))
            if lost is not None:
                # Raised outside the except block on purpose: inside it, the stdlib's own
                # ``sendall`` frame (which holds the data, here the AUTH frame with the password)
                # would stay reachable through ``__context__``.
                raise lost
        finally:
            del data  # an AUTH frame holds the password in clear; keep it out of traceback locals

    def _read_frame(self, deadline: Deadline) -> frame.Frame:
        decoder = self._decoder
        sock = self._sock
        while True:
            try:
                view = decoder.next_buffer()
            except frame.FrameRefusal as refusal:
                raise diagnostics.protocol_error(str(self.host), refusal) from None
            left = deadline.remaining()
            if left is not None and left <= 0:
                raise Interrupted("timeout", sent=True, reason="no answer before the deadline")
            sock.settimeout(left)
            try:
                n = sock.recv_into(view)
            except TimeoutError:
                raise Interrupted(
                    "timeout", sent=True, reason="no answer before the deadline"
                ) from None
            except (OSError, ssl.SSLError) as exc:
                raise Interrupted("lost", sent=True, reason=describe(exc)) from None
            if n == 0:
                where = "between frames" if decoder.at_boundary else "in the middle of a frame"
                raise Interrupted(
                    "lost", sent=True, reason=f"the server closed the connection {where}"
                )
            try:
                done = decoder.commit(n)
            except frame.FrameRefusal as refusal:
                raise diagnostics.protocol_error(str(self.host), refusal) from None
            if done is not None:
                return done

    def exchange(self, request: Request, ctx: failure.Context, deadline: Deadline) -> Reply:
        """One request and its answer.

        Raises ``Interrupted`` when no answer arrived (the connection is then closed), the
        server's error for an ``ERROR`` frame (the connection stays only if the code is on the
        survivor list), and ``ProtocolError`` for an answer that cannot be this request's.
        """
        if self._busy or not self.healthy:
            raise errors.InternalError(
                "Internal error: a connection was used while busy or discarded. "
                "Report it with the stack trace.",
                operation=ctx.operation,
            )
        rid = frame.new_request_id()
        try:
            data = messages.request_frame(request, rid)  # may refuse; touches nothing
        except frame.FrameTooLarge as too_large:
            raise diagnostics.frame_too_large(ctx.operation, too_large) from None
        self._busy = True
        done = False
        try:
            self._send(data, deadline)
            reply = self._read_frame(deadline)
            done = True  # the answer was read whole; _interpret closes if it cannot be trusted
            return self._interpret(reply, rid, request, ctx)
        except errors.ProtocolError as protocol:
            diagnostics.stamp_protocol_error(protocol, ctx)
            raise
        finally:
            self._busy = False
            if not done:
                self.close()

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

    def authenticate_again(
        self, auth: AuthRequest, deadline: Deadline, username: str
    ) -> AuthOkResponse:
        """A token ``AUTH`` on this authenticated connection (a refresh, or a re-authentication
        after ``NOT_AUTHENTICATED``). The connection is closed if it fails."""
        if self._busy or not self.healthy:
            raise errors.InternalError("Internal error: re-authenticating a busy connection.")
        self._busy = True
        done = False
        try:
            ok = self._authenticate(auth, deadline, username)
            done = True
            return ok
        finally:
            self._busy = False
            if not done:
                self.close()

    # -- liveness and shutdown ---------------------------------------------------------------

    def probe(self) -> bool:
        """True when the connection is idle and still looks alive: nothing readable (EOF, a reset or
        unsolicited bytes would be) and nothing buffered in the TLS object. Only for a connection
        that has completed ``AUTH``: right after the handshake the unread session tickets make
        every socket readable."""
        if not self.healthy or self._busy:
            return False
        try:
            if self._sock.pending() > 0:
                return False
            return not readable(self._sock.fileno())
        except (OSError, ValueError):
            return False

    def close(self, *, forked: bool = False) -> None:
        """Close the socket. After a ``fork`` the descriptor is not this process's to shut down:
        it is dropped (``shutdown`` would reset the parent's connection)."""
        self.healthy = False
        sock = self._sock
        try:
            if forked:
                fd = sock.detach()
                if fd >= 0:
                    os.close(fd)
            else:
                sock.close()
        except OSError:
            pass
