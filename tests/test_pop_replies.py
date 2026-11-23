# SPDX-License-Identifier: Apache-2.0
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false, reportUnknownLambdaType=false
"""A pop answers a list (wire.md section 4): the reply bytes below were recorded from a hoardDB
v1.1.0 server and are replayed here byte for byte, through both surfaces.

A pop removes what it returns, so a driver that cannot read the answer loses the entry: these
tests pin that every entry the server says it removed reaches the caller.
"""

from __future__ import annotations

from typing import Any

import pytest

import fakehoard
from clientkit import Script, public, reply
from hoarddb import errors
from hoarddb._core import opcodes

FIFO_POP_ONE = bytes.fromhex(
    "6500000008666f756e640001046974656d7300430000000330003b000000025f69645f00250000003031"
    "6132303738352d373435342d373363642d626136662d36313837626135626538646500106e0001000000"
    "0000026d65737361676500010000000000"
)
FIFO_POP_TWO = bytes.fromhex(
    "a300000008666f756e640001046974656d7300810000000330003b000000025f69645f00250000003031"
    "6132303738352d373435342d376163342d396534622d39373338613430323930633500106e0002000000"
    "000331003b000000025f69645f002500000030316132303738352d373435342d376435392d613838652d"
    "38396366343338376661366600106e00030000000000026d65737361676500010000000000"
)
LIFO_POP_ONE = bytes.fromhex(
    "6500000008666f756e640001046974656d7300430000000330003b000000025f69645f00250000003031"
    "6132303738352d373435662d373435652d623135612d61656166373761376136306400106e0004000000"
    "0000026d65737361676500010000000000"
)
HEAP_POP_ONE = bytes.fromhex(
    "6500000008666f756e640001046974656d7300430000000330003b000000025f69645f00250000003031"
    "6132303738352d373436342d373463372d393563302d31396465303439623831333400106e0004000000"
    "0000026d65737361676500010000000000"
)
QUEUE_EMPTY = bytes.fromhex(
    "2900000008666f756e640000026d657373616765000f000000717565756520697320656d7074790000"
)
HEAP_EMPTY = bytes.fromhex(
    "2800000008666f756e640000026d657373616765000e0000006865617020697320656d7074790000"
)
FIELD_POP_ONE = bytes.fromhex(
    "8e0000000873756363657373000108666f756e640001126c656e000000000000000000046974656d7300"
    "630000000330005b00000003656c656d656e74003b000000025f69645f00250000003031613230373835"
    "2d373462352d373835652d623132312d38343838623961363338663700106e00010000000012706f7369"
    "74696f6e000000000000000000000000"
)
FIELD_POP_TWO = bytes.fromhex(
    "ec0000000873756363657373000108666f756e640001126c656e000000000000000000046974656d7300"
    "c10000000330005b00000003656c656d656e74003b000000025f69645f00250000003031613230373835"
    "2d373462362d373265372d626638372d36636363326432653631336600106e00020000000012706f7369"
    "74696f6e000100000000000000000331005b00000003656c656d656e74003b000000025f69645f002500"
    "000030316132303738352d373462372d373164632d393032662d36383334326438616434373400106e00"
    "030000000012706f736974696f6e000200000000000000000000"
)
FIELD_HEAP_POP_ONE = bytes.fromhex(
    "c10000000873756363657373000108666f756e640001126c656e000000000000000000046974656d7300"
    "960000000330008e00000003656c656d656e74003b000000025f69645f00250000003031613230373835"
    "2d373462622d376132382d613734392d62613835366332333163323800106e000400000000127072696f"
    "7269747900040000000000000002656e7472795f6964002500000030316132303738352d373462622d37"
    "6132382d613734392d62613835366332333163323800000000"
)
FIELD_EMPTY = bytes.fromhex(
    "240000000873756363657373000108666f756e640000126c656e00000000000000000000"
)
FIELD_PEEK_ONE = bytes.fromhex(
    "8e0000000873756363657373000108666f756e640001126c656e000000000000000000046974656d7300"
    "630000000330005b00000003656c656d656e74003b000000025f69645f00250000003031613230373835"
    "2d373462372d373837642d623033392d61306565383531363336333300106e00040000000012706f7369"
    "74696f6e000300000000000000000000"
)

