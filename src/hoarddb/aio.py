# SPDX-License-Identifier: Apache-2.0
"""The asyncio API: the same names, arguments and results as ``hoarddb``, awaitable.

``hoarddb.aio.connect()`` is not a coroutine function: it returns an object that is awaitable and
an async context manager, and does no I/O until awaited.
"""

from hoarddb._aio.buckets import (
    BlobBucket,
    BTreeBucket,
    Bucket,
    FifoBucket,
    HashBucket,
    HeapBucket,
    LifoBucket,
    OpenedBucket,
)
from hoarddb._aio.client import AsyncConnect, Client, Database, connect
from hoarddb._aio.cursor import AsyncCursor
from hoarddb._aio.fields import FieldStore
from hoarddb._trust.policy import accept_first_sight
from hoarddb.options import ConnectOptions, PoolOptions, ServerKey, TlsOptions

AsyncClient = Client

__all__ = [
    "AsyncClient",
    "AsyncConnect",
    "AsyncCursor",
    "BTreeBucket",
    "BlobBucket",
    "Bucket",
    "Client",
    "ConnectOptions",
    "Database",
    "FieldStore",
    "FifoBucket",
    "HashBucket",
    "HeapBucket",
    "LifoBucket",
    "OpenedBucket",
    "PoolOptions",
    "ServerKey",
    "TlsOptions",
    "accept_first_sight",
    "connect",
]
