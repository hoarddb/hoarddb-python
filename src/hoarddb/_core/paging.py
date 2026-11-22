# SPDX-License-Identifier: Apache-2.0
"""The cursor paging machines (spec 4.8). The server has no cursor: every page is an independent
request, and a cursor pins no connection, socket or timer. These machines decide what to ask for
next; a runner (or a test's fake server) answers. Nothing here performs I/O.

* ``find`` pages by ``skip``/``limit`` and, from page 2 on, carries ``expect_bucket_id`` so a bucket
  dropped and re-created between pages is refused by the server instead of read as a continuation.
* ``range`` pages by key: the next page's low bound is the last key seen, which makes it stable
  under concurrent inserts (the repeated first row is dropped).
* A page that is ``RESULT_TOO_LARGE`` is asked again with half the page size, down to one document;
  the size that worked is kept for later pages.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hoarddb import errors
from hoarddb._core import messages
from hoarddb._core.messages import (
    FieldItem,
    FieldListRequest,
    FieldListResponse,
    RangeEntry,
    RangeResponse,
    ScanRequest,
    ScanResponse,
)

DEFAULT_PAGE_SIZE = 100


class _Halving:
    """Shared page-size state: halve on RESULT_TOO_LARGE, keep what worked."""

    __slots__ = ("_abandoned", "accepted_any", "page_size")

    def __init__(self, page_size: int) -> None:
        if page_size < 1:
            raise ValueError("page_size must be at least 1")
        self.page_size = page_size
        self.accepted_any = False
        self._abandoned = False

    def abandon(self) -> None:
        """Stop for good (a ``break``, ``close()`` or a raised error). Costs nothing: no state is
        held anywhere else."""
        self._abandoned = True

    def too_large(self, error: errors.ResultTooLargeError) -> None:
        """The page just asked for was too large: halve and ask again, or raise ``error`` (with
        its two numbers) when a single document is already more than the server will hold."""
        if self.page_size <= 1:
            raise error
        self.page_size //= 2


class FindPager(_Halving):
    __slots__ = (
        "_bucket",
        "_bucket_id",
        "_database",
        "_done",
        "_fetched",
        "_filter",
        "_limit",
        "_pending",
        "_projection",
        "_returned",
        "_skip",
        "_sort",
        "total",
    )

    def __init__(
        self,
        *,
        database: str,
        bucket: str,
        filter: Mapping[str, Any] | None = None,  # noqa: A002 - the verb's own keyword
        sort: Mapping[str, Any] | None = None,
        projection: Mapping[str, Any] | None = None,
        skip: int = 0,
        limit: int | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        super().__init__(page_size)
        self._database, self._bucket = database, bucket
        self._filter, self._sort, self._projection = filter, sort, projection
        self._skip = skip
        self._limit = limit or None  # 0 and None both mean "no limit"
        self._fetched = 0
        self._returned = 0
        self._bucket_id = 0
        self._done = False
        self._pending: ScanRequest | None = None
        self.total: int | None = None  # from the first page: the exact match count

    def next_request(self) -> ScanRequest | None:
        if self._done or self._abandoned:
            return None
        want = self.page_size
        if self._limit is not None:
            want = min(want, self._limit - self._returned)
        request = ScanRequest(
            database=self._database,
            bucket=self._bucket,
            filter=self._filter,
            skip=self._skip + self._fetched,
            limit=want,
            sort=self._sort,
            projection=self._projection,
            expect_bucket_id=self._bucket_id,
        )
        self._pending = request
        return request

    def accept(self, response: ScanResponse) -> list[dict[str, Any]]:
        documents = response.documents
        if response.has_more and not documents:
            raise errors.ProtocolError(
                "The server answered a scan page with has_more and no documents. "
                "The connection was discarded.",
                operation="find",
            )
        if self.total is None:
            self.total = response.total
        if response.bucket_id:
            self._bucket_id = response.bucket_id
        self._fetched += len(documents)
        self._returned += len(documents)
        self.accepted_any = True
        if (
            not response.has_more
            or not documents
            or (self._limit is not None and self._returned >= self._limit)
        ):
            self._done = True
        return list(documents)


class RangePager(_Halving):
    __slots__ = (
        "_bucket",
        "_database",
        "_done",
        "_high",
        "_last_key",
        "_limit",
        "_low",
        "_requested",
        "_returned",
    )

    def __init__(
        self,
        *,
        database: str,
        bucket: str,
        gte: str = "",
        lte: str = "",
        limit: int | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        super().__init__(page_size)
        self._database, self._bucket = database, bucket
        self._low, self._high = gte, lte
        self._limit = limit or None
        self._returned = 0
        self._last_key: str | None = None
        self._requested = 0
        self._done = False

    def next_request(self) -> messages.BTreeRangeRequest | None:
        if self._done or self._abandoned:
            return None
        want = self.page_size
        if self._limit is not None:
            want = min(want, self._limit - self._returned)
        # After the first page the low bound is inclusive and repeats the last key: ask for one
        # more so every page still makes progress.
        self._requested = want + (1 if self._last_key is not None else 0)
        return messages.BTreeRangeRequest(
            database=self._database,
            bucket=self._bucket,
            low=self._last_key if self._last_key is not None else self._low,
            high=self._high,
            limit=self._requested,
        )

    def accept(self, response: RangeResponse) -> list[RangeEntry]:
        rows = response.entries
        self.accepted_any = True
        if len(rows) < self._requested:
            self._done = True  # a short page is the last one
        fresh = (
            rows[1:]
            if rows and self._last_key is not None and rows[0].key == self._last_key
            else rows
        )
        if rows:
            self._last_key = rows[-1].key
        if self._limit is not None:
            fresh = fresh[: self._limit - self._returned]
        self._returned += len(fresh)
        if self._limit is not None and self._returned >= self._limit:
            self._done = True
        return list(fresh)


class FieldListPager(_Halving):
    """``FIELD_LIST``: the next page starts after the opaque ``next`` of the last one."""

    __slots__ = (
        "_after",
        "_bucket",
        "_database",
        "_done",
        "_element",
        "_field",
        "_key",
        "_limit",
        "_path",
        "_returned",
    )

    def __init__(
        self,
        *,
        database: str,
        bucket: str,
        key: str,
        field: str,
        path: list[Any] | None = None,
        element: str = "",
        page_size: int = DEFAULT_PAGE_SIZE,
        limit: int | None = None,
    ) -> None:
        super().__init__(page_size)
        self._database, self._bucket, self._key = database, bucket, key
        self._field, self._path, self._element = field, path or [], element
        self._limit = limit or None  # 0 and None both mean "no limit"
        self._returned = 0
        self._after: Any = None
        self._done = False

    def next_request(self) -> FieldListRequest | None:
        if self._done or self._abandoned:
            return None
        want = self.page_size
        if self._limit is not None:
            want = min(want, self._limit - self._returned)
        return FieldListRequest(
            database=self._database,
            bucket=self._bucket,
            key=self._key,
            field=self._field,
            path=self._path,
            element=self._element,
            after=self._after,
            limit=want,
        )

    def accept(self, response: FieldListResponse) -> list[FieldItem]:
        self.accepted_any = True
        entries = list(response.entries)
        if self._limit is not None:
            entries = entries[: self._limit - self._returned]
        self._returned += len(entries)
        if (
            not response.has_more
            or not response.entries
            or (self._limit is not None and self._returned >= self._limit)
        ):
            self._done = True
        else:
            self._after = response.next  # passed back unchanged
        return entries


class SearchPager(_Halving):
    """``SEARCH`` is one page: ``total`` is the number returned, and ``has_more`` only says the
    answer was cut by ``limit`` (the remedy is a larger ``limit``)."""

    __slots__ = ("_bucket", "_database", "_done", "_filter", "_limit", "total")

    def __init__(
        self,
        *,
        database: str,
        bucket: str,
        filter: Mapping[str, Any],  # noqa: A002 - the verb's own keyword
        limit: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        super().__init__(max(1, limit))
        self._database, self._bucket, self._filter = database, bucket, filter
        self._limit = limit
        self._done = False
        self.total: int | None = None

    def next_request(self) -> messages.SearchRequest | None:
        if self._done or self._abandoned:
            return None
        return messages.SearchRequest(
            database=self._database, bucket=self._bucket, filter=self._filter, limit=self._limit
        )

    def accept(self, response: messages.SearchResponse) -> list[dict[str, Any]]:
        self.accepted_any = True
        self._done = True
        results = list(response.results)
        self.total = len(results)
        return results
