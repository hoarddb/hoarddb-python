# SPDX-License-Identifier: Apache-2.0
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false, reportUnknownLambdaType=false
"""P12: keyed, queue, heap and blob verbs through both surfaces against a scripted server."""

from __future__ import annotations

import math
from functools import partial
from typing import Any

import pytest

import fakehoard
from clientkit import Script, public, reply
from hoarddb import errors
from hoarddb._bson import Opaque
from hoarddb._core import opcodes

PUT_OK = reply(opcodes.PUT, {"id": "ada", "success": True, "seq": 7, "copies_confirmed": 2,
                             "replication_factor": 3})  # fmt: skip


def bucket(pc: Any, facet: str = "bucket", name: str = "users") -> Any:
    return getattr(pc.client.database("app"), facet)(name)


def _insert_many(pc: Any, docs: Any, client: Any) -> Any:
    return bucket(pc, "hash").insert_many(docs)


def _peek(pc: Any, limit: int, client: Any) -> Any:
    return bucket(pc, "fifo", "q").peek(limit=limit)


# --- put, insert, insert_many --------------------------------------------------------------------


@pytest.mark.parametrize("facet", ["bucket", "hash", "btree"])
def test_put_sends_the_key_the_document_and_the_default_write_concern(
    mode: str, identity: Any, facet: str
) -> None:
    script = Script({opcodes.PUT: PUT_OK})
    with public(mode, identity, script, write_concern="majority") as (pc, _, _):
        result = pc.call(lambda c: bucket(pc, facet).put("ada", {"Name": "Ada", "Born": 1815}))
        assert (result.id, result.seq, result.copies_confirmed, result.replication_factor) == (
            "ada",
            7,
            2,
            3,
        )
        (sent,) = script.of(opcodes.PUT)
        assert sent == {
            "database": "app",
            "bucket": "users",
            "key": "ada",
            "data": {"Name": "Ada", "Born": 1815},
            "writeconcern": "majority",
        }


def test_a_call_s_write_concern_overrides_the_default_and_is_checked(
    mode: str, identity: Any
) -> None:
    script = Script({opcodes.PUT: PUT_OK})
    with public(mode, identity, script, write_concern="majority") as (pc, _, _):
        pc.call(lambda c: bucket(pc).put("k", {}, write_concern="all"))
        assert script.of(opcodes.PUT)[0]["writeconcern"] == "all"
        with pytest.raises(errors.InvalidArgumentError, match="write_concern"):
            pc.call(lambda c: bucket(pc).put("k", {}, write_concern="most"))
        assert len(script.of(opcodes.PUT)) == 1  # the bad one was never sent


def test_no_write_concern_anywhere_sends_none(mode: str, identity: Any) -> None:
    script = Script({opcodes.PUT: PUT_OK})
    with public(mode, identity, script) as (pc, _, _):
        pc.call(lambda c: bucket(pc).put("k", {}))
        assert "writeconcern" not in script.of(opcodes.PUT)[0]


def test_insert_sends_an_empty_key_and_returns_the_assigned_one(mode: str, identity: Any) -> None:
    script = Script(
        {opcodes.PUT: reply(opcodes.PUT, {"id": "minted-1", "success": True, "seq": 1})}
    )
    with public(mode, identity, script) as (pc, _, _):
        result = pc.call(lambda c: bucket(pc, "hash").insert({"Name": "Ada"}))
        assert result.id == "minted-1" and result.copies_confirmed is None  # absent, not zero
        assert script.of(opcodes.PUT)[0].get("key", "") == ""  # empty: omitted on the wire


@pytest.mark.parametrize("bad", [None, "doc", ["a"], 7, b"x"])
def test_a_document_must_be_a_mapping_and_nothing_is_sent_otherwise(
    mode: str, identity: Any, bad: Any
) -> None:
    with public(mode, identity) as (pc, script, _):
        for call in (
            lambda c: bucket(pc).put("k", bad),
            lambda c: bucket(pc).insert(bad),
            lambda c: bucket(pc).push(bad),
        ):
            with pytest.raises(errors.InvalidArgumentError, match="mapping"):
                pc.call(call)
        assert script.requests == []


