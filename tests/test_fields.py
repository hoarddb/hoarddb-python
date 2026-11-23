# SPDX-License-Identifier: Apache-2.0
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false, reportUnknownLambdaType=false
"""P14: store fields, and ``insert_many`` halving under a bounded server."""

from __future__ import annotations

import random
from typing import Any, cast

import pytest

import fakehoard
from clientkit import Script, public, reply
from hoarddb import errors
from hoarddb._core import opcodes
from hoarddb._ops.field import FieldRef, check_ref
from hoarddb._ops.refs import BucketRef

SENT = {"database": "app", "bucket": "User", "key": "ada"}


def field(pc: Any, name: str = "Purchases", key: str = "ada") -> Any:
    return pc.client.database("app").hash("User").field(key, name)


def listing(cursor: Any, pc: Any) -> list[Any]:
    if pc.loop is None:
        return list(cursor)

    async def go() -> list[Any]:
        return [item async for item in cursor]

    return pc.loop.run_until_complete(go())  # type: ignore[no-any-return]


# --- handles -------------------------------------------------------------------------------------


@pytest.mark.parametrize("facet", ["bucket", "hash", "btree"])
def test_a_field_handle_is_a_few_strings_with_identity_and_no_io(
    mode: str, identity: Any, facet: str
) -> None:
    with public(mode, identity) as (pc, script, server):
        before = len(server.frames)
        db = pc.client.database("app")
        a = getattr(db, facet)("User").field("ada", "Purchases")
        b = getattr(db, facet)("User").field("ada", "Purchases")
        assert a == b and hash(a) == hash(b) and a is not b
        assert (a.key, a.name) == ("ada", "Purchases")
        assert repr(a) == "<FieldStore app/User/ada.Purchases>"
        assert a != getattr(db, facet)("User").field("ada", "Photos")
        assert a != getattr(db, facet)("User").field("grace", "Purchases")
        assert not hasattr(a, "__dict__")
        assert len(server.frames) == before and script.requests == []


def test_a_nested_field_addresses_a_store_inside_an_element(mode: str, identity: Any) -> None:
    with public(mode, identity) as (pc, _, _):
        inner = field(pc, "Friends").field("grace", "Inner").field("x", "Deep")
        assert repr(inner) == "<FieldStore app/User/ada.Friends.grace.Inner.x.Deep>"
        assert inner._path == ("Friends", "grace", "Inner", "x")  # pyright: ignore


# --- queues and heaps ----------------------------------------------------------------------------


def test_push_sends_the_field_the_document_and_a_priority_only_when_given(
    mode: str, identity: Any
) -> None:
    answer = reply(opcodes.FIELD_PUSH, {"success": True, "entry_id": "e1", "priority": 7, "len": 1,
                                        "copies_confirmed": 2})  # fmt: skip
    script = Script({opcodes.FIELD_PUSH: answer})
    with public(mode, identity, script) as (pc, _, _):
        plain = pc.call(lambda c: field(pc).push({"Item": "tea"}))
        heap = pc.call(
            lambda c: field(pc, "Books").push({"T": "x"}, priority=7, write_concern="all")
        )
        assert plain.entry_id == "e1" and plain.copies_confirmed == 2
        assert heap.priority == 7
        first, second = script.of(opcodes.FIELD_PUSH)
        assert first == {**SENT, "field": "Purchases", "data": {"Item": "tea"}}
        assert second["priority"] == 7 and second["writeconcern"] == "all"
        assert second["field"] == "Books"


def test_pop_returns_the_entries_as_a_list(mode: str, identity: Any) -> None:
    item = {
        "entry_id": "e1",
        "position": 3,
        "priority": 2,
        "element": {"Item": "tea", "_id_": "e1"},
    }
    answers = [
        reply(opcodes.FIELD_POP, {"success": True, "found": True, "len": 0, "items": [item]}),
        reply(opcodes.FIELD_POP, {"success": True, "found": False, "len": 0}),
    ]
    script = Script({opcodes.FIELD_POP: answers})
    with public(mode, identity, script) as (pc, _, _):
        (popped,) = pc.call(lambda c: field(pc).pop())
        assert popped.element == {"Item": "tea", "_id_": "e1"}
        assert (popped.entry_id, popped.position, popped.priority) == ("e1", 3, 2)
        assert pc.call(lambda c: field(pc).pop()) == []


