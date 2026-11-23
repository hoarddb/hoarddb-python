# SPDX-License-Identifier: Apache-2.0
"""Type tests for store fields: the element type flows through ``field(..., doc_type=)``, and a
cursor of a field list yields entries."""

from __future__ import annotations

from typing import Any, TypedDict, assert_type

import hoarddb
import hoarddb.aio
from hoarddb import Cursor
from hoarddb.aio import AsyncCursor
from hoarddb.types import FieldBlob, FieldEntry, FieldWriteResult


class Order(TypedDict):
    item: str


def _sync(client: hoarddb.Client) -> None:
    users = client.database("app").hash("users")
    orders = users.field("ada", "Purchases", doc_type=Order)
    assert_type(orders.push({"item": "tea"}), FieldWriteResult)
    assert_type(orders.pop(), list[FieldEntry[Order]])
    assert_type(orders.peek(limit=3), list[FieldEntry[Order]])
    assert_type(orders.put("k", {"item": "tea"}), FieldWriteResult)
    assert_type(orders.get("k"), FieldEntry[Order] | None)
    assert_type(orders.delete("k"), bool)
    assert_type(orders.len(), int)
    assert_type(orders.list(), Cursor[FieldEntry[Order]])
    assert_type(orders.get_blob("k"), FieldBlob | None)
    orders.push({"nope": 1})  # type: ignore[typeddict-item]  # pyright: ignore[reportArgumentType]
    assert_type(users.field("ada", "Friends").get("k"), FieldEntry[dict[str, Any]] | None)


async def _async(client: hoarddb.aio.Client) -> None:
    users = client.database("app").hash("users")
    orders = users.field("ada", "Purchases", doc_type=Order)
    assert_type(await orders.push({"item": "tea"}), FieldWriteResult)
    assert_type(await orders.pop(), list[FieldEntry[Order]])
    assert_type(await orders.len(), int)
    assert_type(orders.list(), AsyncCursor[FieldEntry[Order]])


def test_the_assertions_above_are_checked_by_the_type_checkers() -> None:
    assert callable(_sync) and callable(_async)
