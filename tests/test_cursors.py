# SPDX-License-Identifier: Apache-2.0
"""P10: the two public cursor shells, and one page of a walk through the runners."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import fakehoard
import faketls
import runnerkit as tr
from hoarddb import errors
from hoarddb._aio.cursor import AsyncCursor
from hoarddb._core import frame, opcodes, paging
from hoarddb._core.call import Call
from hoarddb._core.messages import Request
from hoarddb._ops.pages import next_page
from hoarddb._sync.cursor import Cursor

# --- the shells ----------------------------------------------------------------------------------


class Pages:
    """A fetch function over fixed pages that counts how often it was asked."""

    def __init__(self, *pages: list[int] | BaseException, total: int | None = None) -> None:
        self.pages = list(pages)
        self.calls = 0
        self.total = total

    def __call__(self) -> list[int] | None:
        self.calls += 1
        if not self.pages:
            return None
        page = self.pages.pop(0)
        if isinstance(page, BaseException):
            raise page
        return page

    async def afetch(self) -> list[int] | None:
        return self()


def sync_cursor(pages: Pages) -> Cursor[int]:
    return Cursor(pages, lambda: pages.total)


def async_cursor(pages: Pages) -> AsyncCursor[int]:
    return AsyncCursor(pages.afetch, lambda: pages.total)


def test_a_cursor_iterates_every_document_across_pages_and_skips_empty_pages() -> None:
    pages = Pages([1, 2], [], [3], [4, 5])
    assert list(sync_cursor(pages)) == [1, 2, 3, 4, 5]


def test_to_list_and_batches() -> None:
    assert sync_cursor(Pages([1, 2], [3])).to_list() == [1, 2, 3]
    assert list(sync_cursor(Pages([1, 2], [], [3])).batches()) == [[1, 2], [3]]


def test_batches_after_partial_iteration_yields_the_rest_of_the_current_page_first() -> None:
    cursor = sync_cursor(Pages([1, 2, 3], [4]))
    assert next(cursor) == 1
    assert list(cursor.batches()) == [[2, 3], [4]]


def test_pages_are_fetched_lazily_and_a_break_costs_nothing() -> None:
    pages = Pages([1, 2], [3, 4], [5])
    cursor = sync_cursor(pages)
    for item in cursor:
        if item == 2:
            break
    assert pages.calls == 1  # the second page was never asked for
    cursor.close()
    assert pages.calls == 1


def test_close_stops_further_fetches_and_drops_the_buffer() -> None:
    pages = Pages([1, 2, 3], [4])
    cursor = sync_cursor(pages)
    assert next(cursor) == 1
    cursor.close()
    cursor.close()  # idempotent
    assert list(cursor) == [] and pages.calls == 1


def test_a_with_block_closes_the_cursor() -> None:
    pages = Pages([1], [2])
    with sync_cursor(pages) as cursor:
        assert next(cursor) == 1
    assert list(cursor) == [] and pages.calls == 1


def test_total_comes_from_the_pager_and_is_none_before_the_first_page() -> None:
    pages = Pages([1], total=None)
    cursor = sync_cursor(pages)
    assert cursor.total is None
    pages.total = 42
    assert cursor.total == 42


def test_an_error_ends_the_walk_and_is_raised_once() -> None:
    boom = errors.NotFoundError("gone")
    pages = Pages([1], boom, [2])
    cursor = sync_cursor(pages)
    assert next(cursor) == 1
    with pytest.raises(errors.NotFoundError):
        next(cursor)
    assert list(cursor) == []  # starting again is the caller's choice, not the cursor's
    assert pages.calls == 2


def test_the_async_cursor_matches() -> None:
    async def main() -> None:
        pages = Pages([1, 2], [], [3])
        assert [d async for d in async_cursor(pages)] == [1, 2, 3]
        assert await async_cursor(Pages([1], [2, 3])).to_list() == [1, 2, 3]
        assert [b async for b in async_cursor(Pages([1, 2], [], [3])).batches()] == [[1, 2], [3]]

        lazy = Pages([1, 2], [3])
        cursor = async_cursor(lazy)
        async for item in cursor:
            if item == 1:
                break
        assert lazy.calls == 1
        cursor.close()
        assert [d async for d in cursor] == []

        async with async_cursor(Pages([1], [2])) as inner:
            assert await inner.__anext__() == 1
        assert [d async for d in inner] == []

        failing = async_cursor(Pages([1], errors.NotFoundError("gone")))
        assert await failing.__anext__() == 1
        with pytest.raises(errors.NotFoundError):
            await failing.__anext__()
        assert [d async for d in failing] == []

    asyncio.run(main())


def test_a_cancelled_fetch_ends_the_async_walk() -> None:
    async def main() -> None:
        async def slow() -> list[int] | None:
            await asyncio.sleep(10)
            return [1]

        cursor = AsyncCursor(slow, lambda: None)
        task = asyncio.ensure_future(cursor.__anext__())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert [d async for d in cursor] == []

    asyncio.run(main())


# --- pages through the runners -------------------------------------------------------------------


def scan_page(docs: list[dict[str, Any]], *, has_more: bool, total: int, bucket_id: int = 7) -> Any:
    return [
        fakehoard.Reply(
            opcodes.SCAN,
            {
                "found": True,
                "database": "d",
                "bucket": "b",
                "documents": docs,
                "total": total,
                "has_more": has_more,
                "bucket_id": bucket_id,
            },
        )
    ]


def too_large() -> Any:
    return [fakehoard.error(14, "result too large", max_result_bytes=100, retained_bytes=500)]


def build(request: Request) -> Call:
    return Call(op="find", target="d/b", request=request, lane="bulk", kind="READ")


def walk_cursor(h: tr.H, pager: paging.FindPager) -> Any:
    """A cursor over ``pager`` run through the harness's runner, in the harness's mode."""
    if h.loop is None:
        return Cursor(lambda: h.runner.run(next_page(pager, build)), lambda: pager.total)
    loop = h.loop

    async def fetch() -> list[Any] | None:
        result: list[Any] | None = await h.runner.run(next_page(pager, build))
        return result

    return _LoopCursor(loop, AsyncCursor(fetch, lambda: pager.total))


class _LoopCursor:
    """Lets the synchronous assertions below read an AsyncCursor on the harness's loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop, cursor: AsyncCursor[Any]) -> None:
        self._loop, self._cursor = loop, cursor

    def to_list(self) -> list[Any]:
        return self._loop.run_until_complete(self._cursor.to_list())

    @property
    def total(self) -> int | None:
        return self._cursor.total