@pytest.mark.parametrize("key", ["", None, 5])
def test_a_key_must_be_a_non_empty_string(mode: str, identity: Any, key: Any) -> None:
    with public(mode, identity) as (pc, script, _):
        for call in (
            lambda c: bucket(pc).put(key, {}),
            lambda c: bucket(pc).get(key),
            lambda c: bucket(pc).exists(key),
            lambda c: bucket(pc).delete(key),
            lambda c: bucket(pc).update(key, {"$set": {"a": 1}}),
        ):
            with pytest.raises(errors.InvalidArgumentError, match="key"):
                pc.call(call)
        assert script.requests == []


def test_a_server_refusal_of_a_write_is_raised_with_its_sentence(mode: str, identity: Any) -> None:
    refusal = reply(opcodes.PUT, {"success": False, "message": "document too large"})
    with public(mode, identity, Script({opcodes.PUT: refusal})) as (pc, _, _):
        with pytest.raises(errors.InvalidArgumentError) as caught:
            pc.call(lambda c: bucket(pc).put("k", {}))
        assert caught.value.message == "document too large"


def test_a_field_type_refusal_is_the_field_type_error(mode: str, identity: Any) -> None:
    script = Script({opcodes.PUT: [fakehoard.error(16, "field Name must be string")]})
    with public(mode, identity, script) as (pc, _, _), pytest.raises(errors.FieldTypeError):
        pc.call(lambda c: bucket(pc).put("k", {"Name": 1}))


# --- insert_many ---------------------------------------------------------------------------------


def batch_ok(ids: list[str], **extra: Any) -> Any:
    return reply(opcodes.BATCH_PUT_OK, {"ids": ids, "count": len(ids), "success": True, **extra})


NONE_WRITTEN = reply(
    opcodes.BATCH_PUT_OK,
    {"success": False, "message": "batch too large for one commit: none of it was written"},
)


def test_insert_many_sends_the_documents_on_the_bulk_lane(mode: str, identity: Any) -> None:
    script = Script({opcodes.BATCH_PUT: batch_ok(["a", "b"], copies_confirmed=2)})
    with public(mode, identity, script) as (pc, _, server):
        result = pc.call(lambda c: bucket(pc, "hash").insert_many([{"_id_": "a"}, {"_id_": "b"}]))
        assert (result.ids, result.count, result.copies_confirmed) == (["a", "b"], 2, 2)
        assert script.of(opcodes.BATCH_PUT)[0]["data"] == [{"_id_": "a"}, {"_id_": "b"}]
        assert pc.client.pool_stats().bulk_idle == 1  # it used the bulk lane
        assert len(server.connections) == 2  # the point connection from connect, and one bulk


def test_a_batch_refused_whole_is_halved_and_the_halves_sent_in_order(
    mode: str, identity: Any
) -> None:
    def answer(doc: dict[str, Any]) -> Any:
        return NONE_WRITTEN if len(doc["data"]) > 2 else batch_ok([d["n"] for d in doc["data"]])

    script = Script({opcodes.BATCH_PUT: answer})
    docs = [{"n": str(i)} for i in range(5)]
    with public(mode, identity, script) as (pc, _, _):
        result = pc.call(lambda c: bucket(pc, "hash").insert_many(docs))
        assert result.ids == ["0", "1", "2", "3", "4"] and result.count == 5
        sizes = [len(d["data"]) for d in script.of(opcodes.BATCH_PUT)]
        assert sizes == [5, 2, 3, 1, 2]  # 5 refused; [0,1]; [2,3,4] refused; [2]; [3,4]


def test_a_later_half_that_fails_reports_what_the_earlier_halves_wrote(
    mode: str, identity: Any
) -> None:
    def answer(doc: dict[str, Any]) -> Any:
        ids = [d["n"] for d in doc["data"]]
        if len(ids) > 2:
            return NONE_WRITTEN
        if ids[0] == "2":
            return reply(opcodes.BATCH_PUT_OK, {"success": False, "message": "disk full"})
        return batch_ok(ids)

    docs = [{"n": str(i)} for i in range(4)]
    with public(mode, identity, Script({opcodes.BATCH_PUT: answer})) as (pc, _, _):
        with pytest.raises(errors.InvalidArgumentError) as caught:
            pc.call(lambda c: bucket(pc, "hash").insert_many(docs))
        assert caught.value.message == "disk full"
        assert caught.value.written_count == 2 and caught.value.written_ids == ["0", "1"]
        assert "docs[written_count:]" in str(caught.value)


