# SPDX-License-Identifier: Apache-2.0
"""Query verbs: ``count``, ``distinct``, ``find``, ``find_one``, ``search``, ``range``, ``first``,
``last`` (spec 4.6, 4.8).

``find``, ``search`` and ``range`` return cursors: the cursor classes live with each surface and
fetch one page at a time with :func:`hoarddb._ops.pages.next_page`. This module builds the pagers
and the page requests, and the single-round-trip verbs.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from hoarddb import errors
from hoarddb._core import failure, paging
from hoarddb._core.call import Call, Op, Reply
from hoarddb._core.messages import (
    BTreeFirstRequest,
    BTreeLastRequest,
    CountRequest,
    DistinctRequest,
    RangeEntry,
    RangeResponse,
    Request,
    ScanRequest,
    ScanResponse,
)
from hoarddb._core.validate import (
    check_count,
    check_filter,
    check_name,
    check_projection,
    check_sort,
)
from hoarddb._ops.refs import BucketRef
from hoarddb._ops.support import call, context, payload
from hoarddb.types import Entry

Builder = Callable[[Request], Call]


def count(ref: BucketRef, filter: Mapping[str, Any] | None, *, timeout: float | None) -> Op[int]:  # noqa: A002
    op = "count"
    check_filter(filter, operation=op)
    request = CountRequest(database=ref.database, bucket=ref.name, filter=filter)
    reply = yield call(op, ref.target, request, "READ", timeout)
    return int(reply.response.count)


def distinct(
    ref: BucketRef,
    field: str,
    filter: Mapping[str, Any] | None,  # noqa: A002
    *,
    timeout: float | None,
) -> Op[list[Any]]:
    op = "distinct"
    check_name("field", field, operation=op)
    check_filter(filter, operation=op)
    request = DistinctRequest(database=ref.database, bucket=ref.name, field=field, filter=filter)
    reply = yield call(op, ref.target, request, "READ", timeout, lane="bulk")
    return list(reply.response.values)


# --- find ----------------------------------------------------------------------------------------


def find_pager(
    ref: BucketRef,
    filter: Mapping[str, Any] | None,  # noqa: A002
    *,
    sort: Mapping[str, Any] | None,
    projection: Mapping[str, Any] | None,
    skip: int,
    limit: int | None,
    page_size: int,
) -> paging.FindPager:
    """Validate the arguments (before anything is sent, at the call that makes the cursor) and
    build the paging machine."""
    op = "find"
    check_filter(filter, operation=op)
    check_sort(sort, operation=op)
    check_projection(projection, operation=op)
    check_count("skip", skip, minimum=0, operation=op)
    if limit is not None:
        check_count("limit", limit, minimum=0, operation=op)
    check_count("page_size", page_size, minimum=1, operation=op)
    return paging.FindPager(
        database=ref.database,
        bucket=ref.name,
        filter=filter,
        sort=sort,
        projection=projection,
        skip=skip,
        limit=limit,
        page_size=page_size,
    )


def page_builder(op: str, ref: BucketRef, timeout: float | None) -> Builder:
    """Pages are independent requests on the bulk lane, one round trip each."""

    def build(request: Request) -> Call:
        return call(op, ref.target, request, "READ", timeout, lane="bulk")

    return build


def check_scan(c: Call, reply: Reply) -> None:
    """``found: false`` on a scan is the one in-band refusal of ``find``: a missing bucket is
    ``NotFoundError`` and a blob's refusal is ``InvalidArgumentError``, the sentence verbatim."""
    r: ScanResponse = reply.response
    if not r.found:
        failure.check_found(payload(reply), reply.request_id, context(c))


def find_one(
    ref: BucketRef,
    filter: Mapping[str, Any] | None,  # noqa: A002
    *,
    timeout: float | None,
) -> Op[dict[str, Any] | None]:
    """The first match, or None: one ``SCAN`` with ``limit`` 1."""
    op = "find_one"
    check_filter(filter, operation=op)
    request = ScanRequest(database=ref.database, bucket=ref.name, filter=filter, limit=1)
    c = call(op, ref.target, request, "READ", timeout, lane="bulk")
    reply = yield c
    check_scan(c, reply)
    docs: list[dict[str, Any]] = reply.response.documents
    return docs[0] if docs else None


# --- search --------------------------------------------------------------------------------------


def search_pager(
    ref: BucketRef,
    filter: Mapping[str, Any],  # noqa: A002
    *,
    limit: int,
) -> paging.SearchPager:
    op = "search"
    if not isinstance(filter, Mapping):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise errors.InvalidArgumentError(
            f"search takes a filter mapping such as {{'Field': 'computing'}}, not a "
            f"{type(filter).__name__}. Nothing was sent.",
            operation=op,
        )
    check_count("limit", limit, minimum=0, operation=op)
    return paging.SearchPager(database=ref.database, bucket=ref.name, filter=filter, limit=limit)


# --- btree ---------------------------------------------------------------------------------------


def range_pager(
    ref: BucketRef, *, gte: str | None, lte: str | None, limit: int | None, page_size: int
) -> paging.RangePager:
    op = "range"
    for name, value in (("gte", gte), ("lte", lte)):
        if value is not None and not isinstance(value, str):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise errors.InvalidArgumentError(
                f"{name} is a key (a string), not a {type(value).__name__}. Nothing was sent.",
                operation=op,
            )
    if limit is not None:
        check_count("limit", limit, minimum=0, operation=op)
    check_count("page_size", page_size, minimum=1, operation=op)
    return paging.RangePager(
        database=ref.database,
        bucket=ref.name,
        gte=gte or "",
        lte=lte or "",
        limit=limit,
        page_size=page_size,
    )


def entries(rows: list[RangeEntry]) -> list[Entry[Any]]:
    return [Entry(row.key, row.value if row.value is not None else {}) for row in rows]


def _end(ref: BucketRef, op: str, request: Request, timeout: float | None) -> Op[Entry[Any] | None]:
    reply = yield call(op, ref.target, request, "READ", timeout)
    r: RangeResponse = reply.response
    rows = entries(r.entries)
    return rows[0] if rows else None  # an empty bucket answers a message, not an error


def first(ref: BucketRef, *, timeout: float | None) -> Op[Entry[Any] | None]:
    request = BTreeFirstRequest(database=ref.database, bucket=ref.name)
    return (yield from _end(ref, "first", request, timeout))


def last(ref: BucketRef, *, timeout: float | None) -> Op[Entry[Any] | None]:
    request = BTreeLastRequest(database=ref.database, bucket=ref.name)
    return (yield from _end(ref, "last", request, timeout))
