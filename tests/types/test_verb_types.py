# SPDX-License-Identifier: Apache-2.0
"""Type tests for the verbs: a typed facet offers only the verbs its store type answers, and the
document type flows through. ``# type: ignore`` / ``# pyright: ignore`` lines are expected errors;
both checkers report an *unused* ignore, so a verb that appears where it must not fails the gate."""

from __future__ import annotations

from typing import Any, TypedDict, assert_type

import hoarddb
import hoarddb.aio
from hoarddb.types import BatchResult, Blob, BlobWriteResult, WriteResult


class User(TypedDict):
    name: str
    born: int


def _sync(client: hoarddb.Client) -> None:
    db = client.database("app")
    users = db.hash("users", doc_type=User)
    assert_type(users.get("ada"), User | None)
    assert_type(users.put("ada", {"name": "Ada", "born": 1815}), WriteResult)
    assert_type(users.insert({"name": "Ada", "born": 1815}), WriteResult)
    assert_type(users.insert_many([{"name": "Ada", "born": 1815}]), BatchResult)
    assert_type(users.update("ada", {"$set": {"born": 1816}, "$inc": {"visits": 1}}), User | None)
    assert_type(users.exists("ada"), bool)
    assert_type(users.delete("ada"), bool)
    assert_type(users.get("ada", consistent=True), User | None)
    users.put("ada", {"name": "Ada"})  # type: ignore[typeddict-item]  # pyright: ignore[reportArgumentType]
    users.update("ada", {"$nope": {}})  # type: ignore[typeddict-unknown-key]  # pyright: ignore[reportArgumentType]
    users.push({"name": "Ada", "born": 1})  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]

    plain = db.hash("plain")
    assert_type(plain.get("k"), dict[str, Any] | None)

    jobs = db.fifo("jobs")
    assert_type(jobs.push({"n": 1}), WriteResult)
    assert_type(jobs.push({"n": 1}, priority=3), WriteResult)
    assert_type(jobs.pop(), dict[str, Any] | None)
    assert_type(jobs.peek(limit=3), list[dict[str, Any]])
    assert_type(jobs.insert_many([{"n": 1}]), BatchResult)
    jobs.get("x")  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
    jobs.put("x", {})  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
    jobs.update("x", {})  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
    jobs.exists("x")  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]

    stack, heap = db.lifo("s"), db.heap("h")
    assert_type(stack.pop(), dict[str, Any] | None)
    assert_type(heap.pop(), dict[str, Any] | None)
    stack.get("x")  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
    heap.get("x")  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]

    tree = db.btree("t")
    assert_type(tree.get("k"), dict[str, Any] | None)
    tree.push({})  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]

    files = db.blob("files")
    assert_type(files.put_blob("k", b"x", metadata={"a": 1}), BlobWriteResult)
    assert_type(files.get_blob("k"), Blob | None)
    assert_type(files.exists("k"), bool)
    assert_type(files.delete("k"), bool)
    files.get("k")  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
    files.put("k", {})  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
    users.put_blob("k", b"x")  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]

    anything = db.bucket("any")  # the generic form has every verb
    assert_type(anything.get("k"), dict[str, Any] | None)
    assert_type(anything.pop(), dict[str, Any] | None)
    assert_type(anything.put_blob("k", b""), BlobWriteResult)

    users.put("ada", {"name": "Ada", "born": 1}, write_concern="majority")
    users.put("ada", {"name": "Ada", "born": 1}, write_concern="most")  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]


async def _async(client: hoarddb.aio.Client) -> None:
    db = client.database("app")
    users = db.hash("users", doc_type=User)
    assert_type(await users.get("ada"), User | None)
    assert_type(await users.put("ada", {"name": "Ada", "born": 1}), WriteResult)
    assert_type(await users.update("ada", {"$set": {"born": 2}}), User | None)
    jobs = db.fifo("jobs")
    assert_type(await jobs.pop(), dict[str, Any] | None)
    assert_type(await jobs.peek(), list[dict[str, Any]])
    jobs.get("x")  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
    files = db.blob("files")
    assert_type(await files.get_blob("k"), Blob | None)


def test_the_assertions_above_are_checked_by_the_type_checkers() -> None:
    assert callable(_sync) and callable(_async)
