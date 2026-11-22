# SPDX-License-Identifier: Apache-2.0
"""Type tests for the query verbs and the cursors (see ``test_doc_type.py`` for the method)."""

from __future__ import annotations

from typing import Any, TypedDict, assert_type

import hoarddb
import hoarddb.aio
from hoarddb.types import Entry


class User(TypedDict):
    name: str
    born: int


def _sync(client: hoarddb.Client) -> None:
    db = client.database("app")
    users = db.hash("users", doc_type=User)
    assert_type(users.find({"name": "Ada"}), hoarddb.Cursor[User])
    assert_type(
        users.find(sort={"born": -1}, projection={"name": 1}, skip=2, limit=3), hoarddb.Cursor[User]
    )
    assert_type(users.find_one({"name": "Ada"}), User | None)
    assert_type(users.count(), int)
    assert_type(users.distinct("name"), list[Any])
    assert_type(users.search({"name": "Ada"}, limit=5), hoarddb.Cursor[User])
    assert_type(users.find().to_list(), list[User])
    assert_type(users.find().total, int | None)
    for user in users.find():
        assert_type(user, User)
    for page in users.find().batches():
        assert_type(page, list[User])
    users.find(sort={"born": 2})  # type: ignore[dict-item]  # pyright: ignore[reportArgumentType]
    users.range()  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
    with users.find() as cursor:
        assert_type(cursor, hoarddb.Cursor[User])

    tree = db.btree("t", doc_type=User)
    assert_type(tree.range(gte="a", lte="m", limit=10), hoarddb.Cursor[Entry[User]])
    assert_type(tree.first(), Entry[User] | None)
    assert_type(tree.last(), Entry[User] | None)
    tree.search({"a": 1})  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
    for entry in tree.range():
        assert_type(entry.key, str)
        assert_type(entry.value, User)

    queue = db.fifo("q")
    assert_type(queue.find(), hoarddb.Cursor[dict[str, Any]])
    assert_type(queue.count(), int)
    queue.search({"a": 1})  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
    queue.range()  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]

    files = db.blob("files")
    assert_type(files.count(), int)
    files.find()  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
    files.distinct("a")  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]

    anything = db.bucket("any")
    assert_type(anything.search({"a": 1}), hoarddb.Cursor[dict[str, Any]])
    assert_type(anything.range(), hoarddb.Cursor[Entry[dict[str, Any]]])


async def _async(client: hoarddb.aio.Client) -> None:
    db = client.database("app")
    users = db.hash("users", doc_type=User)
    assert_type(users.find(), hoarddb.aio.AsyncCursor[User])  # a cursor at once, no await
    assert_type(await users.find().to_list(), list[User])
    assert_type(await users.find_one(), User | None)
    assert_type(await users.count(), int)
    async for user in users.find():
        assert_type(user, User)
    async for page in users.find().batches():
        assert_type(page, list[User])
    async with users.find() as cursor:
        assert_type(cursor, hoarddb.aio.AsyncCursor[User])
    tree = db.btree("t")
    assert_type(tree.range(), hoarddb.aio.AsyncCursor[Entry[dict[str, Any]]])
    assert_type(await tree.first(), Entry[dict[str, Any]] | None)


def test_the_assertions_above_are_checked_by_the_type_checkers() -> None:
    assert callable(_sync) and callable(_async)