def test_a_refusal_that_is_not_none_of_it_was_written_is_never_halved(
    mode: str, identity: Any
) -> None:
    other = reply(opcodes.BATCH_PUT_OK, {"success": False, "message": "document 3 is invalid"})
    script = Script({opcodes.BATCH_PUT: other})
    with public(mode, identity, script) as (pc, _, _):
        with pytest.raises(errors.InvalidArgumentError) as caught:
            pc.call(lambda c: bucket(pc, "hash").insert_many([{"n": 1}, {"n": 2}, {"n": 3}]))
        assert caught.value.written_count is None
        assert len(script.of(opcodes.BATCH_PUT)) == 1  # a duplicate write if it had been halved


def test_a_batch_of_one_that_is_refused_is_raised_as_it_is(mode: str, identity: Any) -> None:
    script = Script({opcodes.BATCH_PUT: NONE_WRITTEN})
    with (
        public(mode, identity, script) as (pc, _, _),
        pytest.raises(errors.InvalidArgumentError, match="none of it was written"),
    ):
        pc.call(lambda c: bucket(pc, "hash").insert_many([{"n": 1}]))
    assert len(script.of(opcodes.BATCH_PUT)) == 1  # sent once, raised as it is


def test_an_error_frame_mid_walk_also_carries_the_written_prefix(mode: str, identity: Any) -> None:
    answers = [NONE_WRITTEN, batch_ok(["0", "1"]), [fakehoard.error(3, "permission denied")]]
    with public(mode, identity, Script({opcodes.BATCH_PUT: answers})) as (pc, _, _):
        with pytest.raises(errors.PermissionDeniedError) as caught:
            pc.call(lambda c: bucket(pc, "hash").insert_many([{"n": i} for i in range(4)]))
        assert caught.value.written_count == 2 and caught.value.written_ids == ["0", "1"]


def test_insert_many_of_nothing_sends_nothing_and_non_lists_are_refused(
    mode: str, identity: Any
) -> None:
    with public(mode, identity) as (pc, script, _):
        empty = pc.call(lambda c: bucket(pc, "hash").insert_many([]))
        assert (empty.ids, empty.count) == ([], 0) and script.requests == []
        for bad in ({"a": 1}, "abc", None, 5):
            with pytest.raises(errors.InvalidArgumentError, match="list of documents"):
                pc.call(partial(_insert_many, pc, bad))
        with pytest.raises(errors.InvalidArgumentError, match="Document 1"):
            pc.call(lambda c: bucket(pc, "hash").insert_many([{"a": 1}, "x"]))
        assert script.requests == []


# --- get, exists, delete -------------------------------------------------------------------------


def found(doc: dict[str, Any]) -> Any:
    return reply(opcodes.GET_RESPONSE, {"found": True, "data": doc, "message": ""})


def test_get_returns_the_document_with_its_id_and_a_miss_is_none(mode: str, identity: Any) -> None:
    script = Script(
        {
            opcodes.GET: [
                found({"Name": "Ada", "_id_": "ada"}),
                reply(opcodes.GET_RESPONSE, {"found": False, "message": "document not found"}),
            ]
        }
    )
    with public(mode, identity, script) as (pc, _, _):
        assert pc.call(lambda c: bucket(pc, "hash").get("ada")) == {"Name": "Ada", "_id_": "ada"}
        assert pc.call(lambda c: bucket(pc, "hash").get("nobody")) is None


@pytest.mark.parametrize(
    "message",
    ["find not supported for bucket type fifo", "pop not supported for bucket type hash", "x"],
)
def test_only_the_listed_messages_are_misses(mode: str, identity: Any, message: str) -> None:
    refusal = reply(opcodes.GET_RESPONSE, {"found": False, "message": message})
    with public(mode, identity, Script({opcodes.GET: refusal})) as (pc, _, _):
        with pytest.raises(errors.InvalidArgumentError) as caught:
            pc.call(lambda c: bucket(pc).get("k"))
        assert caught.value.message == message


