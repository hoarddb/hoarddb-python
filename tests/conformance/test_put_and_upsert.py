# SPDX-License-Identifier: Apache-2.0
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false, reportUnknownLambdaType=false
"""``put`` inserts and ``upsert`` replaces, against a real server, through both surfaces.

A put of a key that already holds a document is the server's refusal with its own code: nothing is
written, and the driver names it (``DocumentExistsError``) instead of reporting an internal error.
"""

from __future__ import annotations

from typing import Any

import pytest

import hoarddb
from hoarddb import errors
from hoarddb._bson import deserialize, serialize
from hoarddb._core import frame, opcodes
from realkit import Rig, Servers, authenticate
from tapkit import RawConn

pytestmark = pytest.mark.server

MODES = ("sync", "async")


@pytest.fixture(params=MODES)
def rig(request: pytest.FixtureRequest, servers: Servers) -> Any:
    made = servers.rig(request.param, servers.node())
    made.call(lambda c: c.create_database("putsert"))
    try:
        yield made
    finally:
        made.drop_every_database()


def bucket(c: Any, name: str) -> Any:
    return c.database("putsert").bucket(name)


@pytest.mark.parametrize("store", ["hash", "btree"])
def test_a_second_put_of_a_key_is_refused_and_changes_nothing(rig: Rig, store: str) -> None:
    rig.call(lambda c: c.database("putsert").create_bucket("users", store))
    rig.call(lambda c: bucket(c, "users").put("ada", {"name": "Ada", "age": 36}))
    with pytest.raises(errors.DocumentExistsError) as caught:
        rig.call(lambda c: bucket(c, "users").put("ada", {"name": "Somebody else"}))
    err = caught.value
    assert (err.code, err.wire_code, err.write_outcome) == ("DOCUMENT_EXISTS", 18, "not_applied")
    assert 'Document "ada" already exists in bucket "users"' in err.message
    assert err.hint is not None and "upsert(key, doc)" in err.hint
    assert not err.retryable
    assert rig.call(lambda c: bucket(c, "users").get("ada")) == {
        "_id_": "ada",
        "name": "Ada",
        "age": 36,
    }
    assert rig.call(lambda c: bucket(c, "users").count()) == 1


def test_an_insert_of_an_id_that_is_taken_is_the_same_refusal(rig: Rig) -> None:
    rig.call(lambda c: c.database("putsert").create_bucket("users", "hash"))
    rig.call(lambda c: bucket(c, "users").put("ada", {"name": "Ada"}))
    with pytest.raises(errors.DocumentExistsError):
        rig.call(lambda c: bucket(c, "users").insert({"_id_": "ada", "name": "x"}))
    with pytest.raises(errors.DocumentExistsError) as caught:
        rig.call(lambda c: bucket(c, "users").insert_many([{"_id_": "new"}, {"_id_": "ada"}]))
    assert caught.value.write_outcome == "not_applied"
    assert rig.call(lambda c: bucket(c, "users").count()) == 1, "the refused batch stored nothing"


@pytest.mark.parametrize("store", ["hash", "btree"])
def test_upsert_creates_a_document_and_then_replaces_it(rig: Rig, store: str) -> None:
    rig.call(lambda c: c.database("putsert").create_bucket("users", store))
    made = rig.call(lambda c: bucket(c, "users").upsert("ada", {"name": "Ada", "age": 36}))
    assert made.id == "ada"
    again = rig.call(lambda c: bucket(c, "users").upsert("ada", {"name": "Ada Lovelace"}))
    assert again.id == "ada"
    # the whole document is replaced: the field the second one left out is gone
    assert rig.call(lambda c: bucket(c, "users").get("ada")) == {
        "_id_": "ada",
        "name": "Ada Lovelace",
    }
    assert rig.call(lambda c: bucket(c, "users").count()) == 1


def test_upsert_keeps_the_documents_store_fields(rig: Rig) -> None:
    stores = [hoarddb.StoreSpec("Purchases", "fifo")]
    rig.call(lambda c: c.database("putsert").create_bucket("users", "hash", stores=stores))
    rig.call(lambda c: bucket(c, "users").put("ada", {"name": "Ada"}))
    rig.call(lambda c: bucket(c, "users").field("ada", "Purchases").push({"item": "tea"}))
    rig.call(lambda c: bucket(c, "users").upsert("ada", {"name": "Ada Lovelace"}))
    assert rig.call(lambda c: bucket(c, "users").field("ada", "Purchases").len()) == 1


@pytest.mark.parametrize("store", ["fifo", "lifo", "heap"])
def test_upsert_on_a_bucket_with_no_keys_is_the_servers_refusal(rig: Rig, store: str) -> None:
    rig.call(lambda c: c.database("putsert").create_bucket("q", store))
    with pytest.raises(errors.InvalidArgumentError, match="no key to replace") as caught:
        rig.call(lambda c: bucket(c, "q").upsert("k", {"n": 1}))
    assert caught.value.write_outcome == "not_applied"
    assert rig.call(lambda c: bucket(c, "q").count()) == 0


