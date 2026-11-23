# SPDX-License-Identifier: Apache-2.0
"""The bucket facets (spec 4.4 to 4.6): one class per store type, plus the generic ``Bucket``.

A facet is a handle: it holds the client's shared context and two names, does no I/O, caches
nothing and is registered nowhere, so a program may hold a million. A typed facet offers only the
verbs its store type answers (``FifoBucket`` has no ``get``); the generic ``Bucket`` offers every
verb and passes the server's refusal through, because the driver never looks a type up.

Each verb has a one-line body: it builds the shared operation and hands it to the client's runner.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Generic, Literal, TypeVar, cast, overload

from hoarddb._ops import blob as blobops
from hoarddb._ops import database as dbops
from hoarddb._ops import keyed as keyedops
from hoarddb._ops import pages
from hoarddb._ops import query as queryops
from hoarddb._ops import queue as queueops
from hoarddb._ops.docs import writes
from hoarddb._ops.refs import BucketRef
from hoarddb._sync.context import Context
from hoarddb._sync.cursor import Cursor
from hoarddb._sync.fields import FieldStore
from hoarddb.types import (
    BatchResult,
    Blob,
    BlobWriteResult,
    BucketDeclaration,
    BucketStats,
    Entry,
    Filter,
    UpdateOps,
    WriteConcern,
    WriteResult,
)

T = TypeVar("T")


class _Handle(Generic[T]):
    __slots__ = ("_ctx", "_db", "_name")

    def __init__(self, ctx: Context, database: str, name: str) -> None:
        self._ctx = ctx
        self._db = database
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def database(self) -> str:
        return self._db

    @property
    def _ref(self) -> BucketRef:
        return BucketRef(self._db, self._name)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self._db}/{self._name}>"

    def __eq__(self, other: object) -> bool:
        return (
            type(other) is type(self)
            and self._ctx is other._ctx  # pyright: ignore[reportAttributeAccessIssue]
            and (self._db, self._name) == (other._db, other._name)  # pyright: ignore
        )

    def __hash__(self) -> int:
        return hash((type(self), id(self._ctx), self._db, self._name))


class _Common(_Handle[T]):
    """What every store type answers."""

    __slots__ = ()

    def info(self, *, timeout: float | None = None) -> BucketDeclaration:
        """The bucket's declaration: store type, entry count and top-level store fields, in one
        round trip (BUCKET_INFO, 0xD9). Read-only and safe to retry. Its cost does not depend on
        how many elements the store fields hold: it never walks them (use stats() for element
        counts). A bucket or database that does not exist is NotFoundError; a caller without read
        permission is PermissionDeniedError."""
        return self._ctx.run(dbops.info(self._ref, timeout=timeout))

    def stats(self, *, timeout: float | None = None) -> BucketStats:
        """The bucket's counts, including every store field's element count. It walks every
        element, so it is not cheap on a bucket with large store fields; use info() for the
        declaration alone. A bucket that does not exist is NotFoundError."""
        return self._ctx.run(dbops.stats(self._ref, timeout=timeout))

    def drop(self, *, timeout: float | None = None) -> None:
        """Drop this bucket and its data, at once. A bucket that does not exist is
        NotFoundError. The handle stays valid (it is only two names); the next call on it
        finds nothing."""
        self._ctx.run(dbops.drop_bucket(self._db, self._name, timeout=timeout))


class _Put(_Handle[T]):
    __slots__ = ()

    @writes
    def put(
        self,
        key: str,
        doc: T,
        *,
        write_concern: WriteConcern | None = None,
        timeout: float | None = None,
    ) -> WriteResult:
        """Store ``doc`` under ``key`` as a new document. On a hash or btree bucket a put
        inserts and never replaces: a key that already holds a document is refused with
        DocumentExistsError and nothing is written, so use upsert() to replace it or update() to
        change some of its fields. The result carries the key (``id``), the sequence number and,
        on a cluster, how many copies were confirmed. Through the generic Bucket the other store
        types answer in their own way, with no error for a held key: a heap replaces the entry
        under that key, and a fifo or lifo pushes a second entry with the same ``_id_``. A blob
        bucket refuses put with InvalidArgumentError (use put_blob())."""
        wc = self._ctx.wc(write_concern)
        return self._ctx.run(keyedops.put(self._ref, key, doc, wc=wc, timeout=timeout))

    @writes
    def upsert(
        self,
        key: str,
        doc: T,
        *,
        write_concern: WriteConcern | None = None,
        timeout: float | None = None,
    ) -> WriteResult:
        """Store ``doc`` under ``key``, replacing the document that is there and creating it
        when there is none. The replaced document's own fields are replaced and its store
        fields are kept. Use put() to refuse a key that is taken instead. A bucket with no keys
        to replace (a fifo, lifo or heap) refuses it with InvalidArgumentError, and the server's
        sentence names the verb to use. The driver never sends an upsert twice: after a lost
        answer the outcome is unknown, and sending it again would overwrite any write that
        landed in between, so that decision is yours."""
        wc = self._ctx.wc(write_concern)
        return self._ctx.run(keyedops.upsert(self._ref, key, doc, wc=wc, timeout=timeout))


class _Insert(_Handle[T]):
    __slots__ = ()

    @writes
    def insert(
        self, doc: T, *, write_concern: WriteConcern | None = None, timeout: float | None = None
    ) -> WriteResult:
        """Store ``doc`` and let the server assign the key (or take the document's ``_id_``).
        The key is in the result's ``id``."""
        wc = self._ctx.wc(write_concern)
        return self._ctx.run(keyedops.insert(self._ref, doc, wc=wc, timeout=timeout))


class _Batch(_Handle[T]):
    __slots__ = ()

    @writes
    def insert_many(
        self,
        docs: Sequence[T],
        *,
        write_concern: WriteConcern | None = None,
        timeout: float | None = None,
    ) -> BatchResult:
        """Store many documents in one request, on the bulk lane.

        A batch the server refuses whole (BatchTooLargeError with nothing written) is halved and
        the halves are sent in order. That is the only refusal the driver halves on: any other may
        follow a partial write. If a later half fails, the error carries ``written_count`` and
        ``written_ids`` and its ``write_outcome`` is ``"unknown"``, never ``"not_applied"``: the
        first part IS stored, so resend only the remainder, ``docs[written_count:]``, never the
        whole list. A batch of one that is refused is
        raised as it is."""
        wc = self._ctx.wc(write_concern)
        return self._ctx.run(keyedops.insert_many(self._ref, docs, wc=wc, timeout=timeout))


class _Get(_Handle[T]):
    __slots__ = ()

    def get(self, key: str, *, consistent: bool = False, timeout: float | None = None) -> T | None:
        """The document under ``key``, or None when there is none (a miss is a value). A
        bucket or database that does not exist is NotFoundError. ``consistent=True`` reads every
        copy and returns the newest (CONSISTENT_GET): the read-your-writes verb on a cluster."""
        op = keyedops.get(self._ref, key, consistent=consistent, timeout=timeout)
        return cast("T | None", self._ctx.run(op))


class _Exists(_Handle[T]):
    __slots__ = ()

    def exists(self, key: str, *, timeout: float | None = None) -> bool:
        """True when ``key`` is stored. An empty key, or a bucket with no keys (a queue), is
        refused with InvalidArgumentError: a False always means the server looked."""
        return self._ctx.run(keyedops.exists(self._ref, key, timeout=timeout))


class _Delete(_Handle[T]):
    __slots__ = ()

    @writes
    def delete(
        self, key: str, *, write_concern: WriteConcern | None = None, timeout: float | None = None
    ) -> bool:
        """Delete the document (or blob) under ``key``. False means nothing was there. The
        driver does not retry a delete: it cannot know a newer write did not arrive. Deleting
        from a bucket whose type has no keys raises InvalidArgumentError with the server's
        message."""
        wc = self._ctx.wc(write_concern)
        return self._ctx.run(keyedops.delete(self._ref, key, wc=wc, timeout=timeout))


class _Update(_Handle[T]):
    __slots__ = ()

    @writes
    def update(
        self,
        key: str,
        ops: UpdateOps,
        *,
        write_concern: WriteConcern | None = None,
        timeout: float | None = None,
    ) -> T | None:
        """Apply operators to the document under ``key`` and return the document after. None
        means there is no such key. The nine operators are $set, $unset (a list of names), $inc
        (int64), $push, $addToSet, $mul (double), $min, $max and $rename; a key may be a path
        (``"Address.City"``, ``"Lines[0].Qty"``). The server applies them in that order and the
        update is all or nothing. An update is not checked against a typed declaration today."""
        wc = self._ctx.wc(write_concern)
        op = keyedops.update(self._ref, key, ops, wc=wc, timeout=timeout)
        return cast("T | None", self._ctx.run(op))


class _Queue(_Handle[T]):
    __slots__ = ()

    @writes
    def push(
        self,
        doc: T,
        *,
        priority: int | None = None,
        write_concern: WriteConcern | None = None,
        timeout: float | None = None,
    ) -> WriteResult:
        """Push ``doc``. On a heap, ``priority`` orders it (an int64; a heap push with none is
        stored at priority 0); a fifo or lifo ignores it. The priority is always sent, as int64,
        0 when you give none."""
        wc = self._ctx.wc(write_concern)
        op = queueops.push(self._ref, doc, priority=priority, wc=wc, timeout=timeout)
        return self._ctx.run(op)

    @writes
    def pop(
        self,
        *,
        limit: int | None = None,
        write_concern: WriteConcern | None = None,
        timeout: float | None = None,
    ) -> list[T]:
        """Take the next documents and return them in pop order: the oldest of a fifo, the
        newest of a lifo, the highest priority of a heap. One by default; ``limit`` asks for up
        to 100 in one request, and fewer than asked for means that was all there was. The answer
        is always a list, and an empty store is ``[]``, not an error. A pop removes what it
        returns, so it is a write: it carries a write concern, and it is never retried."""
        wc = self._ctx.wc(write_concern)
        op = queueops.pop(self._ref, limit=limit, wc=wc, timeout=timeout)
        return cast("list[T]", self._ctx.run(op))

    def peek(self, *, limit: int = 1, timeout: float | None = None) -> list[T]:
        """The next ``limit`` documents (1 to 1000) in pop order, removing none; [] when empty."""
        op = queueops.peek(self._ref, limit=limit, timeout=timeout)
        return cast("list[T]", self._ctx.run(op))


class _Blobs(_Handle[Any]):
    __slots__ = ()

    @writes
    def put_blob(
        self,
        key: str,
        data: bytes,
        *,
        metadata: Mapping[str, Any] | None = None,
        write_concern: WriteConcern | None = None,
        timeout: float | None = None,
    ) -> BlobWriteResult:
        """Store binary ``data`` under ``key`` with an optional metadata document, on the bulk
        lane. A blob over the server's blob limit is refused with its message."""
        wc = self._ctx.wc(write_concern)
        op = blobops.put_blob(self._ref, key, data, metadata=metadata, wc=wc, timeout=timeout)
        return self._ctx.run(op)

    def get_blob(self, key: str, *, timeout: float | None = None) -> Blob | None:
        """The blob under ``key`` with its metadata, or None when there is none."""
        return self._ctx.run(blobops.get_blob(self._ref, key, timeout=timeout))


class _Count(_Handle[T]):
    __slots__ = ()

    def count(self, filter: Filter | None = None, *, timeout: float | None = None) -> int:  # noqa: A002
        """How many documents match ``filter`` (all of them when there is none). A blob bucket
        refuses a filtered count."""
        return self._ctx.run(queryops.count(self._ref, filter, timeout=timeout))


class _Find(_Handle[T]):
    __slots__ = ()

    def find(
        self,
        filter: Filter | None = None,  # noqa: A002
        *,
        sort: Mapping[str, Literal[1, -1]] | None = None,
        projection: Mapping[str, Literal[0, 1]] | None = None,
        skip: int = 0,
        limit: int | None = None,
        page_size: int = 100,
        timeout: float | None = None,
    ) -> Cursor[T]:
        """A cursor over the documents matching ``filter``. **It is not a snapshot**: between
        pages a concurrent insert ahead of the boundary can show a document twice and a
        concurrent delete can hide one.

        There is no server-side cursor. Each page is an independent SCAN on the bulk lane (one
        round trip, nothing pinned), asked for lazily as you iterate, so abandoning a cursor is
        free. From page 2 on every SCAN carries the previous page's ``expect_bucket_id``, so a
        bucket dropped and re-created between pages is refused by the server (that error is
        raised, never retried as a fresh scan). A page the server finds too large
        (RESULT_TOO_LARGE) is asked again with half the page size, down to one document; a size
        that worked is kept; a single document that is still too large raises
        ResultTooLargeError with both numbers.

        ``sort`` has exactly one field (1 or -1). ``filter`` operators are hoardDB's own
        ($gt, $gte, $lt, $lte, $ne, $in, $nin, $exists, $regex, $and, $or); any other is passed
        through so the server's refusal names it. A bucket that does not exist is NotFoundError.
        ``cursor.total`` is the exact match count, known after the first page."""
        self._ctx.guard()
        pager = queryops.find_pager(
            self._ref,
            filter,
            sort=sort,
            projection=projection,
            skip=skip,
            limit=limit,
            page_size=page_size,
        )
        build = queryops.page_builder("find", self._ref, timeout)

        def fetch() -> list[T] | None:
            op = pages.next_page(pager, build, queryops.check_scan)
            return cast("list[T] | None", self._ctx.run(op))

        return Cursor(fetch, lambda: pager.total)

    def find_one(
        self,
        filter: Filter | None = None,  # noqa: A002
        *,
        timeout: float | None = None,
    ) -> T | None:
        """The first document matching ``filter``, or None."""
        return cast(
            "T | None", self._ctx.run(queryops.find_one(self._ref, filter, timeout=timeout))
        )

    def distinct(
        self,
        field: str,
        filter: Filter | None = None,  # noqa: A002
        *,
        timeout: float | None = None,
    ) -> list[Any]:
        """The distinct values of ``field`` among the documents matching ``filter``. A blob
        bucket refuses it."""
        return self._ctx.run(queryops.distinct(self._ref, field, filter, timeout=timeout))


class _Search(_Handle[T]):
    __slots__ = ()

    def search(
        self,
        filter: Filter,  # noqa: A002
        *,
        limit: int = 100,
        timeout: float | None = None,
    ) -> Cursor[T]:
        """Documents matching ``filter`` through a secondary index (hash buckets own indexes).
        One page: ``cursor.total`` is the number returned, not the number that match, and a
        search cut by ``limit`` cannot be paged; the answer is a larger ``limit``, or find()
        when the count matters. On a bucket with no index that serves the filter the server
        answers a full scan, as find() would; a blob bucket refuses it."""
        self._ctx.guard()
        pager = queryops.search_pager(self._ref, filter, limit=limit)
        build = queryops.page_builder("search", self._ref, timeout)

        def fetch() -> list[T] | None:
            return cast("list[T] | None", self._ctx.run(pages.next_page(pager, build)))

        return Cursor(fetch, lambda: pager.total)


class _BTree(_Handle[T]):
    __slots__ = ()

    def range(
        self,
        *,
        gte: str | None = None,
        lte: str | None = None,
        limit: int | None = None,
        page_size: int = 100,
        timeout: float | None = None,
    ) -> Cursor[Entry[T]]:
        """A cursor over the entries with ``gte <= key <= lte`` in key order (an absent side is
        open). Pages by key: the next page starts at the last key seen and the repeated first
        row is dropped, which is stable under concurrent inserts and is the recommended way to
        walk a bucket in order. One limit: range paging cannot detect a drop and re-create of
        the bucket (the BTREE_RANGE request has no ``expect_bucket_id``), so a walk over a
        bucket re-created mid-walk continues on the new bucket's keys. RESULT_TOO_LARGE halves
        the page, as for find()."""
        self._ctx.guard()
        pager = queryops.range_pager(self._ref, gte=gte, lte=lte, limit=limit, page_size=page_size)
        build = queryops.page_builder("range", self._ref, timeout)

        def fetch() -> list[Entry[T]] | None:
            rows = self._ctx.run(pages.next_page(pager, build))
            return None if rows is None else cast("list[Entry[T]]", queryops.entries(rows))

        return Cursor(fetch, lambda: None)

    def first(self, *, timeout: float | None = None) -> Entry[T] | None:
        """The entry with the smallest key, or None for an empty bucket."""
        return cast("Entry[T] | None", self._ctx.run(queryops.first(self._ref, timeout=timeout)))

    def last(self, *, timeout: float | None = None) -> Entry[T] | None:
        """The entry with the largest key, or None for an empty bucket."""
        return cast("Entry[T] | None", self._ctx.run(queryops.last(self._ref, timeout=timeout)))


E = TypeVar("E")


class _Fields(_Handle[T]):
    __slots__ = ()

    @overload
    def field(self, key: str, name: str) -> FieldStore[dict[str, Any]]: ...

    @overload
    def field(self, key: str, name: str, *, doc_type: type[E]) -> FieldStore[E]: ...

    def field(self, key: str, name: str, *, doc_type: type[Any] | None = None) -> FieldStore[Any]:
        """The store field ``name`` of the document ``key``: no I/O, nothing cached. A store
        field belongs to each document: ``users.field("ada", "Purchases").push(order)`` pushes
        onto Ada's own purchase queue. ``doc_type`` is a typing aid only."""
        self._ctx.guard()
        return FieldStore(self._ctx, self._ref, key, name)


class Bucket(
    _Put[T],
    _Insert[T],
    _Batch[T],
    _Get[T],
    _Exists[T],
    _Delete[T],
    _Update[T],
    _Queue[T],
    _Blobs,
    _Count[T],
    _Find[T],
    _Search[T],
    _BTree[T],
    _Fields[T],
    _Common[T],
):
    """Any bucket, with every verb. The server's refusal of a verb its store type does not answer
    reaches the caller verbatim; use a typed facet (``db.hash("users")``) to have a type checker
    refuse the mistake first."""

    __slots__ = ()


class HashBucket(
    _Put[T],
    _Insert[T],
    _Batch[T],
    _Get[T],
    _Exists[T],
    _Delete[T],
    _Update[T],
    _Count[T],
    _Find[T],
    _Search[T],
    _Fields[T],
    _Common[T],
):
    """A hash bucket: keyed documents, secondary indexes."""

    __slots__ = ()


class BTreeBucket(
    _Put[T],
    _Insert[T],
    _Batch[T],
    _Get[T],
    _Exists[T],
    _Delete[T],
    _Update[T],
    _Count[T],
    _Find[T],
    _BTree[T],
    _Fields[T],
    _Common[T],
):
    """A btree bucket: keyed documents in key order."""

    __slots__ = ()


class FifoBucket(_Queue[T], _Batch[T], _Count[T], _Find[T], _Common[T]):
    """A first-in, first-out queue."""

    __slots__ = ()


class LifoBucket(_Queue[T], _Batch[T], _Count[T], _Find[T], _Common[T]):
    """A last-in, first-out stack."""

    __slots__ = ()


class HeapBucket(_Queue[T], _Batch[T], _Count[T], _Find[T], _Common[T]):
    """A priority queue."""

    __slots__ = ()


class BlobBucket(_Exists[Any], _Delete[Any], _Blobs, _Count[Any], _Common[Any]):
    """A bucket of binary blobs with a metadata document each."""

    __slots__ = ()


OpenedBucket = (
    HashBucket[dict[str, Any]]
    | BTreeBucket[dict[str, Any]]
    | FifoBucket[dict[str, Any]]
    | LifoBucket[dict[str, Any]]
    | HeapBucket[dict[str, Any]]
    | BlobBucket
    | Bucket[dict[str, Any]]
)