def test_a_missing_bucket_or_database_is_not_found_not_a_miss(mode: str, identity: Any) -> None:
    script = Script({opcodes.GET: [fakehoard.error(1, 'bucket not found: bucket "users" ...')]})
    with public(mode, identity, script) as (pc, _, _):
        with pytest.raises(errors.NotFoundError) as caught:
            pc.call(lambda c: bucket(pc, "hash").get("ada"))
        assert "create_bucket" in str(caught.value)


def test_a_consistent_get_sends_consistent_get_and_returns_the_document(
    mode: str, identity: Any
) -> None:
    answer = reply(
        opcodes.CONSISTENT_GET,
        {"found": True, "data": {"Name": "Ada"}, "version": 1, "repaired": False},
    )
    script = Script({opcodes.CONSISTENT_GET: answer})
    with public(mode, identity, script) as (pc, _, _):
        assert pc.call(lambda c: bucket(pc, "hash").get("ada", consistent=True)) == {"Name": "Ada"}
        assert script.of(opcodes.GET) == []


def test_a_blob_miss_message_is_a_miss_for_the_blob_form_only(mode: str, identity: Any) -> None:
    miss = reply(
        opcodes.BLOB_READ_RESPONSE,
        {"found": False, "message": 'blob "nothing.txt" not found in v096.files'},
    )
    with public(mode, identity, Script({opcodes.BLOB_READ: miss})) as (pc, _, _):
        assert pc.call(lambda c: bucket(pc, "blob", "files").get_blob("nothing.txt")) is None


def test_exists(mode: str, identity: Any) -> None:
    answers = [reply(opcodes.EXISTS_RESPONSE, {"exists": True}), reply(opcodes.EXISTS_RESPONSE, {})]
    with public(mode, identity, Script({opcodes.EXISTS: answers})) as (pc, _, _):
        assert pc.call(lambda c: bucket(pc, "hash").exists("a")) is True
        assert pc.call(lambda c: bucket(pc, "hash").exists("b")) is False


def test_exists_on_a_queue_is_the_servers_refusal_passed_through(mode: str, identity: Any) -> None:
    script = Script(
        {opcodes.EXISTS: [fakehoard.error(4, "exists not supported: a fifo has no keys")]}
    )
    with (
        public(mode, identity, script) as (pc, _, _),
        pytest.raises(errors.InvalidArgumentError, match="no keys"),
    ):
        pc.call(lambda c: bucket(pc).exists("a"))


def test_delete_is_true_when_something_went_and_false_for_the_two_miss_messages(
    mode: str, identity: Any
) -> None:
    answers = [
        reply(opcodes.DELETE, {"success": True, "message": "document deleted"}),
        reply(opcodes.DELETE, {"success": False, "message": "document not found"}),
        reply(opcodes.DELETE, {"success": False, "message": "blob not found"}),
    ]
    with public(mode, identity, Script({opcodes.DELETE: answers})) as (pc, _, _):
        assert pc.call(lambda c: bucket(pc).delete("a")) is True
        assert pc.call(lambda c: bucket(pc).delete("a")) is False
        assert pc.call(lambda c: bucket(pc).delete("a")) is False


def test_delete_on_a_fifo_raises_it_does_not_return_false(mode: str, identity: Any) -> None:
    refusal = reply(
        opcodes.DELETE, {"success": False, "message": "delete not supported for bucket type fifo"}
    )
    with (
        public(mode, identity, Script({opcodes.DELETE: refusal})) as (pc, _, _),
        pytest.raises(errors.InvalidArgumentError, match="not supported"),
    ):
        pc.call(lambda c: bucket(pc).delete("a"))


# --- update --------------------------------------------------------------------------------------


UPDATED = reply(opcodes.UPDATE, {"success": True, "doc": {"Name": "Ada", "_id_": "ada"}})


