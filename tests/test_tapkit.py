# SPDX-License-Identifier: Apache-2.0
"""The recording proxy has to be invisible: whatever a client sends reaches the server, however soon
or late after the handshake it is sent. Tested with no hoardDB server and no driver, against an echo
TLS server and a raw client (the proxy is the only thing under test)."""

from __future__ import annotations

import socket
import ssl
import threading
import time
from pathlib import Path

import pytest

import faketls
import tapkit

DIALS = 20
FIRST_BYTES_AFTER = 0.05  # seconds after the handshake: a client that does not write at once
ECHO_WITHIN = 2.0  # seconds; the driver's own dial timeout is 10 s, far too long to wait for here


class EchoTlsServer:
    """Completes the TLS 1.3 handshake, then echoes every byte back. With ``tickets`` it sends the
    post-handshake session tickets a real server sends, which carry no application data."""

    def __init__(self, identity: faketls.Identity, *, tickets: bool) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(identity.cert_path), str(identity.key_path))
        context.set_alpn_protocols([tapkit.ALPN])
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.num_tickets = 2 if tickets else 0
        self._context = context
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
        self._listener.settimeout(0.05)
        self.port: int = self._listener.getsockname()[1]
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._accepter = threading.Thread(target=self._accept_loop, daemon=True)

    def __enter__(self) -> EchoTlsServer:
        self._accepter.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._accepter.join(timeout=5)
        for thread in self._threads:
            thread.join(timeout=5)
        self._listener.close()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            thread = threading.Thread(target=self._echo, args=(conn,), daemon=True)
            self._threads.append(thread)
            thread.start()

    def _echo(self, conn: socket.socket) -> None:
        conn.settimeout(5)
        try:
            with self._context.wrap_socket(conn, server_side=True) as tls:
                while not self._stop.is_set():
                    data = tls.recv(65536)
                    if not data:
                        return
                    tls.sendall(data)
        except (OSError, ssl.SSLError):
            return


def dial_and_echo(port: int) -> bool:
    """Connect, wait a little after the handshake, send, and say whether the bytes came back."""
    raw = socket.create_connection(("127.0.0.1", port), timeout=ECHO_WITHIN)
    with tapkit.client_context().wrap_socket(raw, server_hostname="127.0.0.1") as tls:
        tls.settimeout(ECHO_WITHIN)
        time.sleep(FIRST_BYTES_AFTER)
        tls.sendall(b"first-bytes")
        try:
            return tls.recv(64) == b"first-bytes"
        except (TimeoutError, OSError, ssl.SSLError):
            return False


@pytest.mark.parametrize("tickets", [False, True], ids=["no-tickets", "session-tickets"])
def test_a_client_that_writes_after_the_handshake_is_forwarded(
    tmp_path: Path, tickets: bool
) -> None:
    echo_identity = faketls.make_identity(tmp_path, "echo")
    tap_identity = faketls.make_identity(tmp_path, "tap")
    with (
        EchoTlsServer(echo_identity, tickets=tickets) as echo,
        tapkit.Tap(tap_identity, echo.port) as tap,
    ):
        echoed = sum(dial_and_echo(tap.port) for _ in range(DIALS))
    assert echoed == DIALS, (
        f"{DIALS - echoed} of {DIALS} dials got no echo through the proxy "
        f"(session tickets: {tickets})"
    )
