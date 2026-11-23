# SPDX-License-Identifier: Apache-2.0
"""Store-field verbs (spec 4.9; operations.md section 5).

A store field belongs to *each document*: ``users.field("ada", "Purchases")`` is Ada's own
queue. A :class:`FieldRef` is the parent bucket, the parent document's key, the field's name and,
for a store nested inside an element, the ``path`` of ``[outer field, outer element, ...]``. It is
a few strings: making one costs nothing and caches nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from hoarddb import errors
from hoarddb._core import failure, paging
from hoarddb._core.call import Call, Op
from hoarddb._core.messages import (
    FieldDeleteRequest,
    FieldGetRequest,
    FieldItem,
    FieldLenRequest,
    FieldPeekRequest,
    FieldPopRequest,
    FieldPushRequest,
    FieldPutRequest,
    FieldResponse,
    Request,
)
from hoarddb._core.validate import check_count, check_name
from hoarddb._ops.queue import (
    MAX_PEEK,
    check_pop_limit,
    check_priority,
    pop_answer,
    unreadable_pop,
)
from hoarddb._ops.refs import BucketRef
from hoarddb._ops.support import call, context, payload, require_mapping, write_concern
from hoarddb.types import FieldBlob, FieldEntry, FieldWriteResult

MAX_LIST_PAGE = 1000


@dataclass(frozen=True, slots=True)
class FieldRef:
    bucket: BucketRef
    key: str
    name: str
    path: tuple[str, ...] = ()  # [outer field, outer element, ...] for a nested store

    @property
    def target(self) -> str:
        outer = "".join(f".{p}" for p in self.path)
        return f"{self.bucket.target}/{self.key}{outer}.{self.name}"

    def common(self) -> dict[str, Any]:
        return {
            "database": self.bucket.database,
            "bucket": self.bucket.name,
            "key": self.key,
            "field": self.name,
            "path": list(self.path),
        }


def check_ref(ref: FieldRef, *, operation: str) -> None:
    check_name("document key", ref.key, operation=operation)
    for step in ref.path:
        check_name("nested path element", step, operation=operation)
    check_name("store field", ref.name, operation=operation)


def check_nested(element: object, name: object, *, operation: str) -> None:
    """The two names ``.field(element, name)`` adds to a path, checked where they are given so
    the refusal points at the line that passed them, not at a later verb."""
    check_name("element key", element, operation=operation)
    check_name("store field", name, operation=operation)


def _write(r: FieldResponse) -> FieldWriteResult:
    return FieldWriteResult(
        element_key=r.element_key,
        entry_id=r.entry_id,
        priority=r.priority or None,
        blob_id=r.blob_id,
        copies_confirmed=r.copies_confirmed,
        replication_factor=r.replication_factor,
    )


def entry(item: FieldResponse | FieldItem) -> FieldEntry[Any]:
    return FieldEntry(item.element, item.element_key, item.position, item.priority, item.entry_id)


def push(
    ref: FieldRef,
    doc: Any,
    *,
    priority: int | None,
    wc: str | None,
    timeout: float | None,
) -> Op[FieldWriteResult]:
    op = "field_push"
    check_ref(ref, operation=op)
    data = require_mapping(doc, "A document", operation=op)
    request = FieldPushRequest(
        **ref.common(),
        data=data,
        priority=check_priority(priority, operation=op),
        write_concern=write_concern(wc, None, operation=op),
    )
    c = call(op, ref.target, request, "WRITE", timeout)
    reply = yield c
    failure.check_success(payload(reply), reply.request_id, context(c))
    return _write(reply.response)


def pop(
    ref: FieldRef, *, limit: int | None, wc: str | None, timeout: float | None
) -> Op[list[FieldEntry[Any]]]:
    op = "field_pop"
    check_ref(ref, operation=op)
    request = FieldPopRequest(
        **ref.common(),
        limit=check_pop_limit(limit, operation=op),
        write_concern=write_concern(wc, None, operation=op),
    )
    c = call(op, ref.target, request, "WRITE", timeout)
    reply = yield from pop_answer(c, request.limit)
    r: FieldResponse = reply.response
    found = failure.check_found(payload(reply), reply.request_id, context(c))
    if found != bool(r.items):  # found with nothing to read, or entries beside "not found"
        raise unreadable_pop(c)
    return [entry(item) for item in r.items]


def peek(ref: FieldRef, *, limit: int, timeout: float | None) -> Op[list[FieldEntry[Any]]]:
    op = "field_peek"
    check_ref(ref, operation=op)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PEEK:  # pyright: ignore[reportUnnecessaryIsInstance]
        raise errors.InvalidArgumentError(
            f"peek limit must be a whole number from 1 to {MAX_PEEK}, not {limit!r}. "
            "Nothing was sent.",
            operation=op,
        )
    c = call(op, ref.target, FieldPeekRequest(**ref.common(), limit=limit), "READ", timeout)
    reply = yield c
    if not failure.check_found(payload(reply), reply.request_id, context(c)):
        return []
    return [entry(item) for item in reply.response.items]


def put(
    ref: FieldRef,
    element: str,
    doc: Any,
    *,
    wc: str | None,
    timeout: float | None,
) -> Op[FieldWriteResult]:
    op = "field_put"
    check_ref(ref, operation=op)
    check_name("element", element, operation=op)
    data = require_mapping(doc, "A document", operation=op)
    request = FieldPutRequest(
        **ref.common(),
        element=element,
        data=data,
        write_concern=write_concern(wc, None, operation=op),
    )
    c = call(op, ref.target, request, "WRITE", timeout)
    reply = yield c
    failure.check_success(payload(reply), reply.request_id, context(c))
    return _write(reply.response)


def get(ref: FieldRef, element: str, *, timeout: float | None) -> Op[FieldEntry[Any] | None]:
    op = "field_get"
    check_ref(ref, operation=op)
    check_name("element", element, operation=op)
    c = call(op, ref.target, FieldGetRequest(**ref.common(), element=element), "READ", timeout)
    reply = yield c
    if not failure.check_found(payload(reply), reply.request_id, context(c)):
        return None
    return entry(reply.response)


def delete(ref: FieldRef, element: str, *, wc: str | None, timeout: float | None) -> Op[bool]:
    op = "field_delete"
    check_ref(ref, operation=op)
    check_name("element", element, operation=op)
    request = FieldDeleteRequest(
        **ref.common(), element=element, write_concern=write_concern(wc, None, operation=op)
    )
    c = call(op, ref.target, request, "WRITE", timeout)
    reply = yield c
    failure.check_success(payload(reply), reply.request_id, context(c))
    return bool(reply.response.found)


def put_blob(
    ref: FieldRef,
    element: str,
    data: bytes,
    *,
    metadata: Mapping[str, Any] | None,
    wc: str | None,
    timeout: float | None,
) -> Op[FieldWriteResult]:
    op = "field_put_blob"
    check_ref(ref, operation=op)
    check_name("element", element, operation=op)
    if not isinstance(data, bytes | bytearray | memoryview):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise errors.InvalidArgumentError(
            f"A blob is bytes, not a {type(data).__name__}. Pass bytes(...). Nothing was sent.",
            operation=op,
        )
    meta = None if metadata is None else require_mapping(metadata, "metadata", operation=op)
    request = FieldPutRequest(
        **ref.common(),
        element=element,
        data=meta,
        content=bytes(data),
        write_concern=write_concern(wc, None, operation=op),
    )
    c = call(op, ref.target, request, "WRITE", timeout, lane="bulk")
    reply = yield c
    failure.check_success(payload(reply), reply.request_id, context(c))
    return _write(reply.response)


def get_blob(ref: FieldRef, element: str, *, timeout: float | None) -> Op[FieldBlob | None]:
    op = "field_get_blob"
    check_ref(ref, operation=op)
    check_name("element", element, operation=op)
    c = call(
        op,
        ref.target,
        FieldGetRequest(**ref.common(), element=element),
        "READ",
        timeout,
        lane="bulk",
    )
    reply = yield c
    if not failure.check_found(payload(reply), reply.request_id, context(c)):
        return None
    r: FieldResponse = reply.response
    return FieldBlob(r.element_key or element, r.blob_id, r.content, dict(r.element or {}))


def length(ref: FieldRef, *, timeout: float | None) -> Op[int]:
    op = "field_len"
    check_ref(ref, operation=op)
    c = call(op, ref.target, FieldLenRequest(**ref.common()), "READ", timeout)
    reply = yield c
    failure.check_success(payload(reply), reply.request_id, context(c))
    return int(reply.response.len)


def list_pager(ref: FieldRef, *, page_size: int, limit: int | None) -> paging.FieldListPager:
    op = "field_list"
    check_ref(ref, operation=op)
    if (
        isinstance(page_size, bool)
        or not isinstance(page_size, int)  # pyright: ignore[reportUnnecessaryIsInstance]
        or not 1 <= page_size <= MAX_LIST_PAGE
    ):
        raise errors.InvalidArgumentError(
            f"page_size must be a whole number from 1 to {MAX_LIST_PAGE}, not {page_size!r}. "
            "Nothing was sent.",
            operation=op,
        )
    if limit is not None:
        check_count("limit", limit, minimum=0, operation=op)
    return paging.FieldListPager(
        database=ref.bucket.database,
        bucket=ref.bucket.name,
        key=ref.key,
        field=ref.name,
        path=list(ref.path),
        page_size=page_size,
        limit=limit,
    )


def list_builder(ref: FieldRef, timeout: float | None) -> Any:
    """Pages of a list are independent requests on the bulk lane."""

    def build(request: Request) -> Call:
        return call("field_list", ref.target, request, "READ", timeout, lane="bulk")

    return build


def check_list(c: Call, reply: Any) -> None:
    """A page that did not succeed is a refusal with the server's message."""
    failure.check_success(payload(reply), reply.request_id, context(c))