def test_every_operator_is_sent_under_its_wire_name_with_the_right_widths(
    mode: str, identity: Any
) -> None:
    script = Script({opcodes.UPDATE: UPDATED})
    ops = {
        "$set": {"Address.City": "London"},
        "$unset": ["Draft"],
        "$inc": {"Visits": 2},
        "$push": {"Langs": "en"},
        "$addToSet": {"Tags": "maths"},
        "$mul": {"Score": 2},
        "$min": {"Floor": 5},
        "$max": {"Best": 99},
        "$rename": {"Nick": "Handle"},
    }
    with public(mode, identity, script) as (pc, _, _):
        after = pc.call(lambda c: bucket(pc, "hash").update("ada", ops))
        assert after == {"Name": "Ada", "_id_": "ada"}
    (sent,) = script.of(opcodes.UPDATE)
    assert sent["set"] == {"Address.City": "London"} and sent["unset"] == ["Draft"]
    assert sent["inc"] == {"Visits": 2} and sent["push"] == {"Langs": "en"}
    assert sent["addtoset"] == {"Tags": "maths"}  # one word on the wire
    assert sent["mul"] == {"Score": 2.0} and isinstance(sent["mul"]["Score"], float)
    assert (sent["min"], sent["max"], sent["rename"]) == (
        {"Floor": 5},
        {"Best": 99},
        {"Nick": "Handle"},
    )


def test_inc_is_int64_on_the_wire_and_mul_is_a_double() -> None:
    from hoarddb._bson import deserialize
    from hoarddb._core.messages import UpdateRequest, request_payload

    raw = request_payload(
        UpdateRequest(database="d", bucket="b", key="k", inc={"n": 1}, mul={"x": 3.0})
    )
    assert b"\x12n\x00\x01\x00\x00\x00\x00\x00\x00\x00" in raw  # type 0x12 (int64), not 0x10
    assert b"\x01x\x00" in raw  # type 0x01 (double)
    assert deserialize(raw)["inc"] == {"n": 1}


def test_a_missing_key_is_none(mode: str, identity: Any) -> None:
    miss = reply(
        opcodes.UPDATE,
        {
            "success": False,
            "message": 'document "nobody" not found in v059.users — insert it first',
        },
    )
    with public(mode, identity, Script({opcodes.UPDATE: miss})) as (pc, _, _):
        assert pc.call(lambda c: bucket(pc, "hash").update("nobody", {"$set": {"a": 1}})) is None


def test_any_other_in_band_refusal_of_an_update_is_raised(mode: str, identity: Any) -> None:
    refusal = reply(opcodes.UPDATE, {"success": False, "message": "update not supported"})
    with (
        public(mode, identity, Script({opcodes.UPDATE: refusal})) as (pc, _, _),
        pytest.raises(errors.InvalidArgumentError),
    ):
        pc.call(lambda c: bucket(pc, "hash").update("k", {"$set": {"a": 1}}))


@pytest.mark.parametrize(
    ("ops", "needle"),
    [
        ({"$setOnInsert": {"a": 1}}, "the nine are"),
        ({"$foo": {"a": 1}}, "$set, $unset, $inc"),
        ({}, "no operator"),
        ([("$set", {})], "mapping of operators"),
        ({"$set": [1]}, "$set takes a mapping"),
        ({"$unset": {"a": 1}}, "$unset takes a list"),
        ({"$unset": "a"}, "$unset takes a list"),
        ({"$unset": [1]}, "string field names"),
        ({"$inc": {"a": True}}, "$inc adds a whole number"),
        ({"$inc": {"a": 1.5}}, "use $mul"),
        ({"$inc": {"a": 2**63}}, "64-bit range"),
        ({"$mul": {"a": True}}, "$mul multiplies"),
        ({"$mul": {"a": math.nan}}, "finite"),
        ({"$mul": {"a": math.inf}}, "finite"),
        ({"$mul": {"a": "2"}}, "$mul multiplies"),
        ({"$rename": {"a": 3}}, "$rename maps"),
        ({"$push": 5}, "$push takes a mapping"),
        ({"$set": {1: 2}}, "string field names"),
    ],
)
def test_a_bad_update_is_refused_before_anything_is_sent(
    mode: str, identity: Any, ops: Any, needle: str
) -> None:
    with public(mode, identity) as (pc, script, _):
        with pytest.raises(errors.InvalidArgumentError, match="Nothing was sent") as caught:
            pc.call(lambda c: bucket(pc, "hash").update("k", ops))
        assert needle in str(caught.value)
        assert script.requests == []


