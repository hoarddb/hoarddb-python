# SPDX-License-Identifier: Apache-2.0
"""What the blocking and the asyncio connection share and the sans-I/O core must not import."""

from __future__ import annotations

import select
import socket
import sys
from typing import Final

KEEPALIVE_IDLE: Final = 60  # seconds before the first TCP keepalive probe

OVERLOAD_HINT: Final = (
    " If a server answered, its connection cap may be full (HOARDDB_TCP_MAX_CONNS_PER_IP)."
)


def tune_socket(sock: socket.socket) -> None:
    """``TCP_NODELAY`` on, ``SO_KEEPALIVE`` on with the idle time set where the platform has it."""
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    option = getattr(socket, "TCP_KEEPIDLE", None)
    if option is None and sys.platform == "darwin":  # pragma: no cover
        option = getattr(socket, "TCP_KEEPALIVE", None)
    if option is not None:
        sock.setsockopt(socket.IPPROTO_TCP, option, KEEPALIVE_IDLE)


def describe(exc: BaseException) -> str:
    """A short reason for an I/O failure, for a message that must say what happened."""
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused"
    if isinstance(exc, TimeoutError):
        return "timed out"
    if isinstance(exc, socket.gaierror):
        return f"the name could not be resolved ({exc.strerror or exc})"
    text = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
    return text or type(exc).__name__


def readable(fd: int) -> bool:
    """True when the descriptor has something to read, hung up, or errored, without blocking.

    ``poll`` and not ``select``: ``select`` raises ``ValueError`` for a descriptor of 1024 or more,
    which a busy process reaches (spec 6.2 item 1).
    """
    if hasattr(select, "poll"):
        poller = select.poll()
        poller.register(fd, select.POLLIN | select.POLLPRI | select.POLLERR | select.POLLHUP)
        return bool(poller.poll(0))
    import selectors  # pragma: no cover - platforms without poll

    with selectors.DefaultSelector() as selector:  # pragma: no cover
        selector.register(fd, selectors.EVENT_READ)
        return bool(selector.select(0))