HASH_REFUSAL = (
    "pop is not defined for hash buckets: a hash has no order to take from, so there is no "
    '"next" to remove.'
)
LIMIT_REFUSAL = "invalid pop limit: 101 is above the pop maximum of 100."


def recorded(opcode: int, payload: bytes) -> list[fakehoard.Action]:
    return [fakehoard.Reply(opcode, payload)]


def bucket(pc: Any, facet: str, name: str = "q") -> Any:
    return getattr(pc.client.database("app"), facet)(name)


def field(pc: Any, name: str = "Purchases") -> Any:
    return pc.client.database("app").hash("User").field("ada", name)


# --- bucket pop ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("facet", "payload", "n"),
    [
        ("fifo", FIFO_POP_ONE, 1),
        ("lifo", LIFO_POP_ONE, 4),
        ("heap", HEAP_POP_ONE, 4),
        ("bucket", FIFO_POP_ONE, 1),
    ],
    ids=["fifo", "lifo", "heap", "bucket"],
)
def test_pop_returns_the_entry_the_server_removed(
    mode: str, identity: Any, facet: str, payload: bytes, n: int
) -> None:
    script = Script({opcodes.FIFO_POP: recorded(opcodes.FIFO_POP_RESPONSE, payload)})
    with public(mode, identity, script) as (pc, _, _):
        popped = pc.call(lambda c: bucket(pc, facet).pop())
        assert isinstance(popped, list) and len(popped) == 1
        assert popped[0]["n"] == n and popped[0]["_id_"].startswith("01a20785-")
        (sent,) = script.of(opcodes.FIFO_POP)
        assert sent == {"database": "app", "bucket": "q"}  # no limit on the wire means one


def test_pop_with_a_limit_returns_every_entry_in_pop_order(mode: str, identity: Any) -> None:
    script = Script({opcodes.FIFO_POP: recorded(opcodes.FIFO_POP_RESPONSE, FIFO_POP_TWO)})
    with public(mode, identity, script) as (pc, _, _):
        popped = pc.call(lambda c: bucket(pc, "fifo").pop(limit=2, write_concern="majority"))
        assert [doc["n"] for doc in popped] == [2, 3]
        (sent,) = script.of(opcodes.FIFO_POP)
        assert sent == {"database": "app", "bucket": "q", "limit": 2, "writeconcern": "majority"}


@pytest.mark.parametrize(
    ("facet", "payload"), [("fifo", QUEUE_EMPTY), ("heap", HEAP_EMPTY)], ids=["fifo", "heap"]
)
def test_pop_of_an_empty_store_is_an_empty_list(
    mode: str, identity: Any, facet: str, payload: bytes
) -> None:
    script = Script({opcodes.FIFO_POP: recorded(opcodes.FIFO_POP_RESPONSE, payload)})
    with public(mode, identity, script) as (pc, _, _):
        assert pc.call(lambda c: bucket(pc, facet).pop()) == []
        assert pc.call(lambda c: bucket(pc, facet).pop(limit=100)) == []


@pytest.mark.parametrize("limit", [0, -1, 101, True, 1.5, "3"])
def test_pop_limit_is_one_to_a_hundred_and_a_bad_one_sends_nothing(
    mode: str, identity: Any, limit: Any
) -> None:
    with public(mode, identity) as (pc, script, _):
        with pytest.raises(errors.InvalidArgumentError, match="1 to 100") as caught:
            pc.call(lambda c: bucket(pc, "fifo").pop(limit=limit))
        assert "Nothing was sent" in str(caught.value)
        with pytest.raises(errors.InvalidArgumentError, match="1 to 100"):
            pc.call(lambda c: field(pc).pop(limit=limit))
        assert script.requests == []


