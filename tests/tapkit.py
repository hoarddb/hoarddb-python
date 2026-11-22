# SPDX-License-Identifier: Apache-2.0
"""A recording TLS proxy between a driver and a real server, and a raw frame client.

The driver is pinned to the proxy's own identity; the proxy dials the server and copies bytes in
both directions, decoding the frames it sees on the way. A conformance test can then compare what
the driver REALLY sent (the vector's request rules) and what the server REALLY answered (the
vector's response rules) without trusting either side's own account of it.
"""

from __future__ import annotations

import contextlib
import select
import socket
import ssl
import threading
from dataclasses import dataclass
from typing import Any

import faketls
from hoarddb._core import frame
from hoarddb._core.frame import Frame, FrameDecoder, FrameRefusal

ALPN = "hoarddb/1"
CLIENT_TO_SERVER = "c2s"
SERVER_TO_CLIENT = "s2c"


@dataclass(frozen=True)
class Seen:
    direction: str
    frame: Frame


def client_context() -> ssl.SSLContext:
    """A TLS client that skips CA verification: used where the test itself checks the key."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.set_alpn_protocols([ALPN])
    return context


class Tap:
    """``with Tap(identity, server_port) as tap:`` then dial ``127.0.0.1:tap.port``."""

    def __init__(self, identity: faketls.Identity, upstream_port: int) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(identity.cert_path), str(identity.key_path))
        context.set_alpn_protocols([ALPN])
        self._context = context
        self._upstream = upstream_port
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
        self._listener.settimeout(0.05)
        self.port: int = self._listener.getsockname()[1]
        self._seen: list[Seen] = []
        self.connections = 0  # client connections accepted so far
        self.closed = 0  # relays that have ended (either side closed)
        self.bytes_from_client = 0  # every byte the driver wrote, whole frames or not
        self.blackhole = threading.Event()  # set: the server's answers are read but not passed on
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._sockets: list[socket.socket] = []
        self._accepter = threading.Thread(target=self._accept_loop, daemon=True)

    def __enter__(self) -> Tap:
        self._accepter.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._accepter.join(timeout=5)
        with self._lock:
            sockets = list(self._sockets)
        for sock in sockets:
            _close(sock)
        for thread in list(self._threads):
            thread.join(timeout=5)
        self._listener.close()

    # -- what was seen -------------------------------------------------------------------------

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()

    def frames(self, direction: str | None = None) -> list[Frame]:
        with self._lock:
            return [s.frame for s in self._seen if direction in (None, s.direction)]

    def requests(self) -> list[Frame]:
        """Client frames that are not ``AUTH`` (the connection's own handshake)."""
        return [f for f in self.frames(CLIENT_TO_SERVER) if f.opcode != 0x01]

    def answer_to(self, request: Frame) -> Frame | None:
        for seen in self.frames(SERVER_TO_CLIENT):
            if seen.request_id == request.request_id:
                return seen
        return None

    # -- the proxy -----------------------------------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with self._lock:
                self.connections += 1
            thread = threading.Thread(target=self._serve, args=(conn,), daemon=True)
            self._threads.append(thread)
            thread.start()

    def _serve(self, conn: socket.socket) -> None:
        conn.settimeout(10)
        try:
            client_side = self._context.wrap_socket(conn, server_side=True)
            raw = socket.create_connection(("127.0.0.1", self._upstream), timeout=10)
            server_side = client_context().wrap_socket(raw, server_hostname="127.0.0.1")
        except (OSError, ssl.SSLError):
            _close(conn)
            return
        with self._lock:
            self._sockets += [client_side, server_side]
        self._relay(client_side, server_side)

    def _relay(self, client_side: ssl.SSLSocket, server_side: ssl.SSLSocket) -> None:
        """Copy both directions from ONE thread: an SSL object is not safe to read in one thread
        while another writes it, and a proxy that does so loses the occasional AUTH."""
        decoders = {client_side: FrameDecoder(), server_side: FrameDecoder()}
        other = {client_side: server_side, server_side: client_side}
        direction = {client_side: CLIENT_TO_SERVER, server_side: SERVER_TO_CLIENT}
        for sock in decoders:
            sock.setblocking(False)
        try:
            while not self._stop.is_set():
                readable, _, _ = select.select([client_side, server_side], [], [], 0.2)
                for source in readable:
                    # Non-blocking, and read until the TLS object has nothing left: OpenSSL may
                    # already hold several records that select() on the socket will never announce.
                    while True:
                        try:
                            data = source.recv(65536)
                        except (ssl.SSLWantReadError, BlockingIOError):
                            break
                        if not data:
                            return
                        try:
                            done = decoders[source].feed(data)
                        except FrameRefusal:
                            done = []
                        with self._lock:
                            self._seen.extend(Seen(direction[source], f) for f in done)
                            if source is client_side:
                                self.bytes_from_client += len(data)
                        if source is server_side and self.blackhole.is_set():
                            pass  # a stalled path: the driver is left waiting for an answer
                        else:
                            _send_all(other[source], data)
        except (OSError, ssl.SSLError):
            pass
        finally:
            _close(client_side)
            _close(server_side)
            with self._lock:
                self.closed += 1


def _send_all(sock: ssl.SSLSocket, data: bytes) -> None:
    """``sendall`` on a non-blocking TLS socket: wait (bounded) for it to be writable."""
    view = memoryview(data)
    while view:
        try:
            sent = sock.send(view)
        except (ssl.SSLWantWriteError, BlockingIOError):
            select.select([], [sock], [], 30)
        except ssl.SSLWantReadError:
            select.select([sock], [], [], 30)
        else:
            view = view[sent:]


def _close(sock: socket.socket) -> None:
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(OSError):
        sock.close()


class RawConn:
    """A bare TLS connection to a server that speaks frames and nothing else (no AUTH of its own).

    For what the driver's public surface cannot send: a request before ``AUTH``, a raw payload,
    an oversize header, a bad version byte.
    """

    def __init__(self, port: int, *, timeout: float = 10.0) -> None:
        raw = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self.sock: ssl.SSLSocket = client_context().wrap_socket(raw, server_hostname="127.0.0.1")
        self.sock.settimeout(timeout)
        self.decoder = FrameDecoder()
        self._pending: list[Frame] = []
        self.alpn: str | None = self.sock.selected_alpn_protocol()
        self.peer_der: bytes = self.sock.getpeercert(binary_form=True) or b""

    def __enter__(self) -> RawConn:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        _close(self.sock)

    def send_bytes(self, data: bytes) -> None:
        self.sock.sendall(data)

    def send(self, opcode: int, request_id: bytes, payload: bytes) -> None:
        self.sock.sendall(frame.encode_frame(opcode, request_id, payload))

    def read_frame(self) -> Frame:
        """The next whole frame; ``EOFError`` if the server closed first."""
        while not self._pending:
            data = self.sock.recv(65536)
            if not data:
                raise EOFError("the server closed the connection before a whole frame arrived")
            self._pending.extend(self.decoder.feed(data))
        return self._pending.pop(0)

    def read_until_closed(self) -> bytes:
        """Everything the server sends until it closes (empty if it just closes)."""
        chunks: list[bytes] = []
        while True:
            try:
                data = self.sock.recv(65536)
            except (OSError, ssl.SSLError) as exc:
                if isinstance(exc, TimeoutError):
                    raise
                break
            if not data:
                break
            chunks.append(data)
        return b"".join(chunks)


def password_auth_payload(user: str, password: str, **extra: Any) -> dict[str, Any]:
    """The ``AUTH`` request document of auth.md section 1 for a password."""
    return {
        "username": user,
        "password": password,
        "protocol_version": 1,
        "capabilities": [],
        "client": "hoarddb-conformance/1",
        **extra,
    }