# -- put on the store types that have no keyed insert ---------------------------------------------
#
# "A put inserts and never replaces" is the rule of a hash and a btree. The other four types answer
# a put of a key that is already held in their own way, and these tests pin what a v1.1.0 server
# does, so that the documentation cannot say more than is true.


def test_put_of_a_held_key_on_a_heap_replaces_the_entry(rig: Rig) -> None:
    rig.call(lambda c: c.database("putsert").create_bucket("h", "heap"))
    rig.call(lambda c: bucket(c, "h").put("k", {"n": 1}))
    rig.call(lambda c: bucket(c, "h").put("k", {"n": 2}))  # no refusal
    assert rig.call(lambda c: bucket(c, "h").count()) == 1
    (entry,) = rig.call(lambda c: bucket(c, "h").peek(limit=5))
    assert (entry["_id_"], entry["n"]) == ("k", 2)


@pytest.mark.parametrize("store", ["fifo", "lifo"])
def test_put_of_a_held_key_on_a_queue_appends_a_second_entry(rig: Rig, store: str) -> None:
    rig.call(lambda c: c.database("putsert").create_bucket("q", store))
    rig.call(lambda c: bucket(c, "q").put("k", {"n": 1}))
    rig.call(lambda c: bucket(c, "q").put("k", {"n": 2}))  # no refusal
    assert rig.call(lambda c: bucket(c, "q").count()) == 2
    entries = rig.call(lambda c: bucket(c, "q").peek(limit=5))
    assert [e["_id_"] for e in entries] == ["k", "k"]
    assert sorted(e["n"] for e in entries) == [1, 2]


def test_put_and_insert_on_a_blob_bucket_are_refused(rig: Rig) -> None:
    rig.call(lambda c: c.database("putsert").create_bucket("b", "blob"))
    for write in (
        lambda c: bucket(c, "b").put("k", {"n": 1}),
        lambda c: bucket(c, "b").insert({"n": 1}),
    ):
        with pytest.raises(errors.InvalidArgumentError) as caught:
            rig.call(write)
        assert (caught.value.wire_code, caught.value.write_outcome) == (4, "not_applied")
    assert rig.call(lambda c: bucket(c, "b").count()) == 0


# -- the connection after a refusal ---------------------------------------------------------------


def test_the_server_keeps_the_connection_open_after_document_exists_and_batch_too_large(
    servers: Servers,
) -> None:
    """On one bare connection: a refused put (code 18), then a refused over-bound batch (code 17),
    then a ping that is answered. That is why the driver keeps the connection after either."""
    node = servers.node()

    def ask(conn: RawConn, opcode: int, document: dict[str, Any] | None) -> frame.Frame:
        conn.send(opcode, frame.new_request_id(), serialize(document) if document else b"")
        return conn.read_frame()

    put = {"database": "keeps", "bucket": "rows", "key": "ada", "data": {"n": 1}}
    big = [{"_id_": f"k{i:03d}", "pad": "x" * 100_000} for i in range(250)]
    try:
        with RawConn(node.driver_port, timeout=60.0) as conn:
            assert authenticate(conn).opcode == opcodes.AUTH_OK
            assert ask(conn, opcodes.CREATE_DB, {"name": "keeps"}).opcode == opcodes.CREATE_DB
            made = {"database": "keeps", "name": "rows", "type": "hash"}
            assert ask(conn, opcodes.CREATE_BUCKET, made).opcode == opcodes.CREATE_BUCKET
            assert ask(conn, opcodes.PUT, put).opcode == opcodes.PUT
            for _ in range(3):
                refused = ask(conn, opcodes.PUT, put)
                assert refused.opcode == opcodes.ERROR
                assert deserialize(refused.payload)["code"] == 18
            batch = {"database": "keeps", "bucket": "rows", "data": big}
            too_large = ask(conn, opcodes.BATCH_PUT, batch)
            assert too_large.opcode == opcodes.ERROR
            assert deserialize(too_large.payload)["code"] == 17
            assert ask(conn, opcodes.PING, None).opcode == opcodes.PONG
    finally:
        servers.rig("sync", node).drop_every_database()


def test_twenty_refused_puts_use_one_connection(servers: Servers) -> None:
    node = servers.node()
    rig = servers.rig("sync", node)
    try:
        rig.call(lambda c: c.create_database("putsert"))
        rig.call(lambda c: c.database("putsert").create_bucket("users", "hash"))
        rig.call(lambda c: bucket(c, "users").put("ada", {"n": 1}))
        before = rig.call(lambda c: c.pool_stats())
        for _ in range(20):
            with pytest.raises(errors.DocumentExistsError):
                rig.call(lambda c: bucket(c, "users").put("ada", {"n": 2}))
        after = rig.call(lambda c: c.pool_stats())
        assert (after.dials, after.discarded) == (before.dials, before.discarded), (
            "a refused put must not cost a connection"
        )
    finally:
        rig.drop_every_database()
