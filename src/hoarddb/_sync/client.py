# SPDX-License-Identifier: Apache-2.0
"""The blocking client (spec 4.2 to 4.4): ``connect``, ``Client`` and ``Database``.

A ``Client`` is safe to share between threads. It owns one pool of two lanes, starts no thread and
no timer, and does not survive ``os.fork()``. ``Database`` and the bucket facets are handles: two
names and a reference to the client's shared context.
"""

from __future__ import annotations

import os
import warnings
import weakref
from collections.abc import Sequence
from typing import Any, Literal, Self, TypeVar, Unpack, overload

from hoarddb import errors
from hoarddb._core import config
from hoarddb._deadline import Deadline
from hoarddb._forkguard import register
from hoarddb._ops import client as clientops
from hoarddb._ops import database as dbops
from hoarddb._ops.refs import BucketRef
from hoarddb._sync.buckets import (
    BlobBucket,
    BTreeBucket,
    Bucket,
    FifoBucket,
    HashBucket,
    HeapBucket,
    LifoBucket,
    OpenedBucket,
)
from hoarddb._sync.context import Context
from hoarddb._sync.pool import Pool
from hoarddb._sync.run import Runner
from hoarddb.options import ConnectOptions
from hoarddb.types import (
    BucketInfo,
    DatabaseInfo,
    FieldTypes,
    Health,
    IndexSpec,
    NodeInfo,
    PoolStats,
    ServerInfo,
    StoreSpec,
    StoreType,
)

T = TypeVar("T")

_FACETS: dict[str, type[Any]] = {
    "hash": HashBucket,
    "btree": BTreeBucket,
    "fifo": FifoBucket,
    "lifo": LifoBucket,
    "heap": HeapBucket,
    "blob": BlobBucket,
}


def facet_for(ctx: Context, database: str, name: str, word: str) -> Any:
    """The facet class of a store type word; a word this driver does not know gets the generic
    ``Bucket``, which passes every refusal through."""
    return _FACETS.get(word, Bucket)(ctx, database, name)