def test_pop_on_a_bucket_with_no_order_is_the_servers_refusal(mode: str, identity: Any) -> None:
    refusal = [fakehoard.error(4, HASH_REFUSAL, write_outcome="not_applied")]
    with public(mode, identity, Script({opcodes.FIFO_POP: refusal})) as (pc, _, _):
        with pytest.raises(errors.InvalidArgumentError, match="pop is not defined") as caught:
            pc.call(lambda c: bucket(pc, "bucket", "users").pop())
        assert caught.value.write_outcome == "not_applied"


def test_a_limit_the_server_refuses_removed_nothing(mode: str, identity: Any) -> None:
    refusal = [fakehoard.error(4, LIMIT_REFUSAL, write_outcome="not_applied")]
    with public(mode, identity, Script({opcodes.FIFO_POP: refusal})) as (pc, _, _):
        with pytest.raises(errors.InvalidArgumentError, match="pop maximum") as caught:
            pc.call(lambda c: bucket(pc, "fifo").pop(limit=100))
        assert caught.value.write_outcome == "not_applied"


@pytest.mark.parametrize(
    "answer",
    [
        {"found": True, "message": ""},
        {"found": True, "items": [], "message": ""},
        {"found": True, "data": {"n": 1, "_id_": "m1"}, "message": ""},
    ],
    ids=["no items", "empty items", "a single document under data"],
)
def test_a_pop_answer_that_says_found_with_no_entries_is_never_an_empty_result(
    mode: str, identity: Any, answer: dict[str, Any]
) -> None:
    """The server removed something this driver cannot read: that is an error, not "empty"."""
    script = Script({opcodes.FIFO_POP: reply(opcodes.FIFO_POP_RESPONSE, answer)})
    with public(mode, identity, script) as (pc, _, _):
        with pytest.raises(errors.ProtocolError, match="Do not repeat the pop") as caught:
            pc.call(lambda c: bucket(pc, "fifo").pop())
        assert caught.value.write_outcome == "unknown" and not caught.value.retryable
        assert len(script.of(opcodes.FIFO_POP)) == 1


@pytest.mark.parametrize(
    "answer",
    [
        {"success": True, "found": True, "len": 0},
        {"success": True, "found": True, "element": {"Item": "tea"}, "position": 3},
    ],
    ids=["no items", "a single entry at the top level"],
)
def test_a_field_pop_answer_that_says_found_with_no_entries_is_never_an_empty_result(
    mode: str, identity: Any, answer: dict[str, Any]
) -> None:
    script = Script({opcodes.FIELD_POP: reply(opcodes.FIELD_POP, answer)})
    with public(mode, identity, script) as (pc, _, _):
        with pytest.raises(errors.ProtocolError, match="Do not repeat the pop") as caught:
            pc.call(lambda c: field(pc).pop())
        assert caught.value.write_outcome == "unknown"
        assert len(script.of(opcodes.FIELD_POP)) == 1


def test_an_answer_that_says_not_found_and_still_carries_entries_is_not_an_empty_result(
    mode: str, identity: Any
) -> None:
    """``found: false`` with entries beside it contradicts itself. The entries may have been
    removed, so the driver does not pick a reading: it raises, with the outcome unknown."""
    odd = {"found": False, "items": [{"n": 1, "_id_": "m1"}], "message": "queue is empty"}
    field_odd = {"success": True, "found": False, "len": 0, "items": [{"element": {"n": 1}}]}
    script = Script(
        {
            opcodes.FIFO_POP: reply(opcodes.FIFO_POP_RESPONSE, odd),
            opcodes.FIELD_POP: reply(opcodes.FIELD_POP, field_odd),
        }
    )
    with public(mode, identity, script) as (pc, _, _):
        for pop in (lambda c: bucket(pc, "fifo").pop(), lambda c: field(pc).pop()):
            with pytest.raises(errors.ProtocolError, match="Do not repeat the pop") as caught:
                pc.call(pop)
            assert caught.value.write_outcome == "unknown"


