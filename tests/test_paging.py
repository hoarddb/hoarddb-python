# SPDX-License-Identifier: Apache-2.0
"""P5b: the cursor paging machines (spec 4.8): halving, expect_bucket_id, keyset range."""

from __future__ import annotations

import math
from typing import Any

import pytest

from hoarddb import errors
from hoarddb._core import messages, paging
from hoarddb._core.messages import RangeEntry, RangeResponse, ScanRequest, ScanResponse

BUCKET_ID = 4242


class TooLarge(Exception):
    pass


class FakeScan:
    """A server that holds ``count`` documents and answers SCAN pages like the real one."""

    def __init__(
        self, count: int, *, ceiling: int | None = None, bucket_id: int = BUCKET_ID
    ) -> None:
        self.docs = [{"_id_": f"d{i:04d}", "n": i} for i in range(count)]
        self.ceiling = ceiling
        self.bucket_id = bucket_id
        self.requests: list[ScanRequest] = []

    def handle(self, req: ScanRequest) -> ScanResponse:
        self.requests.append(req)
        if req.expect_bucket_id and req.expect_bucket_id != self.bucket_id:
            raise errors.InvalidArgumentError('Bucket "b" was dropped and re-created')
        page = self.docs[req.skip : req.skip + req.limit] if req.limit else self.docs[req.skip :]
        if self.ceiling is not None and len(page) > self.ceiling:
            raise TooLarge
        return ScanResponse(
            found=True,
            documents=page,
            total=len(self.docs),
            has_more=req.skip + len(page) < len(self.docs),
            bucket_id=self.bucket_id,
        )


def drain(pager: paging.FindPager, server: FakeScan) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    guard = 0
    while (req := pager.next_request()) is not None:
        guard += 1
        assert guard < 500
        try:
            response = server.handle(req)
        except TooLarge:
            pager.too_large(errors.ResultTooLargeError("big", max_result_bytes=1, retained_bytes=2))
            continue
        out.extend(pager.accept(response))
    return out


def find(**kwargs: Any) -> paging.FindPager:
    defaults: dict[str, Any] = {"database": "d", "bucket": "b"}
    return paging.FindPager(**(defaults | kwargs))


def test_pages_of_100_walk_the_whole_bucket_with_skip() -> None:
    server = FakeScan(250)
    docs = drain(find(), server)
    assert [d["n"] for d in docs] == list(range(250))
    assert [(r.skip, r.limit) for r in server.requests] == [(0, 100), (100, 100), (200, 100)]


def test_the_first_page_has_no_expect_bucket_id_and_every_later_page_carries_the_last_one() -> None:
    server = FakeScan(250)
    drain(find(), server)
    assert [r.expect_bucket_id for r in server.requests] == [0, BUCKET_ID, BUCKET_ID]


def test_a_server_that_sends_no_bucket_id_gets_no_expect_bucket_id() -> None:
    server = FakeScan(250, bucket_id=0)
    drain(find(), server)
    assert {r.expect_bucket_id for r in server.requests} == {0}


def test_a_bucket_recreated_between_pages_is_raised_and_never_continued() -> None:
    server = FakeScan(250)
    pager = find()
    first = pager.next_request()
    assert first is not None
    pager.accept(server.handle(first))
    server.bucket_id = BUCKET_ID + 1  # dropped and re-created
    second = pager.next_request()
    assert second is not None
    assert second.expect_bucket_id == BUCKET_ID
    with pytest.raises(errors.InvalidArgumentError, match="re-created"):
        server.handle(second)
    # the walk is over: the pager has no way to restart as a fresh scan
    pager.abandon()
    assert pager.next_request() is None


def test_skip_and_limit_are_honoured_across_pages() -> None:
    server = FakeScan(500)
    docs = drain(find(skip=5, limit=250), server)
    assert [d["n"] for d in docs] == list(range(5, 255))
    assert [(r.skip, r.limit) for r in server.requests] == [(5, 100), (105, 100), (205, 50)]


def test_a_limit_inside_the_first_page_asks_for_just_that() -> None:
    server = FakeScan(500)
    docs = drain(find(limit=7), server)
    assert len(docs) == 7
    assert [(r.skip, r.limit) for r in server.requests] == [(0, 7)]


def test_total_comes_from_the_first_page() -> None:
    server = FakeScan(250)
    pager = find()
    assert pager.total is None
    req = pager.next_request()
    assert req is not None
    pager.accept(server.handle(req))
    assert pager.total == 250


