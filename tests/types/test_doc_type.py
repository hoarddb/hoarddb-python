# SPDX-License-Identifier: Apache-2.0
"""Type tests, the first file of ``tests/types`` (spec 4.4, 11.4): written before any facet.

``mypy --strict`` and ``pyright`` check this file. ``assert_type`` pins what a checker reveals; a
line that must be an error carries a ``type: ignore`` and a ``pyright: ignore``, and both checkers
report an *unused* ignore, so the day the error disappears this file fails the gate.
"""

from __future__ import annotations

from typing import Any, TypedDict, assert_type

import hoarddb
import hoarddb.aio


class User(TypedDict):
    name: str
    born: int


def _sync_reveals(client: hoarddb.Client) -> None:
    db = client.database("app")
    # an accessor without doc_type reveals dict[str, Any]; with it, the given type
    assert_type(db.hash("users"), hoarddb.HashBucket[dict[str, Any]])
    assert_type(db.hash("users", doc_type=User), hoarddb.HashBucket[User])
    assert_type(db.btree("t"), hoarddb.BTreeBucket[dict[str, Any]])
    assert_type(db.btree("t", doc_type=User), hoarddb.BTreeBucket[User])
    assert_type(db.fifo("q"), hoarddb.FifoBucket[dict[str, Any]])
    assert_type(db.lifo("s"), hoarddb.LifoBucket[dict[str, Any]])
    assert_type(db.heap("h", doc_type=User), hoarddb.HeapBucket[User])
    assert_type(db.bucket("any"), hoarddb.Bucket[dict[str, Any]])
    assert_type(db.bucket("any", doc_type=User), hoarddb.Bucket[User])
    assert_type(db.blob("b"), hoarddb.BlobBucket)
    # create_bucket is typed by the literal and by the member
    assert_type(db.create_bucket("a", "hash"), hoarddb.HashBucket[dict[str, Any]])
    assert_type(db.create_bucket("a", hoarddb.StoreType.HASH), hoarddb.HashBucket[dict[str, Any]])
    assert_type(db.create_bucket("a", "btree"), hoarddb.BTreeBucket[dict[str, Any]])
    assert_type(db.create_bucket("j", "fifo"), hoarddb.FifoBucket[dict[str, Any]])
    assert_type(db.create_bucket("j", hoarddb.StoreType.FIFO), hoarddb.FifoBucket[dict[str, Any]])
    assert_type(db.create_bucket("j", "lifo"), hoarddb.LifoBucket[dict[str, Any]])
    assert_type(db.create_bucket("j", "heap"), hoarddb.HeapBucket[dict[str, Any]])
    assert_type(db.create_bucket("j", "blob"), hoarddb.BlobBucket)


async def _async_reveals(client: hoarddb.aio.Client) -> None:
    db = client.database("app")
    assert_type(db.hash("users"), hoarddb.aio.HashBucket[dict[str, Any]])
    assert_type(db.hash("users", doc_type=User), hoarddb.aio.HashBucket[User])
    assert_type(db.blob("b"), hoarddb.aio.BlobBucket)
    assert_type(await db.create_bucket("j", "fifo"), hoarddb.aio.FifoBucket[dict[str, Any]])
    assert_type(
        await db.create_bucket("j", hoarddb.StoreType.HEAP), hoarddb.aio.HeapBucket[dict[str, Any]]
    )


def test_the_checks_above_are_type_level_and_the_module_imports() -> None:
    # The assertions cost nothing at runtime and are never called: a checker proves them.
    assert callable(_sync_reveals) and callable(_async_reveals)
