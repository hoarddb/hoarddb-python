# SPDX-License-Identifier: Apache-2.0
"""P5b: Call, Reply and Op: operations are generators that never touch a socket (spec 3.3)."""

from __future__ import annotations

from hoarddb._core import messages
from hoarddb._core.call import Call, Op, Reply


def get(database: str, bucket: str, key: str) -> Op[dict[str, object] | None]:
    reply = yield Call(
        op="get",
        target=f"{database}/{bucket}/{key}",
        request=messages.GetRequest(database=database, bucket=bucket, key=key),
        lane="point",
        kind="READ",
        timeout=None,
    )
    response = reply.response
    assert isinstance(response, messages.GetResponse)
    return response.data if response.found else None


def run(op: Op[object], answer: Reply) -> tuple[Call, object]:
    call = next(op)
    try:
        op.send(answer)
    except StopIteration as done:
        return call, done.value
    raise AssertionError("the operation should have finished")


def test_an_operation_yields_one_call_and_returns_a_value_without_any_io() -> None:
    reply = Reply(messages.GetResponse(found=True, data={"_id_": "ada"}), b"\x01" * 16)
    call, value = run(get("app", "users", "ada"), reply)
    assert value == {"_id_": "ada"}
    assert (call.op, call.target, call.lane, call.kind) == ("get", "app/users/ada", "point", "READ")
    assert call.request.OPCODE == 0x11


def test_a_miss_is_a_value() -> None:
    _, value = run(get("a", "b", "k"), Reply(messages.GetResponse(found=False), bytes(16)))
    assert value is None


def test_calls_and_replies_are_immutable_values() -> None:
    call, _ = run(get("a", "b", "k"), Reply(messages.GetResponse(), bytes(16)))
    try:
        call.op = "x"  # type: ignore[misc]
    except AttributeError:
        pass
    else:
        raise AssertionError("a Call must be frozen")
