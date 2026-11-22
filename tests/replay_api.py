# SPDX-License-Identifier: Apache-2.0
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false, reportUnknownLambdaType=false
"""The adapters that turn a vector's request into a call of the public API.

Each adapter takes the client and the vector's request document (as Python values) and returns
what the public verb returned, or the string ``RAW`` when the public API deliberately cannot send
that request (a store type it refuses locally, ``schemajson``, which a v1 driver never exposes):
the replay then sends the vector's document through the private raw sender instead.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

import hoarddb
from hoarddb import _wire

RAW = "raw"
Adapter = Callable[[Any, dict[str, Any], dict[str, Any]], Any]
STORE_TYPES = {"hash", "btree", "fifo", "lifo", "heap", "blob"}


def to_store(doc: dict[str, Any]) -> hoarddb.StoreSpec:
    return hoarddb.StoreSpec(
        doc["name"], doc["engine"], tuple(to_store(s) for s in doc.get("fields", []))
    )


def to_index(spec: Any) -> hoarddb.IndexSpec:
    if isinstance(spec, str):
        return hoarddb.IndexSpec([spec])
    return hoarddb.IndexSpec(
        spec["fields"],
        unique=spec.get("unique", False),
        name=spec.get("name"),
        expire_after_seconds=spec.get("expire_after_seconds"),
    )


def to_field(doc: dict[str, Any]) -> hoarddb.FieldSpec:
    return hoarddb.FieldSpec(
        doc["name"],
        doc["type"],
        required=doc.get("required", False),
        members=tuple(to_field(m) for m in doc.get("members", [])),
        elem=to_field(doc["elem"]) if "elem" in doc else None,
    )


def create_bucket(c: Any, p: dict[str, Any], vec: dict[str, Any]) -> Any:
    if p.get("schemajson") or p["type"] not in STORE_TYPES:
        return RAW
    options = p.get("options") or {}
    types = None
    if "fields" in options:
        types = hoarddb.FieldTypes(
            [to_field(f) for f in options["fields"]], mode=options.get("field_types")
        )
    exists = "already exists" in str(vec["response"].get("payload_json", {}).get("message", ""))
    return c.database(p["database"]).create_bucket(
        p["name"],
        p["type"],
        if_not_exists=exists and vec["response"]["opcode_name"] != "ERROR",
        encrypted=p.get("encrypted", False),
        replication_factor=p.get("replication_factor"),
        stores=[to_store(s) for s in options.get("stores", [])],
        indexes=[to_index(i) for i in options.get("indices", [])],
        field_types=types,
    )


def _bucket(c: Any, p: dict[str, Any]) -> Any:
    return c.database(p["database"]).bucket(p["bucket"])


def delete(c: Any, p: dict[str, Any], vec: dict[str, Any]) -> Any:
    if p.get("expire_if"):
        return RAW  # conditional delete is an operator's; the public verb does not offer it
    return _bucket(c, p).delete(p["key"], write_concern=p.get("writeconcern") or None)


def update(c: Any, p: dict[str, Any], vec: dict[str, Any]) -> Any:
    names = {"set": "$set", "unset": "$unset", "inc": "$inc", "push": "$push",
             "addtoset": "$addToSet", "mul": "$mul", "min": "$min", "max": "$max",
             "rename": "$rename"}  # fmt: skip
    ops = {names[k]: v for k, v in p.items() if k in names and v}
    if p.get("filter") or p.get("multi"):
        return RAW  # there is no update by filter in v1
    return _bucket(c, p).update(p["key"], ops, write_concern=p.get("writeconcern") or None)


def _wc(p: dict[str, Any]) -> Any:
    return p.get("writeconcern") or None


def _unknown_wc(p: dict[str, Any]) -> bool:
    """A write concern the driver refuses locally; the vector wants the server's own refusal."""
    return p.get("writeconcern") not in (None, "", "one", "majority", "all")


def _scan(c: Any, p: dict[str, Any], vec: dict[str, Any]) -> Any:
    sort = dict(p.get("sort") or {}) or None
    cursor = _bucket(c, p).find(
        p.get("filter") or None,
        sort=sort,
        projection=p.get("projection") or None,
        skip=p.get("skip") or 0,
        limit=p.get("limit") or None,
    )
    return cursor.to_list()  # a list, or a coroutine on the asyncio surface


def _field(c: Any, p: dict[str, Any]) -> Any:
    return _bucket(c, p).field(p["key"], p["field"])


