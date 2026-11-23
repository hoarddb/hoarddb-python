# SPDX-License-Identifier: Apache-2.0
"""Surface parity (spec 3.4): the blocking and the asyncio public classes cannot drift.

``SURFACE`` is the single table of the public surface: for every public class, every public
method and whether it does I/O (so is a coroutine on the asyncio surface). The test fails when a
verb exists on one surface and not the other, when one is missing from the table, when the
parameter names, defaults or annotations differ, or when a docstring differs.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

import hoarddb
import hoarddb.aio

COMMON = {"info": True, "stats": True, "drop": True}
KEYED = {
    "put": True,
    "upsert": True,
    "insert": True,
    "insert_many": True,
    "get": True,
    "exists": True,
    "delete": True,
    "update": True,
}
QUEUE = {"push": True, "pop": True, "peek": True}
COUNT = {"count": True}
# find, search and range return a cursor at once (no I/O); the pages are fetched on iteration
FIND = {"find": False, "find_one": True, "distinct": True}
SEARCH = {"search": False}
BTREE = {"range": False, "first": True, "last": True}
FIELDS = {"field": False}
BLOB = {"put_blob": True, "get_blob": True}

# class name -> {method: does I/O}
SURFACE: dict[str, dict[str, bool]] = {
    "Client": {
        "database": False,
        "default_bucket": False,
        "create_database": True,
        "drop_database": True,
        "list_databases": True,
        "ping": True,
        "health": True,
        "list_nodes": True,
        "server_info": False,
        "pool_stats": False,
        "close": True,
    },
    "Database": {
        "create_bucket": True,
        "drop_bucket": True,
        "list_buckets": True,
        "declare_store_fields": True,
        "bucket": False,
        "hash": False,
        "btree": False,
        "fifo": False,
        "lifo": False,
        "heap": False,
        "blob": False,
        "open": True,
    },
    "HashBucket": {**COMMON, **KEYED, **COUNT, **FIND, **SEARCH, **FIELDS},
    "BTreeBucket": {**COMMON, **KEYED, **COUNT, **FIND, **BTREE, **FIELDS},
    "FifoBucket": {**COMMON, **QUEUE, "insert_many": True, **COUNT, **FIND},
    "LifoBucket": {**COMMON, **QUEUE, "insert_many": True, **COUNT, **FIND},
    "HeapBucket": {**COMMON, **QUEUE, "insert_many": True, **COUNT, **FIND},
    "BlobBucket": {**COMMON, "exists": True, "delete": True, **BLOB, **COUNT},
    "Bucket": {**COMMON, **KEYED, **QUEUE, **BLOB, **COUNT, **FIND, **SEARCH, **BTREE, **FIELDS},
    "Cursor": {"batches": False, "to_list": True, "close": False},
    "FieldStore": {
        "field": False,
        "push": True,
        "pop": True,
        "peek": True,
        "put": True,
        "get": True,
        "delete": True,
        "put_blob": True,
        "get_blob": True,
        "len": True,
        "list": False,
    },
}

# Called by the at-fork hook, never by programs.
PROTOCOL = {"mark_forked"}


def _public(cls: type) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, member in inspect.getmembers(cls):
        if name.startswith("_") or (name in PROTOCOL and not callable(member)):
            continue
        if inspect.isfunction(member) or inspect.ismethod(member):
            out[name] = member
    return out


def _norm(text: str) -> str:
    """The asyncio twins of the cursor and its iterator types, by their blocking names."""
    return text.replace("AsyncCursor", "Cursor").replace("AsyncIterator", "Iterator")


def _shape(fn: Any) -> list[tuple[str, str, str, str]]:
    sig = inspect.signature(fn)
    return [
        (p.name, str(p.kind), repr(p.default), _norm(str(p.annotation)))
        for p in sig.parameters.values()
    ]


ASYNC_NAMES = {"Cursor": "AsyncCursor"}


def _pairs() -> list[tuple[str, type, type]]:
    return [(n, getattr(hoarddb, n), getattr(hoarddb.aio, ASYNC_NAMES.get(n, n))) for n in SURFACE]


@pytest.mark.parametrize(("name", "sync_cls", "async_cls"), _pairs(), ids=[p[0] for p in _pairs()])
def test_both_surfaces_have_exactly_the_table_s_methods(
    name: str, sync_cls: type, async_cls: type
) -> None:
    wanted = set(SURFACE[name])
    sync_public = set(_public(sync_cls)) - PROTOCOL
    async_public = set(_public(async_cls)) - PROTOCOL
    assert sync_public == wanted, f"{name}: blocking surface differs from the table"
    assert async_public == wanted, f"{name}: asyncio surface differs from the table"


@pytest.mark.parametrize(("name", "sync_cls", "async_cls"), _pairs(), ids=[p[0] for p in _pairs()])
def test_the_i_o_verbs_are_coroutines_on_the_async_surface_only(
    name: str, sync_cls: type, async_cls: type
) -> None:
    for method, does_io in SURFACE[name].items():
        assert not inspect.iscoroutinefunction(getattr(sync_cls, method)), (name, method)
        assert inspect.iscoroutinefunction(getattr(async_cls, method)) is does_io, (name, method)


@pytest.mark.parametrize(("name", "sync_cls", "async_cls"), _pairs(), ids=[p[0] for p in _pairs()])
def test_parameters_defaults_annotations_and_return_types_are_equal(
    name: str, sync_cls: type, async_cls: type
) -> None:
    for method in SURFACE[name]:
        s, a = getattr(sync_cls, method), getattr(async_cls, method)
        assert _shape(s) == _shape(a), (name, method)
        assert _norm(str(inspect.signature(s).return_annotation)) == _norm(
            str(inspect.signature(a).return_annotation)
        ), (name, method)


@pytest.mark.parametrize(("name", "sync_cls", "async_cls"), _pairs(), ids=[p[0] for p in _pairs()])
def test_every_public_method_has_a_docstring_and_the_twins_agree(
    name: str, sync_cls: type, async_cls: type
) -> None:
    for method in SURFACE[name]:
        s, a = getattr(sync_cls, method), getattr(async_cls, method)
        assert inspect.getdoc(s), f"{name}.{method} has no docstring"
        assert inspect.getdoc(s) == inspect.getdoc(a), (name, method)


def test_the_async_names_match_the_blocking_ones_where_the_spec_gives_two() -> None:
    assert hoarddb.aio.AsyncClient is hoarddb.aio.Client
    for exported in ("TlsOptions", "PoolOptions", "ServerKey", "accept_first_sight"):
        assert getattr(hoarddb, exported) is getattr(hoarddb.aio, exported)


WRITES = {
    "put",
    "upsert",
    "insert",
    "insert_many",
    "delete",
    "update",
    "push",
    "pop",
    "put_blob",
}  # incl. FieldStore


@pytest.mark.parametrize(("name", "sync_cls", "async_cls"), _pairs(), ids=[p[0] for p in _pairs()])
def test_every_write_says_how_to_shield_it_and_that_it_is_never_retried(
    name: str, sync_cls: type, async_cls: type
) -> None:
    for method in SURFACE[name]:
        for cls in (sync_cls, async_cls):
            doc = inspect.getdoc(getattr(cls, method)) or ""
            if method in WRITES:
                assert "asyncio.shield" in doc and "never retries a write" in doc, (name, method)
            else:
                assert "asyncio.shield" not in doc, (name, method)