# --- push, pop, peek -----------------------------------------------------------------------------


PUSH_OK = reply(opcodes.FIFO_PUSH, {"id": "m1", "success": True, "seq": 1})


@pytest.mark.parametrize("facet", ["fifo", "lifo", "heap", "bucket"])
def test_push_sends_the_fifo_family_and_always_an_int64_priority(
    mode: str, identity: Any, facet: str
) -> None:
    script = Script({opcodes.FIFO_PUSH: PUSH_OK})
    with public(mode, identity, script) as (pc, _, _):
        pc.call(lambda c: bucket(pc, facet, "q").push({"n": 1}))
        pc.call(lambda c: bucket(pc, facet, "q").push({"n": 2}, priority=-5, write_concern="all"))
        first, second = script.of(opcodes.FIFO_PUSH)
        assert first["priority"] == 0 and second["priority"] == -5
        assert second["writeconcern"] == "all"
        assert first["data"] == {"n": 1}
        assert script.of(opcodes.HEAP_PUSH) == [] and script.of(opcodes.LIFO_PUSH) == []


def test_the_priority_goes_out_as_int64_even_when_it_is_small() -> None:
    from hoarddb._core.messages import FifoPushRequest, request_payload

    raw = request_payload(FifoPushRequest(database="d", bucket="q", data={"n": 1}, priority=0))
    assert b"\x12priority\x00" in raw  # int64, never int32


@pytest.mark.parametrize("bad", [True, 1.5, "1", 2**63, -(2**63) - 1])
def test_a_bad_priority_is_refused_locally(mode: str, identity: Any, bad: Any) -> None:
    with public(mode, identity) as (pc, script, _):
        with pytest.raises(errors.InvalidArgumentError, match="priority"):
            pc.call(lambda c: bucket(pc, "heap", "h").push({"n": 1}, priority=bad))
        assert script.requests == []


def test_pop_returns_the_document_or_none_and_is_a_write(mode: str, identity: Any) -> None:
    answers = [
        reply(opcodes.FIFO_POP_RESPONSE, {"found": True, "data": {"n": 1, "_id_": "m1"}}),
        reply(opcodes.FIFO_POP_RESPONSE, {"found": False, "message": "queue is empty"}),
        reply(opcodes.FIFO_POP_RESPONSE, {"found": False, "message": "heap is empty"}),
    ]
    script = Script({opcodes.FIFO_POP: answers})
    with public(mode, identity, script, write_concern="one") as (pc, _, _):
        assert pc.call(lambda c: bucket(pc, "fifo", "q").pop()) == {"n": 1, "_id_": "m1"}
        assert pc.call(lambda c: bucket(pc, "fifo", "q").pop()) is None
        assert pc.call(lambda c: bucket(pc, "heap", "h").pop(write_concern="majority")) is None
        assert [d["writeconcern"] for d in script.of(opcodes.FIFO_POP)] == [
            "one",
            "one",
            "majority",
        ]


def test_pop_on_a_hash_bucket_is_the_servers_refusal_not_none(mode: str, identity: Any) -> None:
    refusal = reply(
        opcodes.FIFO_POP_RESPONSE,
        {"found": False, "message": "pop not supported for bucket type hash"},
    )
    with (
        public(mode, identity, Script({opcodes.FIFO_POP: refusal})) as (pc, _, _),
        pytest.raises(errors.InvalidArgumentError, match="pop not supported"),
    ):
        pc.call(lambda c: bucket(pc).pop())