@pytest.mark.parametrize(("limit", "said"), [(None, "1 entry"), (7, "up to 7 entries")])
def test_a_pop_whose_answer_is_lost_says_how_many_entries_it_may_have_removed(
    mode: str, identity: Any, limit: int | None, said: str
) -> None:
    script = Script({opcodes.FIFO_POP: [fakehoard.Close()], opcodes.FIELD_POP: [fakehoard.Close()]})
    with public(mode, identity, script) as (pc, _, _):
        for pop in (
            lambda c: bucket(pc, "fifo").pop(limit=limit),
            lambda c: field(pc).pop(limit=limit),
        ):
            with pytest.raises(errors.ConnectionFailedError) as caught:
                pc.call(pop)
            assert caught.value.write_outcome == "unknown" and not caught.value.retryable
            hint = caught.value.hint or ""
            assert f"This pop may have removed {said}" in hint and "peek()" in hint
            assert "do not resend a push, a pop" in hint  # the general sentence stays
        assert len(script.of(opcodes.FIFO_POP)) == 1 and len(script.of(opcodes.FIELD_POP)) == 1


def test_a_pop_the_server_refused_says_nothing_about_removed_entries(
    mode: str, identity: Any
) -> None:
    refusal = [fakehoard.error(4, HASH_REFUSAL, write_outcome="not_applied")]
    with public(mode, identity, Script({opcodes.FIFO_POP: refusal})) as (pc, _, _):
        with pytest.raises(errors.InvalidArgumentError) as caught:
            pc.call(lambda c: bucket(pc, "bucket", "users").pop(limit=5))
        assert "may have removed" not in str(caught.value)


# --- store-field pop and peek --------------------------------------------------------------------


def test_field_pop_returns_the_entry_the_server_removed(mode: str, identity: Any) -> None:
    script = Script({opcodes.FIELD_POP: recorded(opcodes.FIELD_POP, FIELD_POP_ONE)})
    with public(mode, identity, script) as (pc, _, _):
        (entry,) = pc.call(lambda c: field(pc).pop())
        assert entry.element is not None and entry.element["n"] == 1
        assert entry.position == 0
        (sent,) = script.of(opcodes.FIELD_POP)
        assert sent == {"database": "app", "bucket": "User", "key": "ada", "field": "Purchases"}


def test_field_pop_with_a_limit_returns_every_entry_in_pop_order(mode: str, identity: Any) -> None:
    script = Script({opcodes.FIELD_POP: recorded(opcodes.FIELD_POP, FIELD_POP_TWO)})
    with public(mode, identity, script) as (pc, _, _):
        entries = pc.call(lambda c: field(pc).pop(limit=2))
        assert [(e.element["n"], e.position) for e in entries] == [(2, 1), (3, 2)]
        (sent,) = script.of(opcodes.FIELD_POP)
        assert sent["limit"] == 2


def test_field_pop_of_a_heap_field_carries_the_priority_and_the_entry_id(
    mode: str, identity: Any
) -> None:
    script = Script({opcodes.FIELD_POP: recorded(opcodes.FIELD_POP, FIELD_HEAP_POP_ONE)})
    with public(mode, identity, script) as (pc, _, _):
        (entry,) = pc.call(lambda c: field(pc, "Books").pop())
        assert entry.element["n"] == 4 and entry.priority == 4
        assert entry.entry_id == entry.element["_id_"]


def test_field_pop_of_an_empty_field_is_an_empty_list(mode: str, identity: Any) -> None:
    script = Script({opcodes.FIELD_POP: recorded(opcodes.FIELD_POP, FIELD_EMPTY)})
    with public(mode, identity, script) as (pc, _, _):
        assert pc.call(lambda c: field(pc).pop()) == []


def test_field_peek_reads_the_same_list_shape_and_removes_nothing(mode: str, identity: Any) -> None:
    answers = [
        recorded(opcodes.FIELD_PEEK, FIELD_PEEK_ONE),
        recorded(opcodes.FIELD_PEEK, FIELD_EMPTY),
    ]
    script = Script({opcodes.FIELD_PEEK: answers})
    with public(mode, identity, script) as (pc, _, _):
        (entry,) = pc.call(lambda c: field(pc).peek(limit=5))
        assert entry.element["n"] == 4 and entry.position == 3
        assert pc.call(lambda c: field(pc).peek()) == []
