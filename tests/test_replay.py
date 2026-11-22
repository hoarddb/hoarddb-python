# SPDX-License-Identifier: Apache-2.0
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false, reportUnknownLambdaType=false
"""Legs 1 and 3 without a server: every exchange vector the public API can send, replayed through
both surfaces against a fake server that answers with the vector's captured response bytes."""

from __future__ import annotations

import re
from typing import Any

import pytest

import ejson
import fakehoard
import replay
import replay_api
from clientkit import PC
from hoarddb import errors
from hoarddb._core import opcodes, retry
from vectors import driver_vectors

# Vectors this module replays (more are added with each step of the build).
OPCODES = {
    "PING", "CREATE_DB", "DELETE_DB", "LIST_DATABASES", "CREATE_BUCKET", "DELETE_BUCKET",
    "LIST_BUCKETS", "DECLARE_STORE_FIELDS", "BUCKET_STATS", "BUCKET_INFO", "HEALTH_CHECK",
    "LIST_NODES", "PUT", "GET", "CONSISTENT_GET", "EXISTS", "DELETE", "UPDATE", "BATCH_PUT",
    "FIFO_PUSH", "FIFO_POP", "FIFO_PEEK", "BLOB_WRITE", "BLOB_READ", "BTREE_INSERT",
    "BTREE_LOOKUP", "LIFO_PUSH", "HEAP_PUSH", "LIFO_POP", "HEAP_POP", "LIFO_PEEK", "SCAN",
    "SEARCH", "COUNT", "DISTINCT", "BTREE_RANGE", "BTREE_FIRST", "BTREE_LAST",
    "FIELD_PUSH", "FIELD_POP", "FIELD_PEEK", "FIELD_PUT", "FIELD_GET", "FIELD_DELETE", "FIELD_LEN",
    "FIELD_LIST",
}  # fmt: skip

# Connection-level vectors that no public verb produces: the client always sends AUTH first.
UNREACHABLE = {
    "ping before authentication",
    "request before authentication",
    "a request payload that is not BSON",
}


def selected() -> list[Any]:
    out = []
    for vec in driver_vectors(kind="exchange"):
        if vec["request"]["opcode_name"] in OPCODES and vec["name"] not in UNREACHABLE:
            out.append(pytest.param(vec, id=vec["name"]))
    return out


MISSES = {"document not found", "queue is empty", "heap is empty", "blob not found"}
MISS_SHAPES = (re.compile(r'^blob ".*" not found in '), re.compile(r'^document ".*" not found in '))


def in_band_failure(vec: dict[str, Any]) -> bool:
    """A success frame whose payload refuses: ``success`` or ``found`` false with a sentence that
    is not one of the listed misses (errors.md section 3)."""
    payload = vec["response"].get("payload_json") or {}
    if vec["response"]["opcode_name"] == "ERROR":
        return False
    if vec["response"]["opcode_name"].startswith("FIELD_"):  # a field miss is ``found: false``
        return payload.get("success") is False
    if payload.get("success") is not False and payload.get("found") is not False:
        return False
    message = payload.get("message", "")
    return not (message in MISSES or any(shape.match(message) for shape in MISS_SHAPES))


@pytest.mark.parametrize("vec", selected())
def test_the_vector_replays_through_the_public_api(
    vec: dict[str, Any], mode: str, identity: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    if vec["response"]["opcode_name"] == "ERROR" and vec["error"]["code"] in (6, 13):
        # The driver retries these with backoff until its deadline, on the real clock: the last
        # attempt then races the deadline and can end as a timeout instead of the server's error.
        # Here the question is only how the answer is mapped, so a wait that never fits means one
        # request and the server's own error (the retry rows are tested in test_retry.py).
        monkeypatch.setattr(retry, "backoff_delay", lambda failures, jitter: 1e6)
    handler = replay.Replay(vec)
    with fakehoard.FakeHoard(
        identity,
        handler=handler,
        on_auth=lambda c, d: [fakehoard.auth_ok(capabilities=("auth_token", "bucket_rf"))],
    ) as server:
        pc = PC(mode, server, identity, request_timeout=0.6)
        try:
            payload = (
                ejson.to_python(vec["request"]["payload_json"])
                if "payload_json" in vec["request"]
                else {}
            )
            adapter = replay_api.ADAPTERS[vec["request"]["opcode_name"]]

            raw = {"used": False}

            def run() -> Any:
                def go(c: Any) -> Any:
                    sent = adapter(c, payload, vec)
                    if sent == replay_api.RAW:
                        raw["used"] = True
                        return replay_api.send_raw(c, vec, payload)
                    return sent

                return pc.call(go)

            if in_band_failure(vec):
                expected = ejson.to_python(vec["response"]["payload_json"])["message"]
                failed: errors.HoardDBError | None = None
                try:
                    run()
                except (errors.InvalidArgumentError, errors.NotFoundError) as caught:
                    failed = caught
                if failed is None:
                    # only the raw sender may see an in-band failure as an ordinary answer
                    assert raw["used"], "an in-band failure must be raised by a public verb"
                else:
                    assert failed.message == expected
            else:
                result = replay.check_outcome(vec, run)
                check = replay_api.EXPECT.get(vec["request"]["opcode_name"])
                if (
                    check is not None
                    and not raw["used"]
                    and vec["response"]["opcode_name"] != "ERROR"
                ):
                    check(result, ejson.to_python(vec["response"]["payload_json"]))
            halved = vec["response"]["opcode_name"] == "ERROR" and vec["error"]["code"] == 14
            if halved:  # RESULT_TOO_LARGE: the cursor halves its page until one document is left
                assert len(handler.seen) >= 1
            else:
                assert len(handler.seen) == 1, "exactly one request after AUTH"
        finally:
            pc.close()


def test_every_selected_vector_exists() -> None:
    names = {v["request"]["opcode_name"] for v in driver_vectors(kind="exchange")}
    assert names >= OPCODES and opcodes.PING == 0xA0
    assert len(selected()) >= 45