def test_a_pop_is_never_retried_after_the_connection_is_lost(mode: str, identity: Any) -> None:
    script = Script({opcodes.FIFO_POP: [fakehoard.Close()]})
    with public(mode, identity, script) as (pc, _, _):
        with pytest.raises(errors.ConnectionFailedError) as caught:
            pc.call(lambda c: bucket(pc, "fifo", "q").pop())
        assert caught.value.write_outcome == "unknown" and not caught.value.retryable
        assert "do not resend a push, a pop" in str(caught.value)
        assert len(script.of(opcodes.FIFO_POP)) == 1


def test_a_put_is_never_retried_after_an_unknown_unavailable(mode: str, identity: Any) -> None:
    script = Script({opcodes.PUT: [fakehoard.error(6, "no holder", write_outcome="unknown")]})
    with public(mode, identity, script) as (pc, _, _):
        with pytest.raises(errors.UnavailableError) as caught:
            pc.call(lambda c: bucket(pc, "hash").put("k", {}))
        assert caught.value.write_outcome == "unknown" and not caught.value.retryable
        assert len(script.of(opcodes.PUT)) == 1


def test_peek_returns_documents_in_pop_order_and_empty_is_a_list(mode: str, identity: Any) -> None:
    answers = [
        reply(opcodes.FIFO_PEEK, {"found": True, "items": [{"n": 1}, {"n": 2}]}),
        reply(opcodes.FIFO_PEEK, {"found": False, "message": "queue is empty"}),
    ]
    script = Script({opcodes.FIFO_PEEK: answers})
    with public(mode, identity, script) as (pc, _, _):
        assert pc.call(lambda c: bucket(pc, "fifo", "q").peek(limit=2)) == [{"n": 1}, {"n": 2}]
        assert pc.call(lambda c: bucket(pc, "fifo", "q").peek()) == []
        assert [d["limit"] for d in script.of(opcodes.FIFO_PEEK)] == [2, 1]


@pytest.mark.parametrize("limit", [0, -1, 1001, True, 1.5, "3"])
def test_peek_limit_is_one_to_a_thousand(mode: str, identity: Any, limit: Any) -> None:
    with public(mode, identity) as (pc, script, _):
        with pytest.raises(errors.InvalidArgumentError, match="1 to 1000"):
            pc.call(lambda c: bucket(pc, "fifo", "q").peek(limit=limit))
        assert script.requests == []
    empty = reply(opcodes.FIFO_PEEK, {"found": False, "message": "queue is empty"})
    with public(mode, identity, Script({opcodes.FIFO_PEEK: empty})) as (
        pc,
        _,
        _,
    ):
        for good in (1, 1000):
            pc.call(partial(_peek, pc, good))


def test_peek_on_a_hash_bucket_is_the_servers_refusal(mode: str, identity: Any) -> None:
    refusal = reply(opcodes.FIFO_PEEK, {"found": False, "message": "peek not supported for hash"})
    with (
        public(mode, identity, Script({opcodes.FIFO_PEEK: refusal})) as (pc, _, _),
        pytest.raises(errors.InvalidArgumentError),
    ):
        pc.call(lambda c: bucket(pc).peek())


# --- blobs ---------------------------------------------------------------------------------------


def test_put_blob_sends_the_bytes_and_metadata_on_the_bulk_lane(mode: str, identity: Any) -> None:
    answer = reply(
        opcodes.BLOB_WRITE,
        {"success": True, "key": "hello.txt", "blobid": "b-1", "size": 14, "copies_confirmed": 2},
    )
    script = Script({opcodes.BLOB_WRITE: answer})
    with public(mode, identity, script) as (pc, _, _):
        result = pc.call(
            lambda c: bucket(pc, "blob", "files").put_blob(
                "hello.txt", b"hello, hoardDB", metadata={"type": "text"}
            )
        )
        assert (result.key, result.blob_id, result.size, result.copies_confirmed) == (
            "hello.txt",
            "b-1",
            14,
            2,
        )
        (sent,) = script.of(opcodes.BLOB_WRITE)
        assert sent["data"] == b"hello, hoardDB" and sent["metadata"] == {"type": "text"}
        assert pc.client.pool_stats().bulk_idle == 1