def test_pop_is_a_write_and_is_never_retried(mode: str, identity: Any) -> None:
    script = Script({opcodes.FIELD_POP: [fakehoard.Close()]})
    with public(mode, identity, script) as (pc, _, _):
        with pytest.raises(errors.ConnectionFailedError) as caught:
            pc.call(lambda c: field(pc).pop())
        assert caught.value.write_outcome == "unknown"
        assert len(script.of(opcodes.FIELD_POP)) == 1


def test_peek_returns_entries_in_pop_order_and_validates_its_limit(
    mode: str, identity: Any
) -> None:
    answer = reply(
        opcodes.FIELD_PEEK,
        {"success": True, "found": True, "items": [{"element": {"n": 1}, "position": 0},
                                                   {"element": {"n": 2}, "position": 1}]},
    )  # fmt: skip
    script = Script({opcodes.FIELD_PEEK: answer})
    with public(mode, identity, script) as (pc, _, _):
        got = pc.call(lambda c: field(pc).peek(limit=2))
        assert [e.element for e in got] == [{"n": 1}, {"n": 2}]
        assert [e.position for e in got] == [0, 1]
        assert script.of(opcodes.FIELD_PEEK)[0]["limit"] == 2
        for bad in (0, 1001, True, 2.5):
            with pytest.raises(errors.InvalidArgumentError, match="1 to 1000"):
                pc.call(peek_with(pc, bad))
        assert len(script.of(opcodes.FIELD_PEEK)) == 1


def test_peek_of_an_empty_field_is_an_empty_list(mode: str, identity: Any) -> None:
    empty = reply(opcodes.FIELD_PEEK, {"success": True, "found": False, "len": 0})
    with public(mode, identity, Script({opcodes.FIELD_PEEK: empty})) as (pc, _, _):
        assert pc.call(lambda c: field(pc).peek()) == []


# --- hash, btree and blob fields -----------------------------------------------------------------


def test_put_get_delete_on_an_element(mode: str, identity: Any) -> None:
    script = Script(
        {
            opcodes.FIELD_PUT: reply(opcodes.FIELD_PUT, {"success": True, "element_key": "grace"}),
            opcodes.FIELD_GET: [
                reply(opcodes.FIELD_GET, {"success": True, "found": True, "element_key": "grace",
                                          "element": {"Name": "Grace"}}),
                reply(opcodes.FIELD_GET, {"success": True, "found": False, "element_key": "x"}),
            ],
            opcodes.FIELD_DELETE: [
                reply(opcodes.FIELD_DELETE, {"success": True, "found": True}),
                reply(opcodes.FIELD_DELETE, {"success": True, "found": False}),
            ],
        }
    )  # fmt: skip
    with public(mode, identity, script, write_concern="one") as (pc, _, _):
        stored = pc.call(lambda c: field(pc, "Friends").put("grace", {"Name": "Grace"}))
        assert stored.element_key == "grace"
        got = pc.call(lambda c: field(pc, "Friends").get("grace"))
        assert got.element == {"Name": "Grace"} and got.element_key == "grace"
        assert pc.call(lambda c: field(pc, "Friends").get("nobody")) is None
        assert pc.call(lambda c: field(pc, "Friends").delete("grace")) is True
        assert pc.call(lambda c: field(pc, "Friends").delete("grace")) is False
        (put,) = script.of(opcodes.FIELD_PUT)
        assert put == {**SENT, "field": "Friends", "element": "grace", "data": {"Name": "Grace"},
                       "writeconcern": "one"}  # fmt: skip


