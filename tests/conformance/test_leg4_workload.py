# SPDX-License-Identifier: Apache-2.0
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false, reportUnknownLambdaType=false
"""Leg 4: per store type, write N documents through the public API, read them back, and assert
every value equal field by field and type for type (the server keeps no field order). A driver
that returns nothing with no error fails here: it is counted as a miss.

Both surfaces run the same workload and must produce equal results (spec 3.4).
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

import bsonvec
from realkit import RESULT_LINES, Rig, Servers

pytestmark = pytest.mark.server

N = 200
BLOBS = 40
STORE_TYPES = ("hash", "btree", "fifo", "lifo", "heap", "blob")


def make_document(rng: random.Random, index: int) -> dict[str, Any]:
    """A document that carries every BSON type the driver maps, with values that differ."""
    return {
        "n": index,
        "text": f"héllo wörld {index} ☃ {'x' * rng.randrange(0, 40)}",
        "small": rng.randrange(-1000, 1000),
        "big": rng.choice([-1, 1]) * rng.randrange(2**33, 2**62),
        "ratio": rng.uniform(-1e6, 1e6),
        "flag": bool(index % 2),
        "nothing": None,
        "when": datetime(2020, 1, 1, tzinfo=UTC) + timedelta(milliseconds=rng.randrange(10**11)),
        "bytes": bytes(rng.randrange(256) for _ in range(rng.randrange(0, 24))),
        "uuid": uuid.UUID(int=rng.getrandbits(128), version=4),
        "money": Decimal(f"{rng.randrange(-(10**6), 10**6)}.{rng.randrange(100):02d}"),
        "nested": {"a": index, "deeper": {"list": [index, "two", 3.5, True, None, {"k": "v"}]}},
        "tags": [f"tag{index % 7}", f"tag{index % 11}"],
    }


def shape(value: Any) -> Any:
    """Order-free, type-exact: a document's fields are sorted by name, everything else is
    ``bsonvec.canon`` (bool is not int, a Decimal keeps its exponent, bytes are not str)."""
    if isinstance(value, dict):
        return ("doc", sorted((k, shape(v)) for k, v in value.items()))
    if isinstance(value, list):
        return ("arr", [shape(v) for v in value])
    return bsonvec.canon(value)


def without_id(doc: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in doc.items() if k != "_id_"}


class Counts:
    def __init__(self) -> None:
        self.hits = 0
        self.misses = 0

    def check(self, expected: Any, actual: Any) -> None:
        if actual is not None and shape(expected) == shape(actual):
            self.hits += 1
        else:
            self.misses += 1


def handle(database: str, name: str) -> Callable[[Any], Any]:
    return lambda client: client.database(database).bucket(name)


def workload(rig: Rig, store: str, database: str) -> tuple[Counts, list[Any]]:
    """Write, read back, and return the counts and what was read (for the surface comparison)."""
    rng = random.Random(f"leg4-{store}")  # the same documents on both surfaces
    docs = [make_document(rng, i) for i in range(N)]
    rig.call(lambda c: c.create_database(database))
    rig.call(lambda c: c.database(database).create_bucket("things", store))
    bucket = handle(database, "things")
    counts = Counts()
    read: list[Any] = []

    if store in ("hash", "btree"):
        for i, doc in enumerate(docs):
            rig.call(lambda c, i=i, doc=doc: bucket(c).put(f"k{i:04d}", doc))
        for i, doc in enumerate(docs):
            got = rig.call(lambda c, i=i: bucket(c).get(f"k{i:04d}"))
            counts.check(doc, None if got is None else without_id(got))
            read.append(None if got is None else shape(without_id(got)))
        assert rig.call(lambda c: bucket(c).count()) == N
        found = rig.call(lambda c: bucket(c).find().to_list())
        assert len(found) == N
        if store == "btree":
            entries = rig.call(lambda c: bucket(c).range().to_list())
            assert [e.key for e in entries] == [f"k{i:04d}" for i in range(N)]
            first = rig.call(lambda c: bucket(c).first())
            last = rig.call(lambda c: bucket(c).last())
            assert (first.key, last.key) == ("k0000", f"k{N - 1:04d}")
    elif store in ("fifo", "lifo", "heap"):
        order = list(range(N))
        if store == "heap":
            rng.shuffle(order)
        for i in order:
            priority = i % 5 if store == "heap" else None
            rig.call(lambda c, i=i, p=priority: bucket(c).push(docs[i], priority=p))
        peeked = rig.call(lambda c: bucket(c).peek(limit=3))
        assert len(peeked) == 3
        if store == "fifo":
            expected_order = order
        elif store == "lifo":
            expected_order = order[::-1]
        else:  # highest priority first, and first in first out among equals
            expected_order = sorted(order, key=lambda i: (-(i % 5), order.index(i)))
        for i in expected_order:
            got = rig.call(lambda c: bucket(c).pop())
            counts.check(docs[i], None if got is None else without_id(got))
            read.append(None if got is None else shape(without_id(got)))
        assert rig.call(lambda c: bucket(c).pop()) is None, "a drained queue answers None"
        assert rig.call(lambda c: bucket(c).count()) == 0
    else:  # blob
        payloads = [rng.randbytes(rng.choice([1, 100, 4096, 65536])) for _ in range(BLOBS)]
        for i, data in enumerate(payloads):
            rig.call(
                lambda c, i=i, data=data: bucket(c).put_blob(f"b{i:03d}", data, metadata={"i": i})
            )
        for i, data in enumerate(payloads):
            got = rig.call(lambda c, i=i: bucket(c).get_blob(f"b{i:03d}"))
            counts.check(
                {"data": data, "size": len(data), "metadata": {"i": i}},
                None
                if got is None
                else {"data": got.data, "size": got.size, "metadata": got.metadata},
            )
            read.append(None if got is None else (got.data, got.size, shape(got.metadata)))
        assert rig.call(lambda c: bucket(c).count()) == BLOBS
    return counts, read


@pytest.mark.parametrize("store", STORE_TYPES)
def test_n_documents_read_back_equal_through_both_surfaces(store: str, servers: Servers) -> None:
    node = servers.node()
    outcomes: dict[str, tuple[Counts, list[Any]]] = {}
    for mode in ("sync", "async"):
        rig = servers.rig(mode, node)
        database = f"leg4_{store}_{mode}"
        try:
            outcomes[mode] = workload(rig, store, database)
        finally:
            rig.drop_every_database()
    total = BLOBS if store == "blob" else N
    for mode, (counts, _) in outcomes.items():
        RESULT_LINES.append(f"leg 4 {store:5} {mode:5} hits={counts.hits} misses={counts.misses}")
        assert (counts.hits, counts.misses) == (total, 0), f"{store} via {mode}"
    assert outcomes["sync"][1] == outcomes["async"][1], "the two surfaces must agree exactly"
