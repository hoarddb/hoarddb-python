# SPDX-License-Identifier: Apache-2.0
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false, reportUnknownLambdaType=false
"""P13: find, find_one, count, distinct, search, range, first, last and the cursors, through both
surfaces against a scripted server."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import fakehoard
from clientkit import Script, reply
from clientkit import public as _public
from hoarddb import errors
from hoarddb._core import opcodes


def public(*a: Any, **k: Any) -> Any:
    return _public(*a, **k)


def bucket(pc: Any, facet: str = "bucket", name: str = "people") -> Any:
    return getattr(pc.client.database("app"), facet)(name)


def listing(cursor: Any, pc: Any) -> list[Any]:
    """Read a cursor to the end, with either surface."""
    if pc.loop is None:
        return list(cursor)

    async def go() -> list[Any]:
        return [item async for item in cursor]

    return pc.loop.run_until_complete(go())  # type: ignore[no-any-return]


def to_list(cursor: Any, pc: Any) -> list[Any]:
    if pc.loop is None:
        return list(cursor.to_list())
    return pc.loop.run_until_complete(cursor.to_list())  # type: ignore[no-any-return]


def page(
    docs: list[dict[str, Any]], *, more: bool, total: int, bucket_id: int | None = 7, **extra: Any
) -> Any:
    doc: dict[str, Any] = {
        "found": True,
        "database": "app",
        "bucket": "people",
        "documents": docs,
        "total": total,
        "has_more": more,
        **extra,
    }
    if bucket_id is not None:
        doc["bucket_id"] = bucket_id
    return reply(opcodes.SCAN, doc)


def docs(start: int, n: int) -> list[dict[str, Any]]:
    return [{"n": i, "_id_": f"k{i}"} for i in range(start, start + n)]


# --- count, distinct, find_one -------------------------------------------------------------------


@pytest.mark.parametrize("facet", ["bucket", "hash", "btree", "fifo", "lifo", "heap", "blob"])
def test_count_is_on_every_facet(mode: str, identity: Any, facet: str) -> None:
    script = Script({opcodes.COUNT: reply(opcodes.COUNT, {"count": 3})})
    with public(mode, identity, script) as (pc, _, _):
        assert pc.call(lambda c: bucket(pc, facet).count()) == 3
        assert script.of(opcodes.COUNT)[0] == {"database": "app", "bucket": "people"}


def test_count_with_a_filter(mode: str, identity: Any) -> None:
    script = Script({opcodes.COUNT: reply(opcodes.COUNT, {"count": 2})})
    with public(mode, identity, script) as (pc, _, _):
        assert pc.call(lambda c: bucket(pc, "hash").count({"Field": "computing"})) == 2
        assert script.of(opcodes.COUNT)[0]["filter"] == {"Field": "computing"}


def test_a_filter_must_be_a_mapping(mode: str, identity: Any) -> None:
    with public(mode, identity) as (pc, script, _):
        for call in (
            lambda c: bucket(pc).count("Field=computing"),
            lambda c: bucket(pc).distinct("Field", ["x"]),
            lambda c: bucket(pc).find_one(5),
        ):
            with pytest.raises(errors.InvalidArgumentError, match="filter is a mapping"):
                pc.call(call)
        assert script.requests == []


def test_distinct_is_on_the_bulk_lane(mode: str, identity: Any) -> None:
    script = Script({opcodes.DISTINCT: reply(opcodes.DISTINCT, {"values": ["maths", "computing"]})})
    with public(mode, identity, script) as (pc, _, _):
        assert pc.call(lambda c: bucket(pc, "hash").distinct("Field")) == ["maths", "computing"]
        assert script.of(opcodes.DISTINCT)[0] == {
            "database": "app",
            "bucket": "people",
            "field": "Field",
        }
        assert pc.client.pool_stats().bulk_idle == 1


def test_find_one_asks_for_one_and_returns_it_or_none(mode: str, identity: Any) -> None:
    script = Script(
        {
            opcodes.SCAN: [
                page(docs(0, 1), more=True, total=5),
                page([], more=False, total=0),
            ]
        }
    )
    with public(mode, identity, script) as (pc, _, _):
        assert pc.call(lambda c: bucket(pc, "hash").find_one({"n": 0})) == docs(0, 1)[0]
        assert pc.call(lambda c: bucket(pc, "hash").find_one({"n": 9})) is None
        assert [d["limit"] for d in script.of(opcodes.SCAN)] == [1, 1]


# --- find ----------------------------------------------------------------------------------------


def test_find_walks_pages_lazily_and_carries_the_bucket_id_from_page_two(
    mode: str, identity: Any
) -> None:
    script = Script(
        {
            opcodes.SCAN: [
                page(docs(0, 2), more=True, total=5, bucket_id=11),
                page(docs(2, 2), more=True, total=5, bucket_id=11),
                page(docs(4, 1), more=False, total=5, bucket_id=11),
            ]
        }
    )
    with public(mode, identity, script) as (pc, _, _):
        cursor = pc.call(lambda c: bucket(pc, "hash").find({"Field": "x"}, page_size=2))
        assert script.of(opcodes.SCAN) == []  # making a cursor sends nothing
        assert listing(cursor, pc) == docs(0, 5)
        scans = script.of(opcodes.SCAN)
        assert [(s.get("skip", 0), s["limit"]) for s in scans] == [(0, 2), (2, 2), (4, 2)]
        assert "expect_bucket_id" not in scans[0]
        assert [s["expect_bucket_id"] for s in scans[1:]] == [11, 11]
        assert all(s["filter"] == {"Field": "x"} for s in scans)
        assert cursor.total == 5


def test_a_break_costs_nothing_and_a_cursor_pins_no_connection(mode: str, identity: Any) -> None:
    script = Script(
        {
            opcodes.SCAN: [
                page(docs(0, 2), more=True, total=9),
                page(docs(2, 2), more=False, total=9),
            ]
        }
    )
    with public(mode, identity, script) as (pc, _, _):
        cursor = pc.call(lambda c: bucket(pc, "hash").find(page_size=2))
        first = (
            next(iter(cursor))
            if pc.loop is None
            else pc.loop.run_until_complete(cursor.__anext__())
        )
        assert first == docs(0, 1)[0]
        stats = pc.client.pool_stats()
        assert stats.bulk_in_use == 0 and stats.point_in_use == 0  # nothing held between pages
        cursor.close()
        assert len(script.of(opcodes.SCAN)) == 1


def test_find_with_sort_projection_skip_and_limit(mode: str, identity: Any) -> None:
    script = Script({opcodes.SCAN: page(docs(0, 1), more=True, total=2)})
    with public(mode, identity, script) as (pc, _, _):
        cursor = pc.call(
            lambda c: bucket(pc, "hash").find(
                {"Field": "computing"},
                sort={"Born": -1},
                projection={"Name": 1},
                skip=3,
                limit=1,
            )
        )
        assert to_list(cursor, pc) == docs(0, 1)
        (sent,) = script.of(opcodes.SCAN)
        assert sent["sort"] == {"Born": -1} and sent["projection"] == {"Name": 1}
        assert sent["skip"] == 3 and sent["limit"] == 1  # the limit caps the first page too


def test_find_stops_at_its_limit_across_pages(mode: str, identity: Any) -> None:
    script = Script(
        {
            opcodes.SCAN: [
                page(docs(0, 2), more=True, total=9),
                page(docs(2, 1), more=True, total=9),
                page(docs(3, 2), more=True, total=9),
            ]
        }
    )
    with public(mode, identity, script) as (pc, _, _):
        cursor = pc.call(lambda c: bucket(pc, "hash").find(limit=3, page_size=2))
        assert to_list(cursor, pc) == docs(0, 3)
        assert [s["limit"] for s in script.of(opcodes.SCAN)] == [2, 1]


@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({"sort": {"a": 1, "b": 1}}, "exactly one field"),
        ({"sort": {}}, "exactly one field"),
        ({"sort": {"a": 2}}, "1 (ascending) or -1"),
        ({"sort": {"a": True}}, "1 (ascending) or -1"),
        ({"sort": "a"}, "exactly one field"),
        ({"projection": {"a": 2}}, "1 (keep) or 0 (drop)"),
        ({"projection": ["a"]}, "projection is a mapping"),
        ({"skip": -1}, "skip must be"),
        ({"skip": True}, "skip must be"),
        ({"limit": -1}, "limit must be"),
        ({"page_size": 0}, "page_size must be"),
        ({"page_size": 1.5}, "page_size must be"),
    ],
)
def test_bad_find_arguments_are_refused_when_the_cursor_is_made(
    mode: str, identity: Any, kwargs: dict[str, Any], needle: str
) -> None:
    with public(mode, identity) as (pc, script, _):
        with pytest.raises(errors.InvalidArgumentError, match="Nothing was sent") as caught:
            pc.call(lambda c: bucket(pc, "hash").find(**kwargs))
        assert needle in str(caught.value)
        assert script.requests == []


def test_find_of_a_bucket_that_does_not_exist_is_not_found(mode: str, identity: Any) -> None:
    gone = reply(
        opcodes.SCAN,
        {
            "found": False,
            "documents": None,
            "message": 'bucket not found: bucket "people" not found in database "app". Run ...',
        },
    )
    with public(mode, identity, Script({opcodes.SCAN: gone})) as (pc, _, _):
        cursor = pc.call(lambda c: bucket(pc, "hash").find())
        with pytest.raises(errors.NotFoundError) as caught:
            listing(cursor, pc)
        assert "create_bucket" in str(caught.value)


def test_find_of_a_blob_bucket_is_the_servers_refusal(mode: str, identity: Any) -> None:
    refusal = reply(
        opcodes.SCAN,
        {"found": False, "documents": None, "message": "db.files.find() does not scan blobs"},
    )
    with public(mode, identity, Script({opcodes.SCAN: refusal})) as (pc, _, _):
        cursor = pc.call(lambda c: bucket(pc).find())
        with pytest.raises(errors.InvalidArgumentError, match="does not scan blobs"):
            listing(cursor, pc)


def test_has_more_with_no_documents_is_a_protocol_error(mode: str, identity: Any) -> None:
    with public(mode, identity, Script({opcodes.SCAN: page([], more=True, total=9)})) as (pc, _, _):
        cursor = pc.call(lambda c: bucket(pc).find())
        with pytest.raises(errors.ProtocolError, match="has_more"):
            listing(cursor, pc)


# --- RESULT_TOO_LARGE ----------------------------------------------------------------------------


def too_large() -> Any:
    return [fakehoard.error(14, "result too large", max_result_bytes=1000, retained_bytes=5000)]


def test_result_too_large_halves_the_page_and_the_size_that_worked_is_kept(
    mode: str, identity: Any
) -> None:
    def answer(doc: dict[str, Any]) -> Any:
        limit = doc["limit"]
        if limit > 4:
            return too_large()
        start = doc.get("skip", 0)
        remaining = max(0, 10 - start)
        n = min(limit, remaining)
        return page(docs(start, n), more=start + n < 10, total=10)

    script = Script({opcodes.SCAN: answer})
    with public(mode, identity, script) as (pc, _, _):
        cursor = pc.call(lambda c: bucket(pc, "hash").find(page_size=16))
        assert len(to_list(cursor, pc)) == 10
        sent = [s["limit"] for s in script.of(opcodes.SCAN)]
        assert sent[:3] == [16, 8, 4]  # at most log2(16) + 1 attempts for the first page
        assert all(n == 4 for n in sent[3:])  # later pages keep the size that worked


def test_a_later_page_may_halve_again(mode: str, identity: Any) -> None:
    def answer(doc: dict[str, Any]) -> Any:
        start = doc.get("skip", 0)
        if start >= 4 and doc["limit"] > 2:
            return too_large()  # the documents got fatter
        n = min(doc["limit"], 6 - start)
        return page(docs(start, n), more=start + n < 6, total=6)

    script = Script({opcodes.SCAN: answer})
    with public(mode, identity, script) as (pc, _, _):
        cursor = pc.call(lambda c: bucket(pc, "hash").find(page_size=4))
        assert len(to_list(cursor, pc)) == 6
        assert [s["limit"] for s in script.of(opcodes.SCAN)] == [4, 4, 2]


def test_a_single_document_over_the_ceiling_raises_with_both_numbers(
    mode: str, identity: Any
) -> None:
    script = Script({opcodes.SCAN: lambda doc: too_large()})
    with public(mode, identity, script) as (pc, _, _):
        cursor = pc.call(lambda c: bucket(pc, "hash").find(page_size=4))
        with pytest.raises(errors.ResultTooLargeError) as caught:
            listing(cursor, pc)
        assert (caught.value.max_result_bytes, caught.value.retained_bytes) == (1000, 5000)
        assert [s["limit"] for s in script.of(opcodes.SCAN)] == [4, 2, 1]


def test_the_connection_survives_a_result_too_large(mode: str, identity: Any) -> None:
    answers = [too_large(), page(docs(0, 1), more=False, total=1)]
    script = Script({opcodes.SCAN: answers})
    with public(mode, identity, script) as (pc, _, server):
        cursor = pc.call(lambda c: bucket(pc, "hash").find(page_size=2))
        assert len(to_list(cursor, pc)) == 1
        assert len(server.connections) == 2  # connect()'s, and one bulk connection used twice


def test_a_bucket_dropped_between_pages_is_raised_and_never_rescanned(
    mode: str, identity: Any
) -> None:
    stale = [fakehoard.error(1, 'Bucket "people" in database "app" was dropped and re-created')]
    script = Script({opcodes.SCAN: [page(docs(0, 2), more=True, total=4), stale]})
    with public(mode, identity, script) as (pc, _, _):
        cursor = pc.call(lambda c: bucket(pc, "hash").find(page_size=2))
        assert next(iter(cursor)) if pc.loop is None else True
        with pytest.raises(errors.NotFoundError, match="dropped and re-created"):
            to_list(cursor, pc) if pc.loop is None else listing(cursor, pc)
        assert len(script.of(opcodes.SCAN)) == 2


# --- search --------------------------------------------------------------------------------------


def test_search_is_one_page_and_total_is_the_number_returned(mode: str, identity: Any) -> None:
    answer = reply(
        opcodes.SEARCH,
        {"results": docs(0, 2), "total": 2, "has_more": True, "index_used": "by_field"},
    )
    script = Script({opcodes.SEARCH: answer})
    with public(mode, identity, script) as (pc, _, _):
        cursor = pc.call(lambda c: bucket(pc, "hash").search({"Field": "x"}, limit=2))
        assert to_list(cursor, pc) == docs(0, 2)
        assert cursor.total == 2
        assert script.of(opcodes.SEARCH) == [
            {"database": "app", "bucket": "people", "filter": {"Field": "x"}, "limit": 2}
        ]
        assert len(script.of(opcodes.SEARCH)) == 1  # a cut search is not paged


def test_search_needs_a_mapping_and_a_sane_limit(mode: str, identity: Any) -> None:
    with public(mode, identity) as (pc, script, _):
        with pytest.raises(errors.InvalidArgumentError, match="filter mapping"):
            pc.call(lambda c: bucket(pc, "hash").search("Field"))
        with pytest.raises(errors.InvalidArgumentError, match="limit must be"):
            pc.call(lambda c: bucket(pc, "hash").search({"a": 1}, limit=-1))
        assert script.requests == []


def test_search_of_a_blob_is_the_servers_error(mode: str, identity: Any) -> None:
    script = Script(
        {opcodes.SEARCH: [fakehoard.error(4, "scan is not supported for bucket type blob")]}
    )
    with public(mode, identity, script) as (pc, _, _):
        cursor = pc.call(lambda c: bucket(pc).search({"a": 1}))
        with pytest.raises(errors.InvalidArgumentError, match="not supported"):
            listing(cursor, pc)


# --- btree ---------------------------------------------------------------------------------------


def rows(keys: list[str]) -> Any:
    return reply(
        opcodes.BTREE_RESPONSE,
        {"entries": [{"key": k, "value": {"k": k, "_id_": k}} for k in keys], "message": ""},
    )


def test_range_pages_by_key_and_drops_the_repeated_first_row(mode: str, identity: Any) -> None:
    script = Script(
        {
            opcodes.BTREE_RANGE: [
                rows(["a", "b", "c"]),
                rows(["c", "d", "e", "f"]),
                rows(["f", "g"]),
            ]
        }
    )
    with public(mode, identity, script) as (pc, _, _):
        cursor = pc.call(lambda c: bucket(pc, "btree").range(gte="a", lte="z", page_size=3))
        entries = to_list(cursor, pc)
        assert [e.key for e in entries] == ["a", "b", "c", "d", "e", "f", "g"]
        assert entries[0].value == {"k": "a", "_id_": "a"}
        sent = script.of(opcodes.BTREE_RANGE)
        assert [(s["low"], s["high"], s["limit"]) for s in sent] == [
            ("a", "z", 3),
            ("c", "z", 4),
            ("f", "z", 4),
        ]
        assert cursor.total is None  # a range knows no total


def test_range_with_an_open_side_and_a_limit(mode: str, identity: Any) -> None:
    script = Script({opcodes.BTREE_RANGE: rows(["a", "b"])})
    with public(mode, identity, script) as (pc, _, _):
        cursor = pc.call(lambda c: bucket(pc, "btree").range(limit=2))
        assert [e.key for e in to_list(cursor, pc)] == ["a", "b"]
        (sent,) = script.of(opcodes.BTREE_RANGE)
        assert sent.get("low", "") == "" and sent.get("high", "") == "" and sent["limit"] == 2


def test_range_on_a_hash_bucket_is_the_servers_refusal(mode: str, identity: Any) -> None:
    refusal = [
        fakehoard.error(4, 'bucket "users" is type hash; this operation requires type btree')
    ]
    with public(mode, identity, Script({opcodes.BTREE_RANGE: refusal})) as (pc, _, _):
        cursor = pc.call(lambda c: bucket(pc).range())
        with pytest.raises(errors.InvalidArgumentError, match="requires type btree"):
            listing(cursor, pc)


def test_range_bounds_are_strings(mode: str, identity: Any) -> None:
    with public(mode, identity) as (pc, script, _):
        with pytest.raises(errors.InvalidArgumentError, match="a key"):
            pc.call(lambda c: bucket(pc, "btree").range(gte=5))
        assert script.requests == []


def one(key: str, n: int) -> dict[str, Any]:
    return {"entries": [{"key": key, "value": {"n": n}}]}


def test_first_and_last_return_an_entry_or_none_for_an_empty_bucket(
    mode: str, identity: Any
) -> None:
    empty = reply(
        opcodes.BTREE_FIRST,
        {"entries": [], "message": 'bucket "b" is empty — insert a document first'},
    )
    script = Script(
        {
            opcodes.BTREE_FIRST: [
                reply(opcodes.BTREE_FIRST, {"entries": [{"key": "ada", "value": {"n": 1}}]}),
                empty,
            ],
            opcodes.BTREE_LAST: reply(
                opcodes.BTREE_LAST, {"entries": [{"key": "zed", "value": {"n": 2}}]}
            ),
        }
    )
    with public(mode, identity, script) as (pc, _, _):
        first = pc.call(lambda c: bucket(pc, "btree").first())
        assert (first.key, first.value) == ("ada", {"n": 1})
        assert pc.call(lambda c: bucket(pc, "btree").first()) is None
        last = pc.call(lambda c: bucket(pc, "btree").last())
        assert (last.key, last.value) == ("zed", {"n": 2})


# --- the async cursor ----------------------------------------------------------------------------


def test_the_async_cursor_supports_async_for_batches_and_async_with(identity: Any) -> None:
    script = Script(
        {
            opcodes.SCAN: [
                page(docs(0, 2), more=True, total=3),
                page(docs(2, 1), more=False, total=3),
            ]
        }
    )
    with public("async", identity, script) as (pc, _, _):

        async def go() -> None:
            db = pc.client.database("app")
            async with db.hash("people").find(page_size=2) as cursor:
                batches = [b async for b in cursor.batches()]
            assert batches == [docs(0, 2), docs(2, 1)]
            again = db.hash("people").find(page_size=2)
            assert await again.to_list() == docs(0, 3)

        script.answers[opcodes.SCAN] = [
            page(docs(0, 2), more=True, total=3),
            page(docs(2, 1), more=False, total=3),
            page(docs(0, 2), more=True, total=3),
            page(docs(2, 1), more=False, total=3),
        ]
        pc.loop.run_until_complete(go())


def test_a_cancelled_page_fetch_ends_the_async_walk_and_frees_the_slot(identity: Any) -> None:
    script = Script({opcodes.SCAN: [fakehoard.Hang()]})
    with public("async", identity, script) as (pc, _, _):

        async def go() -> None:
            cursor = pc.client.database("app").hash("people").find(timeout=30.0)
            task = asyncio.ensure_future(cursor.__anext__())
            for _ in range(300):
                if script.of(opcodes.SCAN):
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert [d async for d in cursor] == []
            stats = pc.client.pool_stats()
            assert stats.bulk_in_use == 0 and stats.point_in_use == 0

        pc.loop.run_until_complete(go())


@pytest.mark.parametrize(
    ("facet", "make"),
    [
        ("btree", lambda b: b.find()),
        ("bucket", lambda b: b.search({"Field": "x"})),
        ("btree", lambda b: b.range(gte="a")),
        ("btree", lambda b: b.field("ada", "Purchases").list()),
    ],
    ids=["find", "search", "range", "field list"],
)
def test_a_cursor_verb_on_a_closed_client_raises_before_it_makes_a_cursor(
    mode: str, identity: Any, facet: str, make: Any
) -> None:
    with public(mode, identity, Script()) as (pc, script, _):
        handle = bucket(pc, facet)
        pc.call(lambda c: c.close())
        with pytest.raises(errors.ClientClosedError):
            make(handle)
        assert script.requests == []