def field_put(c: Any, p: dict[str, Any], vec: dict[str, Any]) -> Any:
    store = _field(c, p)
    if "content" in p:  # a blob field: the metadata document rides in ``data``
        return store.put_blob(p["element"], p["content"], metadata=p.get("data") or None)
    return store.put(p["element"], p["data"])


def field_get(c: Any, p: dict[str, Any], vec: dict[str, Any]) -> Any:
    store = _field(c, p)
    if "content" in (vec["response"].get("payload_json") or {}):
        return store.get_blob(p["element"])
    return store.get(p["element"])


ADAPTERS: dict[str, Adapter] = {
    "PING": lambda c, p, v: c.ping(),
    "CREATE_DB": lambda c, p, v: c.create_database(p["name"]),
    "DELETE_DB": lambda c, p, v: c.drop_database(p["name"]),
    "LIST_DATABASES": lambda c, p, v: c.list_databases(),
    "CREATE_BUCKET": create_bucket,
    "DELETE_BUCKET": lambda c, p, v: c.database(p["database"]).drop_bucket(p["name"]),
    "LIST_BUCKETS": lambda c, p, v: c.database(p["database"]).list_buckets(),
    "DECLARE_STORE_FIELDS": lambda c, p, v: c.database(p["database"]).declare_store_fields(
        p["name"], [to_store(s) for s in p["stores"]]
    ),
    "BUCKET_STATS": lambda c, p, v: c.database(p["database"]).bucket(p["bucket"]).stats(),
    "BUCKET_INFO": lambda c, p, v: c.database(p["database"]).bucket(p["bucket"]).info(),
    "HEALTH_CHECK": lambda c, p, v: c.health(),
    # the history flag is an operator's; the public verb does not offer it
    "LIST_NODES": lambda c, p, v: RAW if p.get("history") else c.list_nodes(),
    "PUT": lambda c, p, v: (
        _bucket(c, p).put(p["key"], p["data"], write_concern=_wc(p))
        if p.get("key")
        else _bucket(c, p).insert(p["data"], write_concern=_wc(p))
    ),
    "GET": lambda c, p, v: _bucket(c, p).get(p["key"]),
    "CONSISTENT_GET": lambda c, p, v: _bucket(c, p).get(p["key"], consistent=True),
    "EXISTS": lambda c, p, v: _bucket(c, p).exists(p["key"]),
    "DELETE": delete,
    "UPDATE": update,
    "BATCH_PUT": lambda c, p, v: _bucket(c, p).insert_many(p["data"], write_concern=_wc(p)),
    "FIFO_PUSH": lambda c, p, v: _bucket(c, p).push(
        p["data"], priority=p.get("priority"), write_concern=_wc(p)
    ),
    "FIFO_POP": lambda c, p, v: _bucket(c, p).pop(write_concern=_wc(p)),
    "FIFO_PEEK": lambda c, p, v: _bucket(c, p).peek(limit=p.get("limit") or 1),
    "BLOB_WRITE": lambda c, p, v: _bucket(c, p).put_blob(
        p["key"], p["data"], metadata=p.get("metadata") or None, write_concern=_wc(p)
    ),
    "BLOB_READ": lambda c, p, v: _bucket(c, p).get_blob(p["key"]),
    "SCAN": _scan,
    "SEARCH": lambda c, p, v: (
        _bucket(c, p).search(p["filter"], limit=p.get("limit") or 100).to_list()
    ),
    "COUNT": lambda c, p, v: _bucket(c, p).count(p.get("filter") or None),
    "DISTINCT": lambda c, p, v: _bucket(c, p).distinct(p["field"], p.get("filter") or None),
    "BTREE_RANGE": lambda c, p, v: (
        _bucket(c, p)
        .range(gte=p.get("low") or None, lte=p.get("high") or None, limit=p.get("limit") or None)
        .to_list()
    ),
    "BTREE_FIRST": lambda c, p, v: _bucket(c, p).first(),
    "BTREE_LAST": lambda c, p, v: _bucket(c, p).last(),
    "FIELD_PUSH": lambda c, p, v: _field(c, p).push(p["data"], priority=p.get("priority")),
    "FIELD_POP": lambda c, p, v: _field(c, p).pop(),
    "FIELD_PEEK": lambda c, p, v: _field(c, p).peek(limit=p.get("limit") or 1),
    "FIELD_PUT": field_put,
    "FIELD_GET": field_get,
    "FIELD_DELETE": lambda c, p, v: _field(c, p).delete(p["element"]),
    "FIELD_LEN": lambda c, p, v: _field(c, p).len(),
    "FIELD_LIST": lambda c, p, v: _field(c, p).list(limit=p.get("limit") or None).to_list(),
    # the other opcode families, and the rest of an operator's tools: the raw sender
    "BTREE_INSERT": lambda c, p, v: RAW,
    "BTREE_LOOKUP": lambda c, p, v: RAW,
    "LIFO_PUSH": lambda c, p, v: RAW,
    "HEAP_PUSH": lambda c, p, v: RAW,
    "LIFO_POP": lambda c, p, v: RAW,
    "HEAP_POP": lambda c, p, v: RAW,
    "LIFO_PEEK": lambda c, p, v: RAW,
}


