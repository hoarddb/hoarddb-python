# SPDX-License-Identifier: Apache-2.0
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false, reportUnknownLambdaType=false
"""Replay the frozen exchange vectors through the public API against a fake server.

The fake server answers a vector's request with the vector's captured response bytes, after
checking the request the driver sent against the vector (testing.md section 1.1: every field of
``payload_json`` present with an equal value, nothing outside ``allowed_fields``, empty fields may
be absent, integers equal by value). So one vector checks, with no real server: the public verb
builds the right request, and decodes the real answer into its typed result or its error.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

import pytest

import ejson
import fakehoard
from clientkit import PC
from hoarddb import errors
from hoarddb._bson import deserialize
from hoarddb._core import frame, opcodes

Vector = dict[str, Any]

ERROR_CLASSES: dict[int, type[errors.HoardDBError]] = {
    1: errors.NotFoundError,
    2: errors.AlreadyExistsError,
    3: errors.PermissionDeniedError,
    4: errors.InvalidArgumentError,
    6: errors.UnavailableError,
    7: errors.WriteConcernTimeoutError,
    8: errors.ConflictError,
    9: errors.QuotaExceededError,
    10: errors.UnsupportedVersionError,
    12: errors.NotAuthenticatedError,
    13: errors.OverloadedError,
    14: errors.ResultTooLargeError,
    15: errors.DuplicateKeyError,
    16: errors.FieldTypeError,
    17: errors.BatchTooLargeError,
    18: errors.DocumentExistsError,
}


def _empty(value: Any) -> bool:
    return value in (None, "", 0, False, [], {}, b"")


def _equal(expected: Any, actual: Any) -> bool:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        return (
            all(
                (k in actual and _equal(v, actual[k])) or (k not in actual and _empty(v))
                for k, v in expected.items()
            )
            and set(actual) <= set(expected) | set()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(expected) == len(actual)
            and all(_equal(e, a) for e, a in zip(expected, actual, strict=True))
        )
    if isinstance(expected, bool) or isinstance(actual, bool):
        return expected is actual
    if isinstance(expected, int) and isinstance(actual, int):
        return expected == actual
    return bool(expected == actual)


# A cursor always asks for a page (``limit`` is the page size, 100 by default); a vector that gives
# no limit asks for the whole answer. The page size is the driver's paging, not part of the verb.
PAGED = {"SCAN", "BTREE_RANGE"}


def check_request(vec: Vector, doc: dict[str, Any]) -> None:
    """The decoded request the driver sent against the vector's rules."""
    request = vec["request"]
    expected = ejson.to_python(request["payload_json"])
    if request["opcode_name"] in PAGED and _empty(expected.get("limit")):
        doc = {k: v for k, v in doc.items() if k != "limit"}
    allowed = set(request.get("allowed_fields", []))
    outside = set(doc) - allowed
    assert not outside, f"{vec['name']}: fields outside allowed_fields: {sorted(outside)}"
    for key, value in expected.items():
        if key in doc:
            assert _equal(value, doc[key]), f"{vec['name']}: {key}: {doc[key]!r} != {value!r}"
        else:
            assert _empty(value), f"{vec['name']}: {key} is missing (expected {value!r})"


class Replay:
    """A handler that serves one vector's response and records what it was sent."""

    def __init__(self, vec: Vector) -> None:
        self.vec = vec
        self.seen: list[frame.Frame] = []

    def __call__(self, conn: fakehoard.Conn, request: frame.Frame) -> list[fakehoard.Action]:
        self.seen.append(request)
        want = self.vec["request"]
        assert request.opcode == want["opcode"], (
            f"{self.vec['name']}: sent {opcodes.NAMES.get(request.opcode)}, "
            f"vector wants {want['opcode_name']}"
        )
        if "payload_json" in want:
            check_request(self.vec, deserialize(request.payload) if request.payload else {})
        elif "payload_hex" in want:
            assert request.payload == bytes.fromhex(want["payload_hex"])
        response = self.vec["response"]
        return [fakehoard.Reply(response["opcode"], bytes.fromhex(response["payload_hex"]))]


def check_outcome(vec: Vector, call: Callable[[], Any]) -> Any:
    """Run the call; an ERROR vector must raise the class its code names, with the server's
    sentence verbatim, and a success vector must not raise. Returns the result."""
    response = vec["response"]
    if response["opcode_name"] != "ERROR":
        return call()
    expected = vec["error"]
    cls = ERROR_CLASSES.get(expected["code"], errors.HoardDBError)
    with pytest.raises(errors.HoardDBError) as info:
        call()
    caught = info.value
    assert isinstance(caught, cls), f"{vec['name']}: {type(caught).__name__}, want {cls}"
    assert caught.wire_code == expected["code"]
    for fragment in expected.get("message_contains", []):
        assert fragment in caught.message, (vec["name"], fragment, caught.message)
    payload = ejson.to_python(response["payload_json"])
    assert caught.message == payload["message"], "the sentence must pass through verbatim"
    return caught


def call_adapter(pc: PC, fn: Callable[[Any], Any]) -> Callable[[], Any]:
    return lambda: pc.call(fn)


def awaitable(value: Any) -> bool:
    return inspect.isawaitable(value)
