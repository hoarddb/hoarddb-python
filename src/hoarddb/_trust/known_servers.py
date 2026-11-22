# SPDX-License-Identifier: Apache-2.0
"""The shared trust store, ``~/.hoarddb/known_servers`` (trust.md section 4, spec 5.1 item 9).

One server per line: ``host:port <64 hex of the raw 32-byte key>``. ``#`` comments, blank lines and
malformed lines are skipped; two different keys for one host is an error at load. A file or
directory other users can write is refused, with the ``chmod`` to run. A new line is appended with a
single ``os.write`` on an ``O_APPEND`` descriptor, serialised in-process by a lock and between
Python processes by ``fcntl.flock`` where the platform has it.

This does not make the file safe against every writer: the command-line tool rewrites the whole
file without that lock, so a line appended here can be lost when it saves. That fails closed: a
lost line means the next connection asks again, never that an unknown key is trusted.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from hoarddb import errors
from hoarddb._core.uri import HostPort
from hoarddb._trust import fingerprint, policy

try:
    import fcntl
except ImportError:  # pragma: no cover - not POSIX
    fcntl = None  # type: ignore[assignment]

_HEX = frozenset("0123456789abcdefABCDEF")
_PROCESS_LOCK = threading.Lock()


def default_path() -> Path:
    return Path.home() / ".hoarddb" / "known_servers"


def _bad(message: str) -> errors.TlsTrustError:
    return errors.TlsTrustError(message, code="TLS_KNOWN_SERVERS", operation="connect")


def parse(text: str, where: str = "The known_servers file") -> dict[str, bytes]:
    """Parse the file's text into ``{"host:port": raw key}``."""
    entries: dict[str, bytes] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) < 2 or ":" not in parts[0]:
            continue
        digest = parts[1]
        if len(digest) != 64 or not set(digest) <= _HEX:
            continue  # text after the key (parts[2:]) is ignored
        key = bytes.fromhex(digest)
        known = entries.get(parts[0])
        if known is not None and known != key:
            raise _bad(
                f"{where} records two different keys for {parts[0]}. Delete the line for the "
                "key you do not trust."
            )
        entries[parts[0]] = key
    return entries


def _check_writable_by_others(path: Path) -> None:
    for target, fix in ((path.parent, "chmod 700"), (path, "chmod 600")):
        try:
            mode = target.stat().st_mode
        except FileNotFoundError:
            continue
        if mode & 0o022:
            kind = "directory" if target is path.parent else "file"
            raise _bad(
                f"The known_servers {kind} {target} can be written by other users, so a trust "
                f"decision read from it cannot be relied on. Run: {fix} {target}"
            )


class KnownServers:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    @classmethod
    def for_options(cls, path: str | os.PathLike[str] | None) -> KnownServers:
        return cls(default_path() if path is None else path)

    def load(self) -> dict[str, bytes]:
        _check_writable_by_others(self.path)
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return {}
        return parse(text, str(self.path))

    def lookup(self, host: HostPort) -> bytes | None:
        return self.load().get(str(host))

    def record(self, host: HostPort, key: bytes) -> None:
        """Append ``host`` and ``key``; a no-op if already recorded, an error if the file records
        another key for ``host``."""
        name = str(host)
        with _PROCESS_LOCK:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _check_writable_by_others(self.path)
            fd = os.open(self.path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX)  # released when the descriptor closes
                size = os.fstat(fd).st_size
                current = parse(
                    os.pread(fd, size, 0).decode("utf-8", errors="replace"), str(self.path)
                )
                existing = current.get(name)
                if existing == key:
                    return
                if existing is not None:
                    raise policy.key_changed(
                        host,
                        fingerprint.fingerprint_of_key(key),
                        fingerprint.fingerprint_of_key(existing),
                        self.path,
                    )
                prefix = b"\n" if size and os.pread(fd, 1, size - 1) != b"\n" else b""
                os.write(fd, prefix + f"{name} {key.hex()}\n".encode("ascii"))
            finally:
                os.close(fd)