def send_raw(c: Any, vec: dict[str, Any], doc: dict[str, Any]) -> Any:
    """The vector's own document through the private raw sender (blocking or asyncio)."""
    request = vec["request"]
    if hasattr(c, "_ctx") and inspect.iscoroutinefunction(type(c).ping):
        return _wire.asend(c, request["opcode"], doc, kind=_kind(request["opcode_name"]))
    return _wire.send(c, request["opcode"], doc, kind=_kind(request["opcode_name"]))


_WRITES = {"PUT", "DELETE", "UPDATE", "BATCH_PUT", "BTREE_INSERT", "BLOB_WRITE"}


def _kind(opcode_name: str) -> Any:
    if opcode_name in ("CREATE_BUCKET", "CREATE_DB", "DELETE_BUCKET", "DELETE_DB"):
        return "DDL"
    if opcode_name in _WRITES or opcode_name.endswith(("_PUSH", "_POP")):
        return "WRITE"
    return "READ"


def _expect_list_databases(result: Any, j: dict[str, Any]) -> None:
    assert [(d.name, d.bucket_count) for d in result] == [
        (d["name"], d.get("bucketcount", 0)) for d in (j.get("databases") or [])
    ]


def _expect_list_buckets(result: Any, j: dict[str, Any]) -> None:
    assert [(b.name, b.type, b.entries) for b in result] == [
        (b["name"], b["type"], b.get("entries", 0)) for b in (j.get("buckets") or [])
    ]


def _expect_stats(result: Any, j: dict[str, Any]) -> None:
    assert (result.type, result.entries) == (j["type"], j.get("entries", 0))
    assert [s.name for s in result.stores] == [s["name"] for s in (j.get("stores") or [])]
    assert list(result.indexes) == (j.get("indexes") or [])


def _expect_info(result: Any, j: dict[str, Any]) -> None:
    row = j["bucket"]
    assert (result.name, result.type, result.entries) == (row["name"], row["type"], row["entries"])
    assert list(result.stores) == (row.get("stores") or [])
    assert [(f.name, f.engine) for f in result.store_fields] == [
        (f["name"], f["engine"]) for f in (j.get("store_fields") or [])
    ]
    assert result.replication_factor == row.get("replication_factor")


def _expect_health(result: Any, j: dict[str, Any]) -> None:
    assert (result.node_id, result.version) == (j["nodeid"], j["version"])
    assert result.healthy is j["healthy"]


def _expect_nodes(result: Any, j: dict[str, Any]) -> None:
    assert [n.node_id for n in result] == [n["nodeid"] for n in (j.get("nodes") or [])]


EXPECT: dict[str, Callable[[Any, dict[str, Any]], None]] = {
    "LIST_DATABASES": _expect_list_databases,
    "LIST_BUCKETS": _expect_list_buckets,
    "BUCKET_STATS": _expect_stats,
    "BUCKET_INFO": _expect_info,
    "HEALTH_CHECK": _expect_health,
    "LIST_NODES": _expect_nodes,
}


def _same_doc(result: Any, j: dict[str, Any]) -> None:
    data = j.get("data")
    if j.get("found") is True:
        assert result == data
    else:
        assert result is None


def _expect_put(result: Any, j: dict[str, Any]) -> None:
    assert result.id == j["id"] and (result.seq or 0) == j.get("seq", 0)


def _expect_exists(result: Any, j: dict[str, Any]) -> None:
    assert result is bool(j.get("exists"))


def _expect_delete(result: Any, j: dict[str, Any]) -> None:
    assert result is bool(j["success"])


def _expect_update(result: Any, j: dict[str, Any]) -> None:
    assert result == (j["doc"] if j["success"] else None)


def _expect_batch(result: Any, j: dict[str, Any]) -> None:
    assert (result.ids, result.count) == (j.get("ids") or [], j.get("count", 0))


def _expect_pop(result: Any, j: dict[str, Any]) -> None:
    assert result == (j["data"] if j.get("found") else None)