def test_blob_fields_carry_bytes_metadata_and_use_the_bulk_lane(mode: str, identity: Any) -> None:
    script = Script(
        {
            opcodes.FIELD_PUT: reply(
                opcodes.FIELD_PUT, {"success": True, "element_key": "n.txt", "blob_id": "b1"}
            ),
            opcodes.FIELD_GET: reply(
                opcodes.FIELD_GET,
                {"success": True, "found": True, "element_key": "n.txt", "blob_id": "b1",
                 "content": b"notes", "element": {"ContentType": "text/plain"}},
            ),
        }
    )  # fmt: skip
    with public(mode, identity, script) as (pc, _, _):
        written = pc.call(
            lambda c: field(pc, "Data").put_blob(
                "n.txt", b"notes", metadata={"ContentType": "text/plain"}
            )
        )
        assert (written.element_key, written.blob_id) == ("n.txt", "b1")
        blob = pc.call(lambda c: field(pc, "Data").get_blob("n.txt"))
        assert (blob.content, blob.blob_id, blob.metadata) == (
            b"notes",
            "b1",
            {"ContentType": "text/plain"},
        )
        (put,) = script.of(opcodes.FIELD_PUT)
        assert put["content"] == b"notes" and put["data"] == {"ContentType": "text/plain"}
        assert pc.client.pool_stats().bulk_idle == 1
        with pytest.raises(errors.InvalidArgumentError, match="bytes"):
            pc.call(lambda c: field(pc, "Data").put_blob("x", "text"))


def test_a_missing_blob_element_is_none(mode: str, identity: Any) -> None:
    miss = reply(opcodes.FIELD_GET, {"success": True, "found": False})
    with public(mode, identity, Script({opcodes.FIELD_GET: miss})) as (pc, _, _):
        assert pc.call(lambda c: field(pc, "Data").get_blob("x")) is None


def test_a_nested_store_sends_the_path_and_the_inner_field(mode: str, identity: Any) -> None:
    answer = reply(opcodes.FIELD_PUSH, {"success": True, "entry_id": "i1"})
    script = Script({opcodes.FIELD_PUSH: answer})
    with public(mode, identity, script) as (pc, _, _):
        inner = field(pc, "Friends").field("grace", "Letters")
        pc.call(lambda c: inner.push({"Text": "hello"}))
        (sent,) = script.of(opcodes.FIELD_PUSH)
        assert sent["field"] == "Letters" and sent["path"] == ["Friends", "grace"]


# --- len and list --------------------------------------------------------------------------------


def test_len_is_a_method_and_dunder_len_does_not_exist(mode: str, identity: Any) -> None:
    answer = reply(opcodes.FIELD_LEN, {"success": True, "found": True, "len": 12})
    with public(mode, identity, Script({opcodes.FIELD_LEN: answer})) as (pc, _, _):
        assert pc.call(lambda c: field(pc).len()) == 12
        store = field(pc)
        assert not hasattr(store, "__len__") and not hasattr(store, "__bool__")
        with pytest.raises(TypeError):
            len(store)


def entries(*names: str) -> list[dict[str, Any]]:
    return [{"element": {"n": n}, "element_key": n} for n in names]


def test_list_pages_with_the_opaque_next_and_a_limit(mode: str, identity: Any) -> None:
    script = Script(
        {
            opcodes.FIELD_LIST: [
                reply(opcodes.FIELD_LIST, {"success": True, "entries": entries("a", "b"),
                                           "has_more": True, "next": b"\x00\x01opaque"}),
                reply(opcodes.FIELD_LIST, {"success": True, "entries": entries("c"),
                                           "has_more": False}),
            ]
        }
    )  # fmt: skip
    with public(mode, identity, script) as (pc, _, _):
        cursor = pc.call(lambda c: field(pc).list(page_size=2))
        assert [e.element_key for e in listing(cursor, pc)] == ["a", "b", "c"]
        first, second = script.of(opcodes.FIELD_LIST)
        assert first["limit"] == 2 and "after" not in first
        assert second["after"] == b"\x00\x01opaque"  # passed back unchanged
        assert pc.client.pool_stats().bulk_idle == 1  # the bulk lane, one connection


