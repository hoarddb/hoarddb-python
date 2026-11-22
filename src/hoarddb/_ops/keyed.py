# SPDX-License-Identifier: Apache-2.0
"""Keyed verbs: ``put``, ``insert``, ``insert_many``, ``get``, ``exists``, ``delete``, ``update``.

A miss is a value (``None``, ``False``), not an exception: the wire says a miss is a successful
answer (errors.md section 3), and the driver matches the server's message so that nothing else is
mistaken for one. A missing *bucket or database* is ``NotFoundError``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from hoarddb import errors
from hoarddb._core import failure
from hoarddb._core.call import Op
from hoarddb._core.messages import (
    BatchPutRequest,
    BatchPutResponse,
    ConsistentGetRequest,
    DeleteRequest,
    ExistsRequest,
    ExistsResponse,
    GetRequest,
    PutRequest,
    UpdateRequest,
    WriteResponse,
)
from hoarddb._core.updates import update_fields
from hoarddb._core.validate import check_name
from hoarddb._ops.refs import BucketRef
from hoarddb._ops.support import call, context, payload, require_mapping, write_concern
from hoarddb.types import BatchResult, WriteResult

NONE_WRITTEN: Final = "none of it was written"


def write_result(r: WriteResponse) -> WriteResult:
    return WriteResult(r.id, r.seq or None, r.copies_confirmed, r.replication_factor)


def put(
    ref: BucketRef,
    key: str,
    doc: Any,
    *,
    wc: str | None,
    timeout: float | None,
) -> Op[WriteResult]:
    op = "put"
    check_name("key", key, operation=op)
    data = require_mapping(doc, "A document", operation=op)
    request = PutRequest(
        database=ref.database,
        bucket=ref.name,
        key=key,
        data=data,
        write_concern=write_concern(wc, None, operation=op),
    )
    c = call(op, ref.target, request, "WRITE", timeout)
    reply = yield c
    failure.check_success(payload(reply), reply.request_id, context(c))
    return write_result(reply.response)


def insert(ref: BucketRef, doc: Any, *, wc: str | None, timeout: float | None) -> Op[WriteResult]:
    """``PUT`` with an empty key: the server assigns the key (or takes the document's ``_id_``)."""
    op = "insert"
    data = require_mapping(doc, "A document", operation=op)
    request = PutRequest(
        database=ref.database,
        bucket=ref.name,
        key="",
        data=data,
        write_concern=write_concern(wc, None, operation=op),
    )
    c = call(op, ref.target, request, "WRITE", timeout)
    reply = yield c
    failure.check_success(payload(reply), reply.request_id, context(c))
    return write_result(reply.response)


class _Progress:
    """What the halves of one ``insert_many`` have written so far."""

    __slots__ = ("copies", "count", "ids", "replication")

    def __init__(self) -> None:
        self.ids: list[str] = []
        self.count = 0
        self.copies: int | None = None
        self.replication: int | None = None

    def add(self, r: BatchPutResponse) -> None:
        self.ids.extend(r.ids)
        self.count += r.count
        if r.copies_confirmed is not None:
            self.copies = (
                r.copies_confirmed if self.copies is None else min(self.copies, r.copies_confirmed)
            )
        if r.replication_factor is not None:
            self.replication = r.replication_factor


def insert_many(
    ref: BucketRef,
    docs: Sequence[Any],
    *,
    wc: str | None,
    timeout: float | None,
) -> Op[BatchResult]:
    """``BATCH_PUT`` on the bulk lane.

    A batch the server refuses whole (``none of it was written``) is halved and the halves are
    sent in order, while more than one document remains. That sentence is the only refusal that
    is safe to halve: any other may follow a partial write, and halving it is a duplicate write.
    If a later half fails, the error is raised with ``written_count`` and ``written_ids`` set to
    what the earlier halves wrote.
    """
    op = "insert_many"
    if isinstance(docs, Mapping | str | bytes) or not isinstance(docs, Sequence):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise errors.InvalidArgumentError(
            f"insert_many takes a list of documents, not a {type(docs).__name__}. "
            "Nothing was sent.",
            operation=op,
        )
    items = [require_mapping(d, f"Document {i}", operation=op) for i, d in enumerate(docs)]
    chosen = write_concern(wc, None, operation=op)
    progress = _Progress()
    if not items:
        return BatchResult()
    try:
        yield from _send_batch(ref, items, chosen, timeout, progress)
    except errors.HoardDBError as failed:
        if progress.count and failed.written_count is None:
            failed.written_count = progress.count
            failed.written_ids = list(progress.ids)
            failed.hint = (failed.hint + " " if failed.hint else "") + (
                f"{progress.count} documents were written before this failed: resend only "
                "the remainder, docs[written_count:], never the whole list."
            )
        raise
    return BatchResult(progress.ids, progress.count, progress.copies, progress.replication)


def _send_batch(
    ref: BucketRef,
    items: list[Mapping[str, Any]],
    wc: str,
    timeout: float | None,
    progress: _Progress,
) -> Op[None]:
    request = BatchPutRequest(database=ref.database, bucket=ref.name, data=items, write_concern=wc)
    c = call("insert_many", ref.target, request, "WRITE", timeout, lane="bulk")
    try:
        reply = yield c
        failure.check_success(payload(reply), reply.request_id, context(c))
    except errors.InvalidArgumentError as refused:
        if NONE_WRITTEN in refused.message and len(items) > 1:
            middle = len(items) // 2
            yield from _send_batch(ref, items[:middle], wc, timeout, progress)
            yield from _send_batch(ref, items[middle:], wc, timeout, progress)
            return
        raise
    progress.add(reply.response)


def get(
    ref: BucketRef, key: str, *, consistent: bool, timeout: float | None
) -> Op[dict[str, Any] | None]:
    op = "get"
    check_name("key", key, operation=op)
    request: GetRequest | ConsistentGetRequest = (
        ConsistentGetRequest(database=ref.database, bucket=ref.name, key=key)
        if consistent
        else GetRequest(database=ref.database, bucket=ref.name, key=key)
    )
    c = call(op, ref.target, request, "READ", timeout)
    reply = yield c
    if not failure.check_found(payload(reply), reply.request_id, context(c)):
        return None
    data: dict[str, Any] | None = reply.response.data
    return data


def exists(ref: BucketRef, key: str, *, timeout: float | None) -> Op[bool]:
    op = "exists"
    check_name("key", key, operation=op)
    c = call(
        op,
        ref.target,
        ExistsRequest(database=ref.database, bucket=ref.name, key=key),
        "READ",
        timeout,
    )
    reply = yield c
    r: ExistsResponse = reply.response
    return r.exists


def delete(ref: BucketRef, key: str, *, wc: str | None, timeout: float | None) -> Op[bool]:
    op = "delete"
    check_name("key", key, operation=op)
    request = DeleteRequest(
        database=ref.database,
        bucket=ref.name,
        key=key,
        write_concern=write_concern(wc, None, operation=op),
    )
    c = call(op, ref.target, request, "WRITE", timeout)
    reply = yield c
    return failure.check_deleted(payload(reply), reply.request_id, context(c))


def update(
    ref: BucketRef,
    key: str,
    ops: Any,
    *,
    wc: str | None,
    timeout: float | None,
) -> Op[dict[str, Any] | None]:
    op = "update"
    check_name("key", key, operation=op)
    fields = update_fields(ops, operation=op)
    request = UpdateRequest(
        database=ref.database,
        bucket=ref.name,
        key=key,
        write_concern=write_concern(wc, None, operation=op),
        **fields,
    )
    c = call(op, ref.target, request, "WRITE", timeout)
    reply = yield c
    if not failure.check_updated(payload(reply), reply.request_id, context(c)):
        return None
    after: dict[str, Any] | None = reply.response.doc
    return after