class Database:
    """A database: a name and a reference to the client. No I/O, no resources, nothing cached."""

    __slots__ = ("_ctx", "_name")

    def __init__(self, ctx: Context, name: str) -> None:
        self._ctx = ctx
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def __repr__(self) -> str:
        return f"<Database {self._name}>"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Database) and self._ctx is other._ctx and self._name == other._name

    def __hash__(self) -> int:
        return hash((Database, id(self._ctx), self._name))

    @overload
    def create_bucket(
        self,
        name: str,
        type: Literal["hash", StoreType.HASH],
        *,
        if_not_exists: bool = False,
        encrypted: bool = False,
        replication_factor: int | None = None,
        stores: Sequence[StoreSpec] = (),
        indexes: Sequence[IndexSpec] = (),
        field_types: FieldTypes | None = None,
        timeout: float | None = None,
    ) -> HashBucket[dict[str, Any]]: ...

    @overload
    def create_bucket(
        self,
        name: str,
        type: Literal["btree", StoreType.BTREE],
        *,
        if_not_exists: bool = False,
        encrypted: bool = False,
        replication_factor: int | None = None,
        stores: Sequence[StoreSpec] = (),
        indexes: Sequence[IndexSpec] = (),
        field_types: FieldTypes | None = None,
        timeout: float | None = None,
    ) -> BTreeBucket[dict[str, Any]]: ...

    @overload
    def create_bucket(
        self,
        name: str,
        type: Literal["fifo", StoreType.FIFO],
        *,
        if_not_exists: bool = False,
        encrypted: bool = False,
        replication_factor: int | None = None,
        stores: Sequence[StoreSpec] = (),
        indexes: Sequence[IndexSpec] = (),
        field_types: FieldTypes | None = None,
        timeout: float | None = None,
    ) -> FifoBucket[dict[str, Any]]: ...

    @overload
    def create_bucket(
        self,
        name: str,
        type: Literal["lifo", StoreType.LIFO],
        *,
        if_not_exists: bool = False,
        encrypted: bool = False,
        replication_factor: int | None = None,
        stores: Sequence[StoreSpec] = (),
        indexes: Sequence[IndexSpec] = (),
        field_types: FieldTypes | None = None,
        timeout: float | None = None,
    ) -> LifoBucket[dict[str, Any]]: ...

    @overload
    def create_bucket(
        self,
        name: str,
        type: Literal["heap", StoreType.HEAP],
        *,
        if_not_exists: bool = False,
        encrypted: bool = False,
        replication_factor: int | None = None,
        stores: Sequence[StoreSpec] = (),
        indexes: Sequence[IndexSpec] = (),
        field_types: FieldTypes | None = None,
        timeout: float | None = None,
    ) -> HeapBucket[dict[str, Any]]: ...

    @overload
    def create_bucket(
        self,
        name: str,
        type: Literal["blob", StoreType.BLOB],
        *,
        if_not_exists: bool = False,
        encrypted: bool = False,
        replication_factor: int | None = None,
        stores: Sequence[StoreSpec] = (),
        indexes: Sequence[IndexSpec] = (),
        field_types: FieldTypes | None = None,
        timeout: float | None = None,
    ) -> BlobBucket: ...

    @overload
    def create_bucket(
        self,
        name: str,
        type: StoreType | str,
        *,
        if_not_exists: bool = False,
        encrypted: bool = False,
        replication_factor: int | None = None,
        stores: Sequence[StoreSpec] = (),
        indexes: Sequence[IndexSpec] = (),
        field_types: FieldTypes | None = None,
        timeout: float | None = None,
    ) -> OpenedBucket: ...

    def create_bucket(
        self,
        name: str,
        type: StoreType | str,  # noqa: A002 - the public keyword
        *,
        if_not_exists: bool = False,
        encrypted: bool = False,
        replication_factor: int | None = None,
        stores: Sequence[StoreSpec] = (),
        indexes: Sequence[IndexSpec] = (),
        field_types: FieldTypes | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Create a bucket of the given store type and return its facet.

        An existing name is AlreadyExistsError unless ``if_not_exists=True``, and always
        AlreadyExistsError when the existing bucket has another type. With ``if_not_exists=True``
        an existing bucket of the same type is returned and none of the requested declaration
        (``stores``, ``indexes``, ``field_types``, ``encrypted``, ``replication_factor``) is
        applied; so that you are not left believing otherwise, the driver then makes one info()
        call and raises ConflictError naming the requested store fields the bucket does not
        declare (indexes are not compared: check them with stats()). Creating a bucket in a
        database that does not exist creates the database. ``replication_factor`` needs the
        server's ``bucket_rf`` capability (UnsupportedFeatureError otherwise, nothing sent)."""
        word = self._ctx.run(
            dbops.create_bucket(
                self._name,
                name,
                type,
                if_not_exists=if_not_exists,
                encrypted=encrypted,
                replication_factor=replication_factor,
                stores=stores,
                indexes=indexes,
                field_types=field_types,
                facts=self._ctx.facts,
                timeout=timeout,
            )
        )
        return facet_for(self._ctx, self._name, name, word)

    def drop_bucket(self, name: str, *, timeout: float | None = None) -> None:
        """Drop a bucket and its data, at once. A bucket that does not exist is NotFoundError."""
        self._ctx.run(dbops.drop_bucket(self._name, name, timeout=timeout))

    def list_buckets(self, *, timeout: float | None = None) -> list[BucketInfo]:
        """Every bucket of the database in one frame, with no paging: an operator call (at
        hundreds of thousands of buckets the answer approaches the frame limit). Compare
        Bucket.info(), one bucket's declaration, and Bucket.stats(), which counts every store
        element."""
        return self._ctx.run(dbops.list_buckets(self._name, timeout=timeout))

    def declare_store_fields(
        self, bucket: str, stores: Sequence[StoreSpec], *, timeout: float | None = None
    ) -> None:
        """Add store fields to an existing bucket. They cannot be removed."""
        self._ctx.run(dbops.declare_store_fields(self._name, bucket, stores, timeout=timeout))

    @overload
    def bucket(self, name: str) -> Bucket[dict[str, Any]]: ...

    @overload
    def bucket(self, name: str, *, doc_type: type[T]) -> Bucket[T]: ...

    def bucket(self, name: str, *, doc_type: type[Any] | None = None) -> Bucket[Any]:
        """The generic handle: every verb, with the server's refusals passed through. No I/O.
        ``doc_type`` is a typing aid only (a TypedDict or ``dict[str, Any]``); it is never
        inspected at runtime."""
        return Bucket(self._ctx, self._name, name)

    @overload
    def hash(self, name: str) -> HashBucket[dict[str, Any]]: ...

    @overload
    def hash(self, name: str, *, doc_type: type[T]) -> HashBucket[T]: ...

    def hash(self, name: str, *, doc_type: type[Any] | None = None) -> HashBucket[Any]:
        """A hash bucket's handle. No I/O; ``doc_type`` is a typing aid only."""
        return HashBucket(self._ctx, self._name, name)

    @overload
    def btree(self, name: str) -> BTreeBucket[dict[str, Any]]: ...

    @overload
    def btree(self, name: str, *, doc_type: type[T]) -> BTreeBucket[T]: ...

    def btree(self, name: str, *, doc_type: type[Any] | None = None) -> BTreeBucket[Any]:
        """A btree bucket's handle. No I/O; ``doc_type`` is a typing aid only."""
        return BTreeBucket(self._ctx, self._name, name)

    @overload
    def fifo(self, name: str) -> FifoBucket[dict[str, Any]]: ...

    @overload
    def fifo(self, name: str, *, doc_type: type[T]) -> FifoBucket[T]: ...

    def fifo(self, name: str, *, doc_type: type[Any] | None = None) -> FifoBucket[Any]:
        """A first-in, first-out queue's handle. No I/O; ``doc_type`` is a typing aid only."""
        return FifoBucket(self._ctx, self._name, name)

    @overload
    def lifo(self, name: str) -> LifoBucket[dict[str, Any]]: ...

    @overload
    def lifo(self, name: str, *, doc_type: type[T]) -> LifoBucket[T]: ...

    def lifo(self, name: str, *, doc_type: type[Any] | None = None) -> LifoBucket[Any]:
        """A last-in, first-out stack's handle. No I/O; ``doc_type`` is a typing aid only."""
        return LifoBucket(self._ctx, self._name, name)

    @overload
    def heap(self, name: str) -> HeapBucket[dict[str, Any]]: ...

    @overload
    def heap(self, name: str, *, doc_type: type[T]) -> HeapBucket[T]: ...

    def heap(self, name: str, *, doc_type: type[Any] | None = None) -> HeapBucket[Any]:
        """A priority queue's handle. No I/O; ``doc_type`` is a typing aid only."""
        return HeapBucket(self._ctx, self._name, name)

    def blob(self, name: str) -> BlobBucket:
        """A blob bucket's handle. No I/O."""
        return BlobBucket(self._ctx, self._name, name)

    def open(self, name: str, *, timeout: float | None = None) -> OpenedBucket:
        """The facet of the bucket's real store type, learned with one BUCKET_STATS round trip.
        A bucket that does not exist is NotFoundError."""
        word = self._ctx.run(dbops.open_bucket(BucketRef(self._name, name), timeout=timeout))
        opened: OpenedBucket = facet_for(self._ctx, self._name, name, word)
        return opened


def _finalize(pool: Pool, description: str) -> None:
    """Close a client nobody closed. A ``ResourceWarning`` first, as sockets and files do, so
    ``python -X dev`` finds the leak; it is a warning, not an exception, so this never raises."""
    try:
        if pool.stats().closed:
            return
        warnings.warn(
            f"unclosed hoarddb.Client {description}; call close() or use it in a with block",
            ResourceWarning,
            stacklevel=2,
            source=None,
        )
        pool.close(timeout=0.0)
    except BaseException:
        return


class Client:
    """One per program: a pool of connections, safe to share between threads.

    Use it as a context manager, or call ``close()``. It does not survive ``os.fork()``: create
    the client after the fork, in a gunicorn post_fork hook, or inside the worker.
    """

    __slots__ = ("__weakref__", "_ctx", "_finalizer")

    def __init__(self, options: config.ResolvedOptions, pool: Pool, runner: Runner) -> None:
        self._ctx = Context(options, pool, runner)
        self._finalizer = weakref.finalize(self, _finalize, pool, options.display_uri)
        register(self)

    # -- identity ----------------------------------------------------------------------------

    def mark_forked(self) -> None:
        """Called in a forked child by the at-fork hook (never by programs)."""
        self._ctx.forked = True
        self._ctx.pool.mark_forked()
        self._finalizer.detach()

    def __repr__(self) -> str:
        state = "closed" if self._ctx.closed else "open"
        return f"<hoarddb.Client {self._ctx.options.display_uri} {state}>"

    # -- handles (no I/O) --------------------------------------------------------------------

    def database(self, name: str | None = None) -> Database:
        """A database handle: no I/O. Without a name, the URI's default database; if the URI
        names none, ConfigError says which segment is missing."""
        self._ctx.guard()
        if name is None:
            name = self._ctx.options.database
            if name is None:
                raise errors.ConfigError(
                    "No database name was given and the URI has none. Pass a name, "
                    "client.database('app'), or connect with hoarddb://host/app.",
                    code="NO_DEFAULT_DATABASE",
                    operation="database",
                )
        return Database(self._ctx, name)

    def default_bucket(self) -> Bucket[dict[str, Any]]:
        """The URI's default database and bucket (``hoarddb://host/app/users``) as a generic
        bucket handle. No I/O. If the URI names no database or no bucket, ConfigError says which
        segment is missing."""
        self._ctx.guard()
        options = self._ctx.options
        if options.database is None or options.bucket is None:
            missing = "database and bucket" if options.database is None else "bucket"
            raise errors.ConfigError(
                f"The URI names no default {missing}. Connect with hoarddb://host/app/users, "
                "or use client.database('app').bucket('users').",
                code="NO_DEFAULT_BUCKET",
                operation="default_bucket",
            )
        return Bucket(self._ctx, options.database, options.bucket)

    # -- databases ---------------------------------------------------------------------------

    def create_database(
        self, name: str, *, if_not_exists: bool = False, timeout: float | None = None
    ) -> None:
        """Create a database. An existing name is AlreadyExistsError unless
        ``if_not_exists=True``."""
        self._ctx.run(clientops.create_database(name, if_not_exists=if_not_exists, timeout=timeout))

    def drop_database(self, name: str, *, timeout: float | None = None) -> None:
        """Drop a database and everything in it, at once. A database that does not exist is
        NotFoundError."""
        self._ctx.run(clientops.drop_database(name, timeout=timeout))

    def list_databases(self, *, timeout: float | None = None) -> list[DatabaseInfo]:
        """Every database and its bucket count."""
        return self._ctx.run(clientops.list_databases(timeout=timeout))

    # -- the server --------------------------------------------------------------------------

    def ping(self, *, timeout: float | None = None) -> None:
        """One PING/PONG round trip."""
        self._ctx.run(clientops.ping(timeout=timeout))

    def health(self, *, timeout: float | None = None) -> Health:
        """Display facts about the node that answered. A driver never decides from them."""
        return self._ctx.run(clientops.health(timeout=timeout))

    def list_nodes(self, *, timeout: float | None = None) -> list[NodeInfo]:
        """The cluster's nodes. Each ``addr`` is the node's cluster address, never a dial target."""
        return self._ctx.run(clientops.list_nodes(timeout=timeout))

    def server_info(self) -> ServerInfo:
        """Version, protocol, capabilities and ``password_min_length`` as the server said at
        AUTH. No round trip."""
        self._ctx.guard()
        facts = self._ctx.facts
        assert facts is not None  # connect() warmed the pool: the first AUTH has happened
        return ServerInfo(facts.version, 1, facts.capabilities, facts.password_min_length)

    def pool_stats(self) -> PoolStats:
        """Aggregate pool counters. Never per bucket: the pool is per client."""
        self._ctx.guard()
        return self._ctx.pool.stats()

    # -- lifetime ----------------------------------------------------------------------------

    def close(self) -> None:
        """Idempotent. Waits for in-flight calls up to 5 s, then closes every connection. After a
        fork only the inherited descriptors are dropped."""
        ctx = self._ctx
        if os.getpid() != ctx.pid and not ctx.forked:
            self.mark_forked()
        if ctx.closed:
            return
        ctx.closed = True
        self._finalizer.detach()
        ctx.pool.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def connect(uri: str | None = None, **options: Unpack[ConnectOptions]) -> Client:
    """Connect: resolve the options (an option overrides the URI, which overrides the
    environment), then open one connection to ``AUTH`` and learn the server's capabilities, so a
    wrong password or an untrusted key fails here. More connections open lazily, up to the pool's
    size. ``uri=None`` reads HOARDDB_URI, else ``hoarddb://localhost``."""
    resolved = config.resolve_options(uri, options, os.environ)
    pool = Pool(resolved)
    try:
        pool.warm(Deadline(resolved.request_timeout))
    except BaseException:
        pool.close(timeout=0.0)
        raise
    return Client(resolved, pool, Runner(pool, resolved.request_timeout))