def test_the_filter_sort_and_projection_ride_every_page() -> None:
    server = FakeScan(250)
    drain(find(filter={"n": {"$gte": 0}}, sort={"n": -1}, projection={"n": 1}), server)
    for r in server.requests:
        assert (r.filter, r.sort, r.projection) == ({"n": {"$gte": 0}}, {"n": -1}, {"n": 1})


def test_a_too_large_page_halves_until_it_fits_and_the_size_is_kept() -> None:
    server = FakeScan(300, ceiling=30)
    docs = drain(find(), server)
    assert [d["n"] for d in docs] == list(range(300))
    sizes = [r.limit for r in server.requests]
    assert sizes[:3] == [100, 50, 25]  # halved until it fit
    assert set(sizes[3:]) == {25}  # and 25 is kept for the later pages


def test_a_later_page_may_halve_again() -> None:
    class Heavier(FakeScan):
        def handle(self, req: ScanRequest) -> ScanResponse:
            self.ceiling = 60 if req.skip == 0 else 10
            return super().handle(req)

    server = Heavier(200)
    docs = drain(find(), server)
    assert len(docs) == 200
    sizes = [r.limit for r in server.requests]
    assert sizes[0] == 100
    assert sizes[1] == 50  # page 1 halved once
    assert min(sizes) <= 10  # a later page halved again


def test_halving_makes_at_most_log2_page_size_plus_one_attempts_per_page() -> None:
    server = FakeScan(10, ceiling=1)
    pager = find(page_size=100)
    attempts = 0
    while (req := pager.next_request()) is not None and not pager.accepted_any:
        attempts += 1
        try:
            pager.accept(server.handle(req))
        except TooLarge:
            pager.too_large(errors.ResultTooLargeError("x", max_result_bytes=1, retained_bytes=1))
    assert attempts == math.floor(math.log2(100)) + 1  # 100, 50, 25, 12, 6, 3, 1


def test_a_single_document_that_is_still_too_large_raises_with_both_numbers() -> None:
    pager = find(page_size=1)
    assert pager.next_request() is not None
    err = errors.ResultTooLargeError("big", max_result_bytes=1_048_576, retained_bytes=734_003)
    with pytest.raises(errors.ResultTooLargeError) as info:
        pager.too_large(err)
    assert info.value is err
    assert (info.value.max_result_bytes, info.value.retained_bytes) == (1_048_576, 734_003)


def test_has_more_with_no_documents_is_a_protocol_error_not_a_spin() -> None:
    pager = find()
    assert pager.next_request() is not None
    with pytest.raises(errors.ProtocolError, match="has_more"):
        pager.accept(ScanResponse(found=True, documents=[], total=5, has_more=True))


def test_an_empty_bucket_is_one_empty_page() -> None:
    server = FakeScan(0)
    assert drain(find(), server) == []
    assert len(server.requests) == 1


def test_break_after_one_page_leaves_nothing_behind() -> None:
    pager = find()
    assert pager.next_request() is not None
    pager.abandon()  # a cursor pins nothing: this is free, there is no state to release
    assert pager.next_request() is None


# --- range: keyset paging ----------------------------------------------------------------------


class FakeTree:
    def __init__(self, keys: list[str]) -> None:
        self.keys = sorted(keys)
        self.requests: list[messages.BTreeRangeRequest] = []

    def handle(self, req: messages.BTreeRangeRequest) -> RangeResponse:
        self.requests.append(req)
        rows = [
            k
            for k in self.keys
            if (not req.low or k >= req.low) and (not req.high or k <= req.high)
        ]
        if req.limit:
            rows = rows[: req.limit]
        return RangeResponse(entries=[RangeEntry(key=k, value={"k": k}) for k in rows])


def walk(pager: paging.RangePager, tree: FakeTree, between: Any = None) -> list[str]:
    out: list[str] = []
    while (req := pager.next_request()) is not None:
        out.extend(e.key for e in pager.accept(tree.handle(req)))
        if between:
            between(tree)
    return out


def test_range_pages_by_key_and_drops_the_duplicate_first_row() -> None:
    tree = FakeTree([f"k{i:03d}" for i in range(250)])
    got = walk(paging.RangePager(database="d", bucket="b", page_size=100), tree)
    assert got == [f"k{i:03d}" for i in range(250)]
    lows = [r.low for r in tree.requests]
    assert lows == ["", "k099", "k199"]  # the next page's gte is the last key seen
    assert [r.limit for r in tree.requests] == [100, 101, 101]  # one extra for the repeated row