def test_list_stops_at_its_limit(mode: str, identity: Any) -> None:
    page = {"success": True, "entries": entries("a", "b", "c"), "has_more": True, "next": b"n"}
    script = Script({opcodes.FIELD_LIST: reply(opcodes.FIELD_LIST, page)})
    with public(mode, identity, script) as (pc, _, _):
        cursor = pc.call(lambda c: field(pc).list(page_size=10, limit=2))
        assert [e.element_key for e in listing(cursor, pc)] == ["a", "b"]
        assert len(script.of(opcodes.FIELD_LIST)) == 1


@pytest.mark.parametrize("size", [0, 1001, True, 2.5])
def test_list_page_size_is_one_to_a_thousand(mode: str, identity: Any, size: Any) -> None:
    with public(mode, identity) as (pc, script, _):
        with pytest.raises(errors.InvalidArgumentError, match="1 to 1000"):
            pc.call(lambda c: field(pc).list(page_size=size))
        assert script.requests == []


def test_list_halves_a_page_that_is_too_large(mode: str, identity: Any) -> None:
    def answer(doc: dict[str, Any]) -> Any:
        if doc["limit"] > 2:
            return [fakehoard.error(14, "too large", max_result_bytes=10, retained_bytes=20)]
        return reply(
            opcodes.FIELD_LIST, {"success": True, "entries": entries("a"), "has_more": False}
        )

    script = Script({opcodes.FIELD_LIST: answer})
    with public(mode, identity, script) as (pc, _, _):
        cursor = pc.call(lambda c: field(pc).list(page_size=8))
        assert [e.element_key for e in listing(cursor, pc)] == ["a"]
        assert [d["limit"] for d in script.of(opcodes.FIELD_LIST)] == [8, 4, 2]


# --- refusals ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "cls", "message"),
    [
        (1, errors.NotFoundError, 'store field not declared: Bucket "User" declares no "Enemies"'),
        (1, errors.NotFoundError, 'parent document not found: No document with key "nobody"'),
        (4, errors.InvalidArgumentError, "operation not supported by the field's engine"),
    ],
)
def test_the_servers_field_refusals_keep_their_class_and_sentence(
    mode: str, identity: Any, code: int, cls: type, message: str
) -> None:
    script = Script({opcodes.FIELD_LEN: [fakehoard.error(code, message)]})
    with public(mode, identity, script) as (pc, _, _):
        with pytest.raises(cls) as caught:
            pc.call(lambda c: field(pc, "Enemies").len())
        assert cast(Any, caught.value).message == message


def test_a_field_write_that_is_refused_in_band_is_raised(mode: str, identity: Any) -> None:
    refusal = reply(opcodes.FIELD_PUSH, {"success": False, "message": "field is full"})
    with (
        public(mode, identity, Script({opcodes.FIELD_PUSH: refusal})) as (pc, _, _),
        pytest.raises(errors.InvalidArgumentError, match="field is full"),
    ):
        pc.call(lambda c: field(pc).push({"a": 1}))


@pytest.mark.parametrize("bad", ["", None, 5])
def test_the_key_the_field_and_the_element_must_be_names(
    mode: str, identity: Any, bad: Any
) -> None:
    with public(mode, identity) as (pc, script, _):
        for call in (
            lambda c: field(pc, "Purchases", bad).push({"a": 1}),
            lambda c: field(pc, bad).push({"a": 1}),
            lambda c: field(pc, "Friends").get(bad),
            lambda c: field(pc, "Friends").put(bad, {"a": 1}),
        ):
            with pytest.raises(errors.InvalidArgumentError, match="Nothing was sent"):
                pc.call(call)
        assert script.requests == []


# --- insert_many halving: no document is written twice -------------------------------------------