def find_pager(**kw: Any) -> paging.FindPager:
    return paging.FindPager(database="d", bucket="b", **kw)


def test_a_find_walk_pages_and_page_two_on_carries_the_bucket_id(
    mode: str, identity: faketls.Identity
) -> None:
    first = scan_page([{"n": 1}, {"n": 2}], has_more=True, total=3)
    second = scan_page([{"n": 3}], has_more=False, total=3)
    with tr.harness(mode, identity, tr.script(first, second)) as (h, server):
        cursor = walk_cursor(h, find_pager(page_size=2))
        assert cursor.to_list() == [{"n": 1}, {"n": 2}, {"n": 3}]
        assert cursor.total == 3
        scans = [deserialize(f.payload) for f in server.frames_with(opcodes.SCAN)]
        assert "expect_bucket_id" not in scans[0]
        assert scans[1]["expect_bucket_id"] == 7 and scans[1]["skip"] == 2


def deserialize(payload: bytes) -> dict[str, Any]:
    from hoarddb._bson import deserialize as _d

    return _d(payload)


def test_result_too_large_halves_the_page_and_keeps_the_size_that_worked(
    mode: str, identity: faketls.Identity
) -> None:

    def handler(conn: fakehoard.Conn, request: frame.Frame) -> Any:
        limit = deserialize(request.payload)["limit"]
        if limit > 2:
            return too_large()
        docs = [{"n": i} for i in range(limit)]
        return scan_page(docs, has_more=False, total=limit)

    with tr.harness(mode, identity, handler) as (h, server):
        pager = find_pager(page_size=8)
        assert len(walk_cursor(h, pager).to_list()) == 2
        sent = [deserialize(f.payload)["limit"] for f in server.frames_with(opcodes.SCAN)]
        assert sent == [8, 4, 2]  # at most log2(8) + 1 attempts
        assert pager.page_size == 2  # kept for later pages


def test_a_single_document_over_the_ceiling_raises_with_both_numbers(
    mode: str, identity: faketls.Identity
) -> None:
    with tr.harness(mode, identity, lambda conn, f: too_large()) as (h, _):
        with pytest.raises(errors.ResultTooLargeError) as caught:
            walk_cursor(h, find_pager(page_size=4)).to_list()
        assert (caught.value.max_result_bytes, caught.value.retained_bytes) == (100, 500)


def test_a_bucket_dropped_between_pages_is_raised_and_never_restarted(
    mode: str, identity: faketls.Identity
) -> None:
    stale = [
        fakehoard.error(1, 'Bucket "b" in database "d" was dropped and re-created since page 1')
    ]
    first = scan_page([{"n": 1}], has_more=True, total=2)
    with tr.harness(mode, identity, tr.script(first, stale)) as (h, server):
        with pytest.raises(errors.NotFoundError, match="dropped and re-created"):
            walk_cursor(h, find_pager(page_size=1)).to_list()
        assert len(server.frames_with(opcodes.SCAN)) == 2  # not re-scanned from the start