def test_range_bounds_and_limit() -> None:
    tree = FakeTree([f"k{i:03d}" for i in range(250)])
    got = walk(
        paging.RangePager(
            database="d", bucket="b", gte="k010", lte="k200", limit=150, page_size=60
        ),
        tree,
    )
    assert got == [f"k{i:03d}" for i in range(10, 160)]
    assert tree.requests[0].low == "k010"
    assert tree.requests[0].high == "k200"


def test_range_paging_is_stable_under_concurrent_inserts() -> None:
    start = [f"k{i:03d}" for i in range(0, 600, 2)]
    tree = FakeTree(start)
    counter = iter(range(10_000))

    def churn(t: FakeTree) -> None:
        n = next(counter)
        t.keys.append(f"k{(n * 7) % 600:03d}x")  # lands before or after the boundary
        t.keys.sort()

    got = walk(paging.RangePager(database="d", bucket="b", page_size=37), tree, between=churn)
    assert len(got) == len(set(got)), "a key was returned twice"
    assert set(start) <= set(got), "a key that was there all along was skipped"
    assert got == sorted(got)


def test_range_on_an_empty_tree_and_an_exact_multiple_of_the_page() -> None:
    assert walk(paging.RangePager(database="d", bucket="b"), FakeTree([])) == []
    tree = FakeTree([f"k{i:03d}" for i in range(200)])
    assert walk(paging.RangePager(database="d", bucket="b", page_size=100), tree) == [
        f"k{i:03d}" for i in range(200)
    ]


def test_range_halves_a_too_large_page_too() -> None:
    pager = paging.RangePager(database="d", bucket="b", page_size=100)
    first = pager.next_request()
    assert first is not None
    assert first.limit == 100
    pager.too_large(errors.ResultTooLargeError("x", max_result_bytes=1, retained_bytes=1))
    again = pager.next_request()
    assert again is not None
    assert again.limit == 50


# --- FIELD_LIST: after / next ------------------------------------------------------------------


def test_field_list_pages_with_the_opaque_next_passed_back_unchanged() -> None:
    pager = paging.FieldListPager(
        database="d", bucket="b", key="ada", field="Purchases", page_size=2
    )
    req = pager.next_request()
    assert req is not None
    assert (req.after, req.limit, req.key, req.field) == (None, 2, "ada", "Purchases")
    items = [messages.FieldItem(element_key="a"), messages.FieldItem(element_key="b")]
    out = pager.accept(
        messages.FieldListResponse(entries=items, has_more=True, next=b"\x00\xffopaque")
    )
    assert [i.element_key for i in out] == ["a", "b"]
    req = pager.next_request()
    assert req is not None
    assert req.after == b"\x00\xffopaque"
    pager.accept(messages.FieldListResponse(entries=[messages.FieldItem(element_key="c")]))
    assert pager.next_request() is None


def test_field_list_stops_at_its_overall_limit_and_asks_for_no_more_than_is_left() -> None:
    pager = paging.FieldListPager(
        database="d", bucket="b", key="ada", field="P", page_size=3, limit=5
    )
    first = pager.next_request()
    assert first is not None and first.limit == 3
    rows = [messages.FieldItem(element_key=str(i)) for i in range(3)]
    assert (
        len(pager.accept(messages.FieldListResponse(entries=rows, has_more=True, next=b"n1"))) == 3
    )
    second = pager.next_request()
    assert second is not None and second.limit == 2  # only two are left of the five
    more = [messages.FieldItem(element_key=str(i)) for i in range(3, 6)]  # a server may overshoot
    out = pager.accept(messages.FieldListResponse(entries=more, has_more=True, next=b"n2"))
    assert [i.element_key for i in out] == ["3", "4"]  # cut to the limit
    assert pager.next_request() is None


def test_field_list_with_no_limit_walks_to_the_end() -> None:
    pager = paging.FieldListPager(database="d", bucket="b", key="k", field="F", page_size=2)
    row = [messages.FieldItem(element_key="a")]
    pager.accept(messages.FieldListResponse(entries=row, has_more=True, next=b"n"))
    assert pager.next_request() is not None