def _expect_peek(result: Any, j: dict[str, Any]) -> None:
    assert result == (j.get("items") or [])


def _expect_blob_write(result: Any, j: dict[str, Any]) -> None:
    assert (result.key, result.size) == (j["key"], j["size"])


def _expect_blob_read(result: Any, j: dict[str, Any]) -> None:
    if j.get("found"):
        assert (result.data, result.size, result.metadata) == (
            j["data"], j["size"], j.get("metadata") or {},
        )  # fmt: skip
    else:
        assert result is None


EXPECT.update(
    {
        "PUT": _expect_put,
        "GET": _same_doc,
        "CONSISTENT_GET": _same_doc,
        "EXISTS": _expect_exists,
        "DELETE": _expect_delete,
        "UPDATE": _expect_update,
        "BATCH_PUT": _expect_batch,
        "FIFO_PUSH": _expect_put,
        "FIFO_POP": _expect_pop,
        "FIFO_PEEK": _expect_peek,
        "BLOB_WRITE": _expect_blob_write,
        "BLOB_READ": _expect_blob_read,
    }
)


def _with_wc_check(adapter: Adapter) -> Adapter:
    def wrapped(c: Any, p: dict[str, Any], vec: dict[str, Any]) -> Any:
        return RAW if _unknown_wc(p) else adapter(c, p, vec)

    return wrapped


for _name in ("PUT", "DELETE", "UPDATE", "BATCH_PUT", "FIFO_PUSH", "FIFO_POP", "BLOB_WRITE"):
    ADAPTERS[_name] = _with_wc_check(ADAPTERS[_name])


def _expect_scan(result: Any, j: dict[str, Any]) -> None:
    assert result == (j.get("documents") or [])


def _expect_search(result: Any, j: dict[str, Any]) -> None:
    assert result == (j.get("results") or [])


def _expect_count(result: Any, j: dict[str, Any]) -> None:
    assert result == j["count"]


def _expect_distinct(result: Any, j: dict[str, Any]) -> None:
    assert result == (j.get("values") or [])


def _expect_range(result: Any, j: dict[str, Any]) -> None:
    assert [(e.key, e.value) for e in result] == [
        (e["key"], e["value"]) for e in (j.get("entries") or [])
    ]


def _expect_end(result: Any, j: dict[str, Any]) -> None:
    entries = j.get("entries") or []
    if entries:
        assert (result.key, result.value) == (entries[0]["key"], entries[0]["value"])
    else:
        assert result is None


EXPECT.update(
    {
        "SCAN": _expect_scan,
        "SEARCH": _expect_search,
        "COUNT": _expect_count,
        "DISTINCT": _expect_distinct,
        "BTREE_RANGE": _expect_range,
        "BTREE_FIRST": _expect_end,
        "BTREE_LAST": _expect_end,
    }
)


def _expect_field_push(result: Any, j: dict[str, Any]) -> None:
    assert (result.entry_id, result.priority) == (j["entry_id"], j.get("priority"))


def _expect_field_pop(result: Any, j: dict[str, Any]) -> None:
    if j.get("found"):
        assert result.element == j["element"]
    else:
        assert result is None


def _expect_field_peek(result: Any, j: dict[str, Any]) -> None:
    assert [e.element for e in result] == [i["element"] for i in j.get("items") or []]


def _expect_field_put(result: Any, j: dict[str, Any]) -> None:
    assert (result.element_key, result.blob_id) == (j["element_key"], j.get("blob_id", ""))


def _expect_field_get(result: Any, j: dict[str, Any]) -> None:
    if not j.get("found"):
        assert result is None
    elif "content" in j:
        assert (result.content, result.metadata) == (j["content"], j["element"])
    else:
        assert (result.element, result.element_key) == (j["element"], j["element_key"])


def _expect_field_list(result: Any, j: dict[str, Any]) -> None:
    assert [e.element for e in result] == [e["element"] for e in j.get("entries") or []]


EXPECT.update(
    {
        "FIELD_PUSH": _expect_field_push,
        "FIELD_POP": _expect_field_pop,
        "FIELD_PEEK": _expect_field_peek,
        "FIELD_PUT": _expect_field_put,
        "FIELD_GET": _expect_field_get,
        "FIELD_DELETE": lambda r, j: _expect_delete(r, j),
        "FIELD_LEN": lambda r, j: _expect_len(r, j),
        "FIELD_LIST": _expect_field_list,
    }
)


def _expect_len(result: Any, j: dict[str, Any]) -> None:
    assert result == j["len"]