def test_get_blob_returns_bytes_metadata_and_ids(mode: str, identity: Any) -> None:
    answer = reply(
        opcodes.BLOB_READ_RESPONSE,
        {"found": True, "blobid": "b-1", "data": b"hello", "size": 5, "metadata": {"type": "text"}},
    )
    with public(mode, identity, Script({opcodes.BLOB_READ: answer})) as (pc, _, _):
        blob = pc.call(lambda c: bucket(pc, "blob", "files").get_blob("hello.txt"))
        assert (blob.key, blob.blob_id, blob.size, blob.data, blob.metadata) == (
            "hello.txt",
            "b-1",
            5,
            b"hello",
            {"type": "text"},
        )


@pytest.mark.parametrize("data", ["text", 5, None, {"a": 1}])
def test_a_blob_is_bytes(mode: str, identity: Any, data: Any) -> None:
    with public(mode, identity) as (pc, script, _):
        with pytest.raises(errors.InvalidArgumentError, match="bytes"):
            pc.call(lambda c: bucket(pc, "blob", "files").put_blob("k", data))
        assert script.requests == []


def test_a_bytearray_or_memoryview_is_accepted_as_bytes(mode: str, identity: Any) -> None:
    answer = reply(opcodes.BLOB_WRITE, {"success": True, "key": "k", "blobid": "b", "size": 3})
    script = Script({opcodes.BLOB_WRITE: answer})
    with public(mode, identity, script) as (pc, _, _):
        pc.call(lambda c: bucket(pc, "blob", "files").put_blob("k", bytearray(b"abc")))
        pc.call(lambda c: bucket(pc, "blob", "files").put_blob("k", memoryview(b"abc")))
        assert [d["data"] for d in script.of(opcodes.BLOB_WRITE)] == [b"abc", b"abc"]


# --- documents round trip through the codec ------------------------------------------------------


def test_python_values_survive_a_put_and_a_get_decode(mode: str, identity: Any) -> None:
    import datetime
    import decimal
    import uuid

    doc = {
        "s": "text",
        "i": 5,
        "big": 2**40,
        "f": 1.5,
        "b": True,
        "n": None,
        "l": [1, "two", {"three": 3}],
        "d": {"nested": {"deep": [1, 2]}},
        "t": datetime.datetime(2026, 10, 13, 12, 0, tzinfo=datetime.UTC),
        "dec": decimal.Decimal("12345.6789"),
        "u": uuid.UUID(int=7),
        "raw": b"\x00\x01",
        "oid": Opaque("objectid", bytes(range(12))),
    }
    stored: dict[str, Any] = {}

    def keep(request: dict[str, Any]) -> Any:
        stored.update(request["data"])
        return PUT_OK

    def give_back(request: dict[str, Any]) -> Any:
        return found(stored)

    script = Script({opcodes.PUT: keep, opcodes.GET: give_back})
    with public(mode, identity, script) as (pc, _, _):
        pc.call(lambda c: bucket(pc, "hash").put("k", doc))
        assert script.of(opcodes.PUT)[0]["data"] == doc  # what went out decodes to what went in
        assert pc.call(lambda c: bucket(pc, "hash").get("k")) == doc  # and comes back as it was


# --- per-call timeouts are bounded -----------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        *(pytest.param(v, id=str(v)) for v in (math.inf, -math.inf, math.nan, -1.0, 0, 1e300)),
        *(pytest.param(v, id=str(v)) for v in (3601, True, "5")),
        pytest.param(10**400, id="huge-int"),
    ],
)
def test_a_per_call_timeout_that_is_not_a_bounded_positive_number_is_refused_locally(
    mode: str, identity: Any, bad: Any
) -> None:
    script = Script({opcodes.PUT: PUT_OK})
    with public(mode, identity, script) as (pc, _, _):
        with pytest.raises(errors.InvalidArgumentError, match=r"timeout=.*Nothing was sent"):
            pc.call(lambda c: bucket(pc, "hash").put("ada", {"Name": "Ada"}, timeout=bad))
        with pytest.raises(errors.InvalidArgumentError, match="timeout="):
            pc.call(lambda c: c.ping(timeout=bad))
        assert script.of(opcodes.PUT) == []
        pc.call(lambda c: bucket(pc, "hash").put("ada", {"Name": "Ada"}, timeout=3600))
