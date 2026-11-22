# SPDX-License-Identifier: Apache-2.0
"""An in-process fake hoardDB server for component tests: TLS, ALPN, frames, and a script.

It speaks the framing and the AUTH exchange, answers ``PING``, and hands every other frame to a
handler that returns *actions*: reply with a frame, close, reset, hang, sleep or write raw bytes.
It records every frame each connection received, so a test can assert what was (and was not) sent.
"""

from __future__ import annotations

import contextlib
import socket
import ssl
import struct
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import faketls
from hoarddb._bson import deserialize, serialize
from hoarddb._core import frame, opcodes


@dataclass
class Reply:
    opcode: int
    payload: bytes | dict[str, Any] = b""
    request_id: bytes | None = None  # None: echo the request's


@dataclass
class Close:
    pass


@dataclass
class Reset:
    """Close with a TCP reset (SO_LINGER 0)."""


@dataclass
class Hang:
    """Stop answering; keep the connection open until the test ends."""


@dataclass
class Sleep:
    seconds: float


@dataclass
class Raw:
    data: bytes


Action = Reply | Close | Reset | Hang | Sleep | Raw


@dataclass
class Conn:
    index: int
    sock: ssl.SSLSocket
    frames: list[frame.Frame] = field(default_factory=list[frame.Frame])
    auth_requests: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    closed: bool = False


Handler = Callable[[Conn, frame.Frame], Sequence[Action]]


def error(code: int, message: str = "", **extra: Any) -> Reply:
    return Reply(opcodes.ERROR, {"code": code, "message": message, **extra})


def auth_ok(
    token: str | None = "tok-1",
    *,
    expires_in: int = 900,
    deadline_in: int = 43200,
    capabilities: tuple[str, ...] = ("auth_token",),
    version: str = "0.0.0-test",
) -> Reply:
    doc: dict[str, Any] = {
        "success": True,
        "message": "ok",
        "protocol_version": 1,
        "server_version": version,
        "capabilities": list(capabilities),
        "password_min_length": 12,
    }
    if token is not None:
        now = int(time.time())
        doc.update(token=token, token_expires_at=now + expires_in, token_deadline=now + deadline_in)
    return Reply(opcodes.AUTH_OK, doc)


def auth_fail(reason: int, message: str = "") -> Reply:
    doc: dict[str, Any] = {"reason": reason}
    if message:
        doc["message"] = message
    return Reply(opcodes.AUTH_FAIL, doc)


def encode_frame(reply: Reply, request_id: bytes) -> bytes:
    payload = reply.payload if isinstance(reply.payload, bytes) else serialize(reply.payload)
    return frame.encode_frame(reply.opcode, reply.request_id or request_id, payload)


