# SPDX-License-Identifier: Apache-2.0
"""P11: ``Client``, ``Database``, DDL, ``info``/``stats``, handles: both surfaces, one table."""

from __future__ import annotations

import asyncio
import gc
import inspect
import os
import sys
import threading
import warnings
from typing import Any

import pytest

import fakehoard
import hoarddb
import hoarddb.aio
from clientkit import PC, Script, ok, public, reply
from hoarddb import errors
from hoarddb._core import opcodes
from hoarddb.options import PoolOptions

BUCKET_CREATED = ok(opcodes.CREATE_BUCKET, 'bucket "b" created (type: hash)')


# --- connecting ----------------------------------------------------------------------------------


def test_connect_authenticates_once_and_learns_the_server(mode: str, identity: Any) -> None:
    with public(mode, identity) as (pc, _, server):
        assert [f.opcode for f in server.frames] == [opcodes.AUTH]
        info = pc.client.server_info()
        assert info.version == "0.0.0-test" and info.protocol_version == 1
        assert "auth_token" in info.capabilities and info.password_min_length == 12
        stats = pc.client.pool_stats()
        assert stats.dials == 1 and stats.point_idle == 1 and not stats.closed


def test_a_client_s_repr_hides_the_password_and_shows_its_state(mode: str, identity: Any) -> None:
    with public(mode, identity) as (pc, _, _):
        text = repr(pc.client)
        assert "pw" not in text and "admin:***@" in text and "open" in text


