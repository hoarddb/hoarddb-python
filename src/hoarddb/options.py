# SPDX-License-Identifier: Apache-2.0
"""Connection options (spec 4.2)."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypedDict


@dataclass(frozen=True, slots=True)
class ServerKey:
    """A server's ed25519 key as presented in the handshake (passed to ``on_unknown_server``)."""

    host: str
    port: int
    fingerprint: str
    key: bytes


@dataclass(frozen=True, slots=True)
class TlsOptions:
    fingerprints: Sequence[str] = ()  # "SHA256:<64 hex>", positional per host; one applies to all
    known_servers_path: str | os.PathLike[str] | None = None  # None: ~/.hoarddb/known_servers
    use_known_servers: bool = True  # False: neither read nor write the file
    on_unknown_server: Callable[[ServerKey], bool] | None = None  # sync, or awaitable when async
    insecure_reason: str | None = None  # verification off; the reason is required and logged


@dataclass(frozen=True, slots=True)
class PoolOptions:
    size: int = 4  # point lane
    bulk_size: int = 2  # bulk lane
    max_waiters: int = 1000
    idle_timeout: float = 120.0  # seconds; a connection idle longer is closed at checkout


class ConnectOptions(TypedDict, total=False):
    hosts: Sequence[str | tuple[str, int]]
    username: str
    password: str
    auth_token: str
    database: str
    bucket: str
    tls: TlsOptions | Mapping[str, Any]
    pool: PoolOptions | Mapping[str, Any]
    dial_timeout: float
    request_timeout: float
    write_concern: Literal["one", "majority", "all"]
    client_name: str
    logger: logging.Logger