class Bounded:
    """A server whose store commits a batch atomically only up to ``bound`` documents; it refuses a
    bigger one whole ("none of it was written"), and can be told to fail after some commits."""

    def __init__(self, bound: int, fail_after: int | None = None) -> None:
        self.bound = bound
        self.fail_after = fail_after
        self.stored: list[str] = []
        self.commits = 0

    def __call__(self, doc: dict[str, Any]) -> Any:
        batch = [d["n"] for d in doc["data"]]
        if len(batch) > self.bound:
            return reply(
                opcodes.BATCH_PUT_OK,
                {
                    "success": False,
                    "message": "batch exceeds the atomic bound: none of it was written",
                },
            )
        if self.fail_after is not None and self.commits >= self.fail_after:
            return reply(opcodes.BATCH_PUT_OK, {"success": False, "message": "disk full"})
        self.commits += 1
        self.stored.extend(batch)
        return reply(opcodes.BATCH_PUT_OK, {"ids": batch, "count": len(batch), "success": True})


def peek_with(pc: Any, limit: Any) -> Any:
    def go(c: Any) -> Any:
        return field(pc).peek(limit=limit)

    return go


@pytest.mark.parametrize("seed", range(6))
def test_halving_stores_every_document_exactly_once_and_in_order(
    mode: str, identity: Any, seed: int
) -> None:
    rnd = random.Random(seed)
    n, bound = rnd.randint(1, 60), rnd.randint(1, 9)
    docs = [{"n": f"{i:03d}"} for i in range(n)]
    server = Bounded(bound)
    with public(mode, identity, Script({opcodes.BATCH_PUT: server})) as (pc, _, _):
        result = pc.call(lambda c: pc.client.database("app").hash("u").insert_many(docs))
        assert server.stored == [d["n"] for d in docs]  # once each, in order
        assert result.ids == server.stored and result.count == n


@pytest.mark.parametrize("seed", range(6))
def test_the_written_prefix_lets_the_caller_resend_only_the_remainder(
    mode: str, identity: Any, seed: int
) -> None:
    rnd = random.Random(100 + seed)
    n, bound = rnd.randint(8, 50), rnd.randint(1, 5)
    docs = [{"n": f"{i:03d}"} for i in range(n)]
    server = Bounded(bound, fail_after=rnd.randint(1, 4))
    with public(mode, identity, Script({opcodes.BATCH_PUT: server})) as (pc, _, _):
        with pytest.raises(errors.InvalidArgumentError) as caught:
            pc.call(lambda c: pc.client.database("app").hash("u").insert_many(docs))
        written = caught.value.written_count
        assert written is not None
        assert written == len(server.stored) and caught.value.written_ids == server.stored
        assert server.stored == [d["n"] for d in docs[:written]]  # a clean prefix of the input
        server.fail_after = None  # the disk has room again; resend only the remainder
        rest = pc.call(lambda c: pc.client.database("app").hash("u").insert_many(docs[written:]))
        assert server.stored == [d["n"] for d in docs] and rest.count == n - written


@pytest.mark.parametrize("bad", [None, 5, "", b"x"], ids=["none", "int", "empty", "bytes"])
@pytest.mark.parametrize("slot", ["element", "name"])
def test_a_nested_field_path_element_that_is_not_a_name_is_refused_locally(
    mode: str, identity: Any, bad: Any, slot: str
) -> None:
    with public(mode, identity) as (pc, _, server):
        outer = field(pc, "Friends")
        sent = len(server.frames)
        args = (bad, "Letters") if slot == "element" else ("grace", bad)
        with pytest.raises(errors.InvalidArgumentError, match="Nothing was sent"):
            outer.field(*args)
        assert len(server.frames) == sent


@pytest.mark.parametrize("bad", [None, 5, ""], ids=["none", "int", "empty"])
def test_a_field_ref_with_a_bad_path_element_is_refused_by_the_verb_check(bad: Any) -> None:
    ref = FieldRef(BucketRef("app", "User"), "ada", "Letters", ("Friends", bad))
    with pytest.raises(errors.InvalidArgumentError, match="Nothing was sent"):
        check_ref(ref, operation="field_push")