def test_no_password_is_refused_before_any_dial(
    mode: str, identity: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HOARDDB_ROOT_PASSWORD", raising=False)
    with fakehoard.FakeHoard(identity) as server:
        with pytest.raises(errors.ConfigError) as caught:
            _connect(mode, f"hoarddb://admin@127.0.0.1:{server.port}", identity)
        assert caught.value.code == "NO_PASSWORD"
        assert server.connections == []


def test_a_wrong_password_fails_at_connect(mode: str, identity: Any) -> None:
    with fakehoard.FakeHoard(identity, on_auth=lambda c, d: [fakehoard.auth_fail(1)]) as server:
        with pytest.raises(errors.AuthenticationError) as caught:
            _connect(mode, f"hoarddb://admin:bad@127.0.0.1:{server.port}", identity)
        assert caught.value.reason == "CREDENTIALS" and "bad" not in str(caught.value)
        assert len(server.connections) == 1


def _connect(mode: str, uri: str, identity: Any) -> Any:
    tls = hoarddb.TlsOptions(fingerprints=[identity.fingerprint])
    if mode == "sync":
        return hoarddb.connect(uri, tls=tls)

    async def go() -> Any:
        return await hoarddb.aio.connect(uri, tls=tls)

    return asyncio.run(go())


def test_the_default_uri_and_environment_are_read(
    identity: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with fakehoard.FakeHoard(identity) as server:
        monkeypatch.setenv("HOARDDB_URI", f"hoarddb://127.0.0.1:{server.port}/app/users")
        monkeypatch.setenv("HOARDDB_ROOT_PASSWORD", "from-env")
        client = hoarddb.connect(tls=hoarddb.TlsOptions(fingerprints=[identity.fingerprint]))
        try:
            assert server.connections[0].auth_requests[0]["password"] == "from-env"
            assert server.connections[0].auth_requests[0]["username"] == "admin"
            assert client.database().name == "app"
            assert client.default_bucket().name == "users"
        finally:
            client.close()


# --- the server calls ----------------------------------------------------------------------------


def test_ping_health_and_list_nodes(mode: str, identity: Any) -> None:
    script = Script(
        {
            opcodes.HEALTH_CHECK: lambda doc: reply(
                opcodes.GET_RESPONSE,
                {"nodeid": "n1", "uptime": "3s", "version": "9", "bucketcount": 2, "healthy": True,
                 "membership": "confirmed", "host": {"cpus": 4}},
            ),
        }
    )  # fmt: skip
    with public(mode, identity, script) as (pc, _, _):
        assert pc.call(lambda c: c.ping()) is None
        h = pc.call(lambda c: c.health())
        assert (h.node_id, h.version, h.bucket_count, h.healthy) == ("n1", "9", 2, True)
        assert h.membership == "confirmed" and h.host == {"cpus": 4}


def test_list_nodes_decodes_every_node(mode: str, identity: Any) -> None:
    nodes = {"nodes": [{"nodeid": "a", "addr": "192.0.2.1:7434", "fingerprint": "SHA256:x",
                        "status": "active"}]}  # fmt: skip
    script = Script({opcodes.LIST_NODES: reply(opcodes.GET_RESPONSE, nodes)})
    with public(mode, identity, script) as (pc, _, _):
        (node,) = pc.call(lambda c: c.list_nodes())
        assert (node.node_id, node.addr, node.status) == ("a", "192.0.2.1:7434", "active")


def test_database_calls(mode: str, identity: Any) -> None:
    script = Script(
        {
            opcodes.CREATE_DB: [
                ok(opcodes.CREATE_DB, "created"),
                [fakehoard.error(2, 'database "x" already exists')],
                [fakehoard.error(2, 'database "x" already exists')],
            ],
            opcodes.DELETE_DB: ok(opcodes.DELETE_DB, "dropped"),
            opcodes.LIST_DATABASES: reply(
                opcodes.LIST_DATABASES, {"databases": [{"name": "app", "bucketcount": 3}]}
            ),
        }
    )
    with public(mode, identity, script) as (pc, _, _):
        pc.call(lambda c: c.create_database("x"))
        with pytest.raises(errors.AlreadyExistsError):
            pc.call(lambda c: c.create_database("x"))
        pc.call(lambda c: c.create_database("x", if_not_exists=True))  # accepted, not an error
        pc.call(lambda c: c.drop_database("x"))
        (info,) = pc.call(lambda c: c.list_databases())
        assert (info.name, info.bucket_count) == ("app", 3)
        assert script.of(opcodes.DELETE_DB) == [{"name": "x"}]


def test_an_empty_database_name_is_refused_before_anything_is_sent(
    mode: str, identity: Any
) -> None:
    with public(mode, identity) as (pc, script, _):
        with pytest.raises(errors.InvalidArgumentError, match="Nothing was sent"):
            pc.call(lambda c: c.create_database(""))
        assert script.requests == []


# --- create_bucket -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("word", "facet"),
    [("hash", "HashBucket"), ("btree", "BTreeBucket"), ("fifo", "FifoBucket"),
     ("lifo", "LifoBucket"), ("heap", "HeapBucket"), ("blob", "BlobBucket")],
)  # fmt: skip
def test_create_bucket_returns_the_facet_of_the_type_and_sends_its_word(
    mode: str, identity: Any, word: str, facet: str
) -> None:
    script = Script({opcodes.CREATE_BUCKET: ok(opcodes.CREATE_BUCKET, f"created (type: {word})")})
    with public(mode, identity, script) as (pc, _, _):
        for spelling in (word, hoarddb.StoreType(word)):

            def make(c: Any, s: Any = spelling) -> Any:
                return c.database("app").create_bucket("b", s)

            made = pc.call(make)
            assert type(made).__name__ == facet and made.name == "b" and made.database == "app"
        sent = script.of(opcodes.CREATE_BUCKET)
        assert [d["type"] for d in sent] == [word, word]
        assert sent[0] == {"database": "app", "name": "b", "type": word}  # empty fields omitted


@pytest.mark.parametrize("bad", ["fifoStore", "queue", "HASH", "", 7, None])
def test_an_unknown_store_type_names_the_six_and_sends_nothing(
    mode: str, identity: Any, bad: Any
) -> None:
    with public(mode, identity) as (pc, script, _):
        with pytest.raises(errors.InvalidArgumentError) as caught:
            pc.call(lambda c: c.database("app").create_bucket("b", bad))
        assert "hash, btree, fifo, lifo, heap, blob" in str(caught.value)
        assert script.requests == []


def test_create_bucket_sends_stores_indexes_and_field_types(mode: str, identity: Any) -> None:
    script = Script({opcodes.CREATE_BUCKET: BUCKET_CREATED})
    stores = [
        hoarddb.StoreSpec("Purchases", "fifo"),
        hoarddb.StoreSpec("Friends", hoarddb.StoreType.HASH,
                          stores=[hoarddb.StoreSpec("Inner", "blob")]),
    ]  # fmt: skip
    indexes = [
        hoarddb.IndexSpec(["Name"]),
        hoarddb.IndexSpec(["Email"], unique=True, name="by_email"),
        hoarddb.IndexSpec(["Born", "City"], expire_after_seconds=60),
    ]
    types = hoarddb.FieldTypes(
        [hoarddb.FieldSpec("Name", "string", required=True),
         hoarddb.FieldSpec("Tags", "array", elem=hoarddb.FieldSpec("e", "string")),
         hoarddb.FieldSpec("Addr", "object", members=[hoarddb.FieldSpec("City", "string")])],
        mode="warn",
    )  # fmt: skip
    with public(mode, identity, script) as (pc, _, _):
        pc.call(
            lambda c: c.database("app").create_bucket(
                "b", "hash", stores=stores, indexes=indexes, field_types=types, encrypted=True
            )
        )
        (sent,) = script.of(opcodes.CREATE_BUCKET)
        assert sent["encrypted"] is True
        opts = sent["options"]
        assert opts["stores"] == [
            {"name": "Purchases", "engine": "fifo"},
            {"name": "Friends", "engine": "hash", "fields": [{"name": "Inner", "engine": "blob"}]},
        ]
        assert opts["indices"] == [
            "Name",
            {"fields": ["Email"], "unique": True, "name": "by_email"},
            {"fields": ["Born", "City"], "expire_after_seconds": 60},
        ]
        assert opts["field_types"] == "warn"
        assert opts["fields"][0] == {"name": "Name", "type": "string", "required": True}
        assert opts["fields"][1]["elem"] == {"name": "e", "type": "string"}
        assert opts["fields"][2]["members"] == [{"name": "City", "type": "string"}]
        assert "schemajson" not in sent  # a v1 driver never exposes it


def test_an_existing_bucket_is_already_exists_unless_if_not_exists(
    mode: str, identity: Any
) -> None:
    same = ok(opcodes.CREATE_BUCKET, 'bucket "b" already exists (type: hash)')
    script = Script({opcodes.CREATE_BUCKET: same})
    with public(mode, identity, script) as (pc, _, _):
        with pytest.raises(errors.AlreadyExistsError) as caught:
            pc.call(lambda c: c.database("app").create_bucket("b", "hash"))
        assert "already exists (type: hash)" in str(caught.value)
        again = pc.call(lambda c: c.database("app").create_bucket("b", "hash", if_not_exists=True))
        assert type(again).__name__ == "HashBucket"
        assert script.of(opcodes.BUCKET_INFO) == []  # no declaration, so no extra round trip


def test_an_existing_bucket_of_another_type_is_always_already_exists(
    mode: str, identity: Any
) -> None:
    script = Script(
        {opcodes.CREATE_BUCKET: ok(opcodes.CREATE_BUCKET, 'bucket "b" already exists (type: hash)')}
    )
    with public(mode, identity, script) as (pc, _, _), pytest.raises(errors.AlreadyExistsError):
        pc.call(lambda c: c.database("app").create_bucket("b", "btree", if_not_exists=True))


def test_if_not_exists_with_a_declaration_checks_the_store_fields_with_one_info_call(
    mode: str, identity: Any
) -> None:
    same = ok(opcodes.CREATE_BUCKET, 'bucket "User" already exists (type: hash)')
    info_missing = reply(
        opcodes.BUCKET_INFO,
        {
            "found": True,
            "database": "app",
            "bucket": {"name": "User", "type": "hash", "stores": ["Purchases"]},
        },
    )
    script = Script({opcodes.CREATE_BUCKET: same, opcodes.BUCKET_INFO: info_missing})
    stores = [hoarddb.StoreSpec("Purchases", "fifo"), hoarddb.StoreSpec("Photos", "lifo")]
    with public(mode, identity, script) as (pc, _, _):
        with pytest.raises(errors.ConflictError) as caught:
            pc.call(
                lambda c: c.database("app").create_bucket(
                    "User", "hash", if_not_exists=True, stores=stores
                )
            )
        text = str(caught.value)
        assert "Photos" in text and "Purchases" not in text.split("without")[1].split(",")[0]
        assert "declare_store_fields" in text
        assert len(script.of(opcodes.BUCKET_INFO)) == 1
        # all declared: fine, and still exactly one info per call
        done = pc.call(
            lambda c: c.database("app").create_bucket(
                "User", "hash", if_not_exists=True, stores=stores[:1]
            )
        )
        assert type(done).__name__ == "HashBucket" and len(script.of(opcodes.BUCKET_INFO)) == 2


def test_replication_factor_needs_the_capability_and_nothing_is_sent_without_it(
    mode: str, identity: Any
) -> None:
    script = Script({opcodes.CREATE_BUCKET: BUCKET_CREATED})
    with public(mode, identity, script) as (pc, _, _):  # the fake server advertises auth_token only
        with pytest.raises(errors.UnsupportedFeatureError) as caught:
            pc.call(lambda c: c.database("app").create_bucket("b", "hash", replication_factor=3))
        assert "bucket_rf" in str(caught.value) and "Nothing was sent" in str(caught.value)
        assert script.requests == []


def _with_bucket_rf(conn: fakehoard.Conn, doc: dict[str, Any]) -> list[fakehoard.Action]:
    return [fakehoard.auth_ok(capabilities=("bucket_rf",))]


def test_replication_factor_is_sent_when_the_server_advertises_the_capability(
    mode: str, identity: Any
) -> None:
    script = Script({opcodes.CREATE_BUCKET: BUCKET_CREATED})
    with public(
        mode,
        identity,
        script,
        on_auth=_with_bucket_rf,
    ) as (pc, _, _):
        pc.call(lambda c: c.database("app").create_bucket("b", "hash", replication_factor=3))
        assert script.of(opcodes.CREATE_BUCKET)[0]["replication_factor"] == 3


@pytest.mark.parametrize("bad", [0, -1, True])
def test_a_bad_replication_factor_is_refused_locally(mode: str, identity: Any, bad: Any) -> None:
    with public(mode, identity) as (pc, script, _):
        with pytest.raises(errors.InvalidArgumentError):
            pc.call(lambda c: c.database("app").create_bucket("b", "hash", replication_factor=bad))
        assert script.requests == []


def test_a_server_refusal_of_a_declaration_reaches_the_caller_verbatim(
    mode: str, identity: Any
) -> None:
    bad = reply(
        opcodes.CREATE_BUCKET, {"success": False, "message": '"colour" is not a field type'}
    )
    with public(mode, identity, Script({opcodes.CREATE_BUCKET: bad})) as (pc, _, _):
        with pytest.raises(errors.InvalidArgumentError) as caught:
            pc.call(
                lambda c: c.database("app").create_bucket(
                    "b",
                    "hash",
                    field_types=hoarddb.FieldTypes([hoarddb.FieldSpec("Name", "colour")]),
                )
            )
        assert caught.value.message == '"colour" is not a field type'


# --- the rest of Database ------------------------------------------------------------------------


ROW = {
    "name": "u",
    "type": "hash",
    "entries": 4,
    "stores": ["P"],
    "replication_factor": 3,
    "inherited": True,
}


def test_drop_list_declare_and_open(mode: str, identity: Any) -> None:
    script = Script(
        {
            opcodes.DELETE_BUCKET: ok(opcodes.DELETE_BUCKET, "dropped"),
            opcodes.LIST_BUCKETS: reply(
                opcodes.LIST_BUCKETS,
                {"found": True, "database": "app", "buckets": [ROW]},
            ),
            opcodes.DECLARE_STORE_FIELDS: ok(opcodes.DECLARE_STORE_FIELDS, "now declares: Tags"),
            opcodes.BUCKET_STATS: reply(
                opcodes.BUCKET_STATS, {"found": True, "type": "heap", "bucket": "q", "entries": 1}
            ),
        }
    )  # fmt: skip
    with public(mode, identity, script) as (pc, _, _):
        db = pc.client.database("app")
        pc.call(lambda c: db.drop_bucket("u"))
        (row,) = pc.call(lambda c: db.list_buckets())
        assert (row.name, row.type, row.entries, row.stores) == ("u", "hash", 4, ("P",))
        assert (row.replication_factor, row.inherited) == (3, True)
        pc.call(lambda c: db.declare_store_fields("u", [hoarddb.StoreSpec("Tags", "hash")]))
        assert script.of(opcodes.DECLARE_STORE_FIELDS) == [
            {"database": "app", "name": "u", "stores": [{"name": "Tags", "engine": "hash"}]}
        ]
        opened = pc.call(lambda c: db.open("q"))
        assert type(opened).__name__ == "HeapBucket" and opened.name == "q"
        with pytest.raises(errors.InvalidArgumentError):
            pc.call(lambda c: db.declare_store_fields("u", []))


def test_open_of_a_word_this_driver_does_not_know_gives_the_generic_bucket(
    mode: str, identity: Any
) -> None:
    script = Script(
        {opcodes.BUCKET_STATS: reply(opcodes.BUCKET_STATS, {"found": True, "type": "quantum"})}
    )
    with public(mode, identity, script) as (pc, _, _):
        opened = pc.call(lambda c: c.database("app").open("q"))
        assert type(opened).__name__ == "Bucket"


# --- info and stats ------------------------------------------------------------------------------


INFO_ANSWER = reply(
    opcodes.BUCKET_INFO,
    {
        "found": True,
        "database": "app",
        "bucket": {
            "name": "User",
            "type": "hash",
            "entries": 12,
            "stores": ["Purchases", "Photos"],
            "replication_factor": 3,
            "inherited": True,
        },
        "store_fields": [
            {"name": "Purchases", "engine": "fifo"},
            {"name": "Photos", "engine": "quantum"},
        ],
    },
)


@pytest.mark.parametrize("facet", ["hash", "btree", "fifo", "lifo", "heap", "blob", "bucket"])
def test_info_is_the_declaration_on_every_handle(mode: str, identity: Any, facet: str) -> None:
    script = Script({opcodes.BUCKET_INFO: INFO_ANSWER})
    with public(mode, identity, script) as (pc, _, _):
        decl = pc.call(lambda c: getattr(c.database("app"), facet)("User").info())
        assert decl.name == "User" and decl.type == "hash" and decl.entries == 12
        assert decl.stores == ("Purchases", "Photos")
        assert [(f.name, f.engine) for f in decl.store_fields] == [
            ("Purchases", "fifo"),
            ("Photos", "quantum"),  # a newer word passes through unchanged
        ]
        assert decl.replication_factor == 3 and decl.inherited is True
        assert decl.type == hoarddb.StoreType.HASH  # StrEnum equality with the plain string
        assert script.of(opcodes.BUCKET_INFO)[-1] == {"database": "app", "bucket": "User"}


def test_info_keeps_no_state_and_asks_again(mode: str, identity: Any) -> None:
    script = Script({opcodes.BUCKET_INFO: INFO_ANSWER})
    with public(mode, identity, script) as (pc, _, _):
        handle = pc.client.database("app").hash("User")
        pc.call(lambda c: handle.info())
        pc.call(lambda c: handle.info())
        assert len(script.of(opcodes.BUCKET_INFO)) == 2


def test_info_of_a_missing_bucket_is_not_found_with_the_fix(mode: str, identity: Any) -> None:
    script = Script({opcodes.BUCKET_INFO: [fakehoard.error(1, 'bucket "x" not found')]})
    with public(mode, identity, script) as (pc, _, _):
        with pytest.raises(errors.NotFoundError) as caught:
            pc.call(lambda c: c.database("app").hash("x").info())
        assert "create_bucket" in str(caught.value)


def test_stats_decodes_the_counts_and_the_declared_fields(mode: str, identity: Any) -> None:
    answer = reply(
        opcodes.BUCKET_STATS,
        {
            "found": True, "database": "app", "bucket": "User", "type": "hash", "entries": 5,
            "logical_bytes": 900, "indexes": ["by_email"], "encrypted": "aes",
            "stores": [{"name": "P", "engine": "fifo", "parent": "", "elements": 7}],
            "fields": [{"name": "Name", "type": "string", "required": True}],
            "field_types": "strict", "writes_refused": True,
        },
    )  # fmt: skip
    with public(mode, identity, Script({opcodes.BUCKET_STATS: answer})) as (pc, _, _):
        s = pc.call(lambda c: c.database("app").hash("User").stats())
        assert (s.name, s.type, s.entries, s.logical_bytes) == ("User", "hash", 5, 900)
        assert s.indexes == ("by_email",) and s.stores[0].elements == 7
        assert s.fields[0].required and s.field_types == "strict" and s.writes_refused


def test_drop_on_a_handle_drops_the_bucket(mode: str, identity: Any) -> None:
    script = Script({opcodes.DELETE_BUCKET: ok(opcodes.DELETE_BUCKET, "dropped")})
    with public(mode, identity, script) as (pc, _, _):
        pc.call(lambda c: c.database("app").hash("User").drop())
        assert script.of(opcodes.DELETE_BUCKET) == [{"database": "app", "name": "User"}]


# --- handles cost nothing ------------------------------------------------------------------------


def test_handles_do_no_io_and_have_identity(mode: str, identity: Any) -> None:
    with public(mode, identity) as (pc, script, server):
        client = pc.client
        before = len(server.frames)
        db = client.database("app")
        a, b = db.hash("users"), db.hash("users")
        assert a == b and hash(a) == hash(b) and a is not b
        assert repr(a) == "<HashBucket app/users>" and repr(db) == "<Database app>"
        assert a != db.btree("users") and a != db.hash("other") and a != "app/users"
        assert db == client.database("app") and db != client.database("other")
        assert {a: 1}[b] == 1  # usable as a dictionary key by the caller
        assert len(server.frames) == before and script.requests == []


def test_handles_belong_to_one_client(mode: str, identity: Any) -> None:
    with public(mode, identity) as (one, _, _), public(mode, identity) as (two, _, _):
        assert one.client.database("app") != two.client.database("app")
        assert one.client.database("app").hash("u") != two.client.database("app").hash("u")


def test_a_handle_is_small_registered_nowhere_and_holds_nothing_but_names(
    mode: str, identity: Any
) -> None:
    with public(mode, identity) as (pc, _, _):
        db = pc.client.database("app")
        handle = db.hash("users")
        assert not hasattr(handle, "__dict__")  # slots: no per-handle dictionary
        assert sys.getsizeof(handle) <= 64 and sys.getsizeof(db) <= 56
        holders = [
            r for r in gc.get_referrers(handle) if not inspect.isframe(r) and r is not locals()
        ]
        assert holders == []  # no registry, cache or client list holds it
        before = len(gc.get_objects())
        handles = [db.hash(f"b{i}") for i in range(5000)]
        del handles
        gc.collect()
        assert len(gc.get_objects()) - before < 100  # nothing was left behind


def test_creating_a_million_handles_is_cheap_and_does_not_grow_the_client(
    mode: str, identity: Any
) -> None:
    with public(mode, identity) as (pc, _, _):
        db = pc.client.database("app")
        stats = pc.client.pool_stats()
        for _ in range(200_000):
            db.hash("b")
        assert pc.client.pool_stats() == stats


# --- defaults ------------------------------------------------------------------------------------


def test_database_and_default_bucket_name_the_missing_segment(mode: str, identity: Any) -> None:
    with public(mode, identity) as (pc, _, _):
        with pytest.raises(errors.ConfigError, match="database name"):
            pc.client.database()
        with pytest.raises(errors.ConfigError, match="database and bucket"):
            pc.client.default_bucket()
    uri = "hoarddb://admin:pw@127.0.0.1:{port}/app"
    with fakehoard.FakeHoard(identity) as server:
        pc = PC(mode, server, identity, uri=uri.format(port=server.port))
        try:
            assert pc.client.database().name == "app"
            with pytest.raises(errors.ConfigError, match="bucket"):
                pc.client.default_bucket()
        finally:
            pc.close()


# --- closing -------------------------------------------------------------------------------------


def test_close_is_idempotent_and_later_calls_say_the_client_is_closed(
    mode: str, identity: Any
) -> None:
    with fakehoard.FakeHoard(identity) as server:
        pc = PC(mode, server, identity)
        pc.call(lambda c: c.ping())
        pc.close()
        # the harness closed it once; the second close is a no-op, not an error
        if mode == "sync":
            pc.client.close()
        else:
            asyncio.run(pc.client.close())
        assert "closed" in repr(pc.client)
        with pytest.raises(errors.ClientClosedError):
            pc.client.database("app")
        with pytest.raises(errors.ClientClosedError):
            _ping(mode, pc)
        assert server.wait_for(lambda: all(c.closed for c in server.connections))


def _ping(mode: str, pc: PC) -> None:
    if mode == "sync":
        pc.client.ping()
    else:
        asyncio.run(pc.client.ping())


def test_a_with_block_closes_the_client(identity: Any) -> None:
    with fakehoard.FakeHoard(identity) as server:
        tls = hoarddb.TlsOptions(fingerprints=[identity.fingerprint])
        with hoarddb.connect(f"hoarddb://admin:pw@127.0.0.1:{server.port}", tls=tls) as client:
            client.ping()
        assert "closed" in repr(client)
        assert server.wait_for(lambda: all(c.closed for c in server.connections))


def test_an_unclosed_client_warns_and_is_cleaned_up(
    mode: str, identity: Any, recwarn: pytest.WarningsRecorder
) -> None:
    with fakehoard.FakeHoard(identity) as server:
        tls = hoarddb.TlsOptions(fingerprints=[identity.fingerprint])
        uri = f"hoarddb://admin:pw@127.0.0.1:{server.port}"
        loop = asyncio.new_event_loop()
        if mode == "sync":
            client: Any = hoarddb.connect(uri, tls=tls)
        else:

            async def go() -> Any:
                return await hoarddb.aio.connect(uri, tls=tls)

            client = loop.run_until_complete(go())
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            del client
            gc.collect()
            if mode == "async":
                loop.run_until_complete(asyncio.sleep(0.05))
        loop.close()
        texts = [str(w.message) for w in caught if issubclass(w.category, ResourceWarning)]
        assert any("unclosed hoarddb" in t and "pw" not in t for t in texts), texts
    recwarn.clear()


def test_a_closed_client_makes_no_resource_warning(
    mode: str, identity: Any, recwarn: pytest.WarningsRecorder
) -> None:
    gc.collect()  # garbage left by earlier tests must not be blamed on this one
    recwarn.clear()
    with public(mode, identity) as (pc, _, _):
        pc.call(lambda c: c.ping())
    gc.collect()
    assert not [w for w in recwarn.list if issubclass(w.category, ResourceWarning)]


# --- fork ----------------------------------------------------------------------------------------


def test_a_forked_client_refuses_calls_and_its_close_leaves_the_parent_alone(
    mode: str, identity: Any
) -> None:
    with public(mode, identity) as (pc, _, _):
        pid = os.fork()
        if pid == 0:
            code = 1
            try:
                try:
                    pc.client.database("app")
                except errors.ForkedClientError:
                    code = 0
                if mode == "sync":
                    pc.client.close()
                else:
                    pc.client._ctx.pool.close_now()  # pyright: ignore[reportPrivateUsage]
            finally:
                os._exit(code)
        _, status = os.waitpid(pid, 0)
        assert os.WEXITSTATUS(status) == 0
        pc.call(lambda c: c.ping())  # the parent's connection still answers


# --- aio.connect ---------------------------------------------------------------------------------


def test_aio_connect_does_no_io_until_awaited_and_both_forms_give_a_client(identity: Any) -> None:
    async def main() -> None:
        with fakehoard.FakeHoard(identity) as server:
            tls = hoarddb.TlsOptions(fingerprints=[identity.fingerprint])
            uri = f"hoarddb://admin:pw@127.0.0.1:{server.port}"
            pending = hoarddb.aio.connect(uri, tls=tls)
            await asyncio.sleep(0.05)
            assert server.connections == []  # nothing happened when it was called
            client = await pending
            assert isinstance(client, hoarddb.aio.Client)
            await client.ping()
            await client.close()
            async with hoarddb.aio.connect(uri, tls=tls) as inner:
                assert isinstance(inner, hoarddb.aio.Client)
                await inner.ping()
            assert "closed" in repr(inner)  # the async with closed what it opened
            assert hoarddb.aio.AsyncClient is hoarddb.aio.Client

    asyncio.run(main())


def test_one_async_connect_object_cannot_be_used_twice_or_both_ways(identity: Any) -> None:
    async def main() -> None:
        with fakehoard.FakeHoard(identity) as server:
            tls = hoarddb.TlsOptions(fingerprints=[identity.fingerprint])
            uri = f"hoarddb://admin:pw@127.0.0.1:{server.port}"
            pending = hoarddb.aio.connect(uri, tls=tls)
            client = await pending
            with pytest.raises(errors.ConfigError, match="already"):
                await pending
            with pytest.raises(errors.ConfigError, match="already"):
                async with pending:
                    pass
            await client.close()

    asyncio.run(main())


def test_an_async_client_used_from_a_second_loop_is_a_config_error(identity: Any) -> None:
    holder: list[Any] = []

    async def first() -> None:
        with fakehoard.FakeHoard(identity) as server:
            tls = hoarddb.TlsOptions(fingerprints=[identity.fingerprint])
            client = await hoarddb.aio.connect(
                f"hoarddb://admin:pw@127.0.0.1:{server.port}", tls=tls
            )
            holder.append(client)

            def elsewhere() -> None:
                async def second() -> None:
                    with pytest.raises(errors.ConfigError, match="event loop"):
                        await client.ping()

                asyncio.run(second())

            await asyncio.to_thread(elsewhere)
            await client.close()

    asyncio.run(first())


# --- threads and tasks ---------------------------------------------------------------------------


def test_the_blocking_client_starts_no_thread(identity: Any) -> None:
    with fakehoard.FakeHoard(identity) as server:
        before = set(threading.enumerate())
        tls = hoarddb.TlsOptions(
            fingerprints=[identity.fingerprint],
        )
        client = hoarddb.connect(f"hoarddb://admin:pw@127.0.0.1:{server.port}", tls=tls,
                                 pool=PoolOptions(size=2))  # fmt: skip
        client.ping()
        ours = set(threading.enumerate()) - before - set(server._threads)  # pyright: ignore
        assert ours == set()
        client.close()
        assert set(threading.enumerate()) - before - set(server._threads) == set()  # pyright: ignore


def test_the_async_client_leaves_no_task_behind(identity: Any) -> None:
    async def main() -> None:
        with fakehoard.FakeHoard(identity) as server:
            before = set(asyncio.all_tasks())
            tls = hoarddb.TlsOptions(fingerprints=[identity.fingerprint])
            client = await hoarddb.aio.connect(
                f"hoarddb://admin:pw@127.0.0.1:{server.port}", tls=tls
            )
            await client.ping()
            assert set(asyncio.all_tasks()) == before  # none while it is open
            await client.close()
            await asyncio.sleep(0)
            assert set(asyncio.all_tasks()) == before

    asyncio.run(main())
