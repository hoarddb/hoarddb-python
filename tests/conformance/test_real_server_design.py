# SPDX-License-Identifier: Apache-2.0
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false, reportUnknownLambdaType=false
"""Spec 11.3 tests that only a real server can answer: what the server really says between pages,
about a declaration that differs, and while a cursor is open. Both surfaces."""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

import hoarddb
from hoarddb import errors
from hoarddb._core import opcodes
from realkit import Rig, Servers

pytestmark = pytest.mark.server


def on_surface(
    rig: Rig, blocking: Callable[[Any], Any], asynchronous: Callable[[Any], Awaitable[Any]]
) -> Any:
    return rig.call(blocking if rig.mode == "sync" else asynchronous)


@pytest.fixture
def rig(mode: str, servers: Servers) -> Any:
    chosen = servers.rig(mode, servers.node())
    try:
        yield chosen
    finally:
        chosen.drop_every_database()


def fill_hash(rig: Rig, database: str, count: int, label: str) -> None:
    rig.call(lambda c: c.create_database(database))
    rig.call(lambda c: c.database(database).create_bucket("h", "hash"))
    docs = [{"n": i, "label": label} for i in range(count)]
    rig.call(lambda c: c.database(database).bucket("h").insert_many(docs))


def test_a_bucket_re_created_between_pages_is_not_continued(rig: Rig) -> None:
    fill_hash(rig, "pages", 250, "first")

    def blocking(c: Any) -> Any:
        found = c.database("pages").bucket("h").find(page_size=100)
        walk = iter(found)
        page_one = [next(walk) for _ in range(100)]
        c.database("pages").drop_bucket("h")
        c.database("pages").create_bucket("h", "hash")
        c.database("pages").bucket("h").insert_many(
            [{"n": i, "label": "second"} for i in range(250)]
        )
        rig.tap.clear()
        try:
            list(walk)
        except errors.HoardDBError as caught:
            return page_one, caught
        return page_one, None

    async def asynchronous(c: Any) -> Any:
        found = c.database("pages").bucket("h").find(page_size=100)
        walk = found.__aiter__()
        page_one = [await walk.__anext__() for _ in range(100)]
        await c.database("pages").drop_bucket("h")
        await c.database("pages").create_bucket("h", "hash")
        await (
            c.database("pages")
            .bucket("h")
            .insert_many([{"n": i, "label": "second"} for i in range(250)])
        )
        rig.tap.clear()
        try:
            async for _ in walk:
                pass
        except errors.HoardDBError as caught:
            return page_one, caught
        return page_one, None

    page_one, caught = on_surface(rig, blocking, asynchronous)
    assert len(page_one) == 100 and {d["label"] for d in page_one} == {"first"}
    assert caught is not None, "page 2 must not read the new bucket as a continuation"
    assert "was dropped and re-created" in caught.message
    scans = [f for f in rig.tap.requests() if f.opcode == opcodes.SCAN]
    assert len(scans) == 1, "the refusal ends the walk: no fresh scan may follow it"


def test_if_not_exists_on_an_existing_bucket_checks_the_declaration(rig: Rig) -> None:
    rig.call(lambda c: c.create_database("decl"))
    rig.call(lambda c: c.database("decl").create_bucket("User", "hash"))
    wanted = [hoarddb.StoreSpec("Purchases", "fifo")]
    rig.tap.clear()
    with pytest.raises(errors.ConflictError):
        rig.call(
            lambda c: c.database("decl").create_bucket(
                "User", "hash", if_not_exists=True, stores=wanted
            )
        )
    asked = [f.opcode for f in rig.tap.requests()]
    assert asked.count(opcodes.BUCKET_INFO) == 1, "exactly one info() call to compare the two"
    rig.tap.clear()
    rig.call(lambda c: c.database("decl").create_bucket("User", "hash", if_not_exists=True))
    assert opcodes.BUCKET_INFO not in [f.opcode for f in rig.tap.requests()], (
        "an empty declaration needs no comparison"
    )


def test_a_cursor_pins_no_connection_and_no_thread(rig: Rig) -> None:
    fill_hash(rig, "pins", 350, "x")
    threads = threading.active_count()

    def blocking(c: Any) -> Any:
        in_use: list[int] = []
        for _ in c.database("pins").bucket("h").find(page_size=100):
            in_use.append(c.pool_stats().bulk_in_use)
        return in_use

    async def asynchronous(c: Any) -> Any:
        in_use: list[int] = []
        async for _ in c.database("pins").bucket("h").find(page_size=100):
            in_use.append(c.pool_stats().bulk_in_use)
        return in_use

    seen = on_surface(rig, blocking, asynchronous)
    assert len(seen) == 350 and set(seen) == {0}, "no bulk connection is held between documents"
    assert rig.call(lambda c: c.pool_stats().bulk_in_use) == 0
    assert threading.active_count() == threads, "a cursor must not leave a thread behind"


def test_range_paging_is_stable_under_inserts(rig: Rig) -> None:
    rig.call(lambda c: c.create_database("ranges"))
    rig.call(lambda c: c.database("ranges").create_bucket("t", "btree"))
    original = [f"k{i:04d}" for i in range(0, 600, 2)]  # even numbers: odd ones are inserted
    rig.call(
        lambda c: (
            c.database("ranges")
            .bucket("t")
            .insert_many([{"_id_": key, "v": 1} for key in original])
        )
    )

    def blocking(c: Any) -> Any:
        bucket = c.database("ranges").bucket("t")
        keys: list[str] = []
        for entry in bucket.range(page_size=50):
            keys.append(entry.key)
            if len(keys) == 60:  # between pages: insert behind and ahead of the boundary
                bucket.put("k0001", {"v": 2})
                bucket.put("k0599", {"v": 2})
        return keys

    async def asynchronous(c: Any) -> Any:
        bucket = c.database("ranges").bucket("t")
        keys: list[str] = []
        async for entry in bucket.range(page_size=50):
            keys.append(entry.key)
            if len(keys) == 60:
                await bucket.put("k0001", {"v": 2})
                await bucket.put("k0599", {"v": 2})
        return keys

    keys = on_surface(rig, blocking, asynchronous)
    assert keys == sorted(set(keys)), "in key order and never twice"
    assert set(original) <= set(keys), "no key that was there all along may be skipped"
    assert "k0599" in keys and "k0001" not in keys