class FakeHoard:
    """``with FakeHoard(identity) as server:`` then dial ``127.0.0.1:server.port``."""

    def __init__(
        self,
        identity: faketls.Identity,
        *,
        handler: Handler | None = None,
        on_auth: Callable[[Conn, dict[str, Any]], Sequence[Action]] | None = None,
        greeting: Sequence[Action] | None = None,
        greeting_fn: Callable[[int], Sequence[Action] | None] | None = None,
        answer_ping: bool = True,
        alpn: tuple[str, ...] | None = ("hoarddb/1",),
    ) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(identity.cert_path), str(identity.key_path))
        if alpn is not None:
            context.set_alpn_protocols(list(alpn))
        self._context = context
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(64)
        self._listener.settimeout(0.05)
        self.port: int = self._listener.getsockname()[1]
        self.handler: Handler = handler or self._unknown
        self.on_auth: Callable[[Conn, dict[str, Any]], Sequence[Action]] = on_auth or (
            lambda conn, doc: [auth_ok()]
        )
        self.greeting = greeting
        self.greeting_fn = greeting_fn  # per connection index: actions, or None to serve it
        self.answer_ping = answer_ping
        self.connections: list[Conn] = []
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._accepter = threading.Thread(target=self._accept_loop, daemon=True)

    # -- lifecycle ---------------------------------------------------------------------------

    def __enter__(self) -> FakeHoard:
        self._accepter.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._accepter.join(timeout=5)
        for conn in list(self.connections):
            with contextlib.suppress(OSError):
                conn.sock.shutdown(socket.SHUT_RDWR)  # wakes a handler blocked in recv
            with contextlib.suppress(OSError):
                conn.sock.close()
        for t in self._threads:
            t.join(timeout=5)
        self._listener.close()

    # -- what a test reads -------------------------------------------------------------------

    @property
    def frames(self) -> list[frame.Frame]:
        with self._lock:
            return [f for c in self.connections for f in c.frames]

    def frames_with(self, opcode: int) -> list[frame.Frame]:
        return [f for f in self.frames if f.opcode == opcode]

    def wait_for(self, predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.005)
        return predicate()

    # -- the server --------------------------------------------------------------------------

    @staticmethod
    def _unknown(conn: Conn, request: frame.Frame) -> Sequence[Action]:
        name = opcodes.NAMES.get(request.opcode, hex(request.opcode))
        return [error(5, f"fake server: no handler for {name}")]

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                raw, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            t = threading.Thread(target=self._serve, args=(raw,), daemon=True)
            self._threads.append(t)
            t.start()

    def _serve(self, raw: socket.socket) -> None:
        raw.settimeout(10)
        try:
            tls = self._context.wrap_socket(raw, server_side=True)
        except (OSError, ssl.SSLError):
            raw.close()
            return
        with self._lock:
            conn = Conn(len(self.connections), tls)
            self.connections.append(conn)
        decoder = frame.FrameDecoder()
        try:
            greeting = self.greeting_fn(conn.index) if self.greeting_fn else self.greeting
            if greeting is not None:
                if self._act(conn, greeting, bytes(16)):
                    # a polite server reads what the client already sent before it closes; closing
                    # with unread bytes makes the operating system reset the connection
                    tls.settimeout(1.0)
                    with contextlib.suppress(OSError, ssl.SSLError):
                        tls.recv(65536)
                return
            while not self._stop.is_set():
                data = tls.recv(65536)
                if not data:
                    return
                for request in decoder.feed(data):
                    with self._lock:
                        conn.frames.append(request)
                    if not self._act(conn, self._answer(conn, request), request.request_id):
                        return
        except (OSError, ssl.SSLError, frame.FrameRefusal):
            return
        finally:
            conn.closed = True
            with contextlib.suppress(OSError):
                tls.close()

    def _answer(self, conn: Conn, request: frame.Frame) -> Sequence[Action]:
        if request.opcode == opcodes.AUTH:
            doc = deserialize(request.payload)
            conn.auth_requests.append(doc)
            return self.on_auth(conn, doc)
        if request.opcode == opcodes.PING and self.answer_ping:
            return [Reply(opcodes.PONG)]
        return self.handler(conn, request)

    def _act(self, conn: Conn, actions: Sequence[Action], request_id: bytes) -> bool:
        """Carry out the actions; False when the connection is to end."""
        for action in actions:
            if isinstance(action, Reply):
                conn.sock.sendall(encode_frame(action, request_id))
            elif isinstance(action, Raw):
                conn.sock.sendall(action.data)
            elif isinstance(action, Sleep):
                time.sleep(action.seconds)
            elif isinstance(action, Hang):
                self._stop.wait()
                return False
            elif isinstance(action, Reset):
                conn.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                return False
            else:
                return False
        return True


def start(tmp_path: Path, **kwargs: Any) -> tuple[FakeHoard, faketls.Identity]:
    """Mint an identity under ``tmp_path`` and build (not start) a server."""
    identity = faketls.make_identity(tmp_path, "hoard")
    return FakeHoard(identity, **kwargs), identity
