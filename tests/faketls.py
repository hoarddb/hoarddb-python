# SPDX-License-Identifier: Apache-2.0
"""An in-process fake hoardDB TLS server for component tests, and a way to mint ed25519 identities.

The server completes the TLS handshake and then records every application byte a client sends, so a
test can assert "zero bytes" after a refusal. Identities are made with the ``openssl`` command-line
tool, and the expected public key is read from openssl's own SPKI output, independently of the
driver's DER walker.
"""

from __future__ import annotations

import hashlib
import socket
import ssl
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Identity:
    cert_path: Path
    key_path: Path
    public_key: bytes  # the raw 32 bytes
    cert_der: bytes

    @property
    def fingerprint(self) -> str:
        return "SHA256:" + hashlib.sha256(self.public_key).hexdigest()


def _run(*args: str) -> bytes:
    return subprocess.run(args, check=True, capture_output=True).stdout


def make_identity(directory: Path, name: str = "node") -> Identity:
    key = directory / f"{name}.key"
    cert = directory / f"{name}.crt"
    _run("openssl", "genpkey", "-algorithm", "ED25519", "-out", str(key))
    _run(
        "openssl", "req", "-x509", "-new", "-key", str(key),
        "-subj", "/O=hoardDB/CN=hoarddb-node", "-days", "3650", "-out", str(cert),
    )  # fmt: skip
    spki = _run("openssl", "pkey", "-in", str(key), "-pubout", "-outform", "DER")
    der = _run("openssl", "x509", "-in", str(cert), "-outform", "DER")
    return Identity(cert, key, spki[-32:], der)


class FakeTlsServer:
    def __init__(
        self,
        identity: Identity,
        *,
        alpn: tuple[str, ...] | None = ("hoarddb/1",),
        max_version: ssl.TLSVersion | None = None,
    ) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(identity.cert_path), str(identity.key_path))
        if alpn is not None:
            context.set_alpn_protocols(list(alpn))
        if max_version is not None:
            context.maximum_version = max_version
        self._context = context
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
        self._listener.settimeout(0.05)
        self.port: int = self._listener.getsockname()[1]
        self.received: list[bytes] = []
        self.handshakes = 0
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._accepter = threading.Thread(target=self._accept_loop, daemon=True)

    def __enter__(self) -> FakeTlsServer:
        self._accepter.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._accepter.join(timeout=5)
        for t in self._threads:
            t.join(timeout=5)
        self._listener.close()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            t = threading.Thread(target=self._serve, args=(conn,), daemon=True)
            self._threads.append(t)
            t.start()

    def _serve(self, conn: socket.socket) -> None:
        conn.settimeout(5)
        try:
            with self._context.wrap_socket(conn, server_side=True) as tls:
                with self._lock:
                    self.handshakes += 1
                while True:
                    data = tls.recv(65536)
                    if not data:
                        return
                    with self._lock:
                        self.received.append(data)
        except (OSError, ssl.SSLError):
            return

    @property
    def bytes_received(self) -> int:
        with self._lock:
            return sum(len(b) for b in self.received)

    def settle(self) -> None:
        """Wait for every connection handler to finish (clients have closed by now)."""
        for t in list(self._threads):
            t.join(timeout=5)
