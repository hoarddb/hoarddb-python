# SPDX-License-Identifier: Apache-2.0
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false, reportUnknownLambdaType=false
"""Replaying one ``exchange`` vector against a real server (legs 3 and 9): the checks are shared
by the single-node and the three-node runs.

The public API sends the request where a verb exists; the private raw sender sends the rest. A
recording tap sits between the driver and the server, so the test checks what the driver REALLY
sent (the vector's request rules) and what the server REALLY answered (its response rules, ``vary``
paths by presence and BSON type), and then that the driver's typed result agrees with that answer.
``rewrite`` maps the vector's strings to this run's (ports and keys the harness cannot choose).
"""

from __future__ import annotations

from typing import Any

import ejson
import replay
import replay_api
import respcheck
from hoarddb import errors
from hoarddb._bson import serialize
from hoarddb._core import opcodes
from realkit import Rig, decode
from respcheck import Rewrite

RETRIED_CODES = (6, 13, 14)  # UNAVAILABLE, OVERLOADED, RESULT_TOO_LARGE: the driver may resend


def request_document(vec: dict[str, Any]) -> bytes:
    request = vec["request"]
    if "payload_json" in request:
        return serialize(ejson.to_python(request["payload_json"]))
    return bytes.fromhex(request["payload_hex"])


def error_problems(
    vec: dict[str, Any], payload: bytes, rewrite: Rewrite | None = None
) -> list[str]:
    """An ``ERROR`` or ``AUTH_FAIL`` answer: the code, the stable parts of the sentence, and the
    other fields (the whole sentence is not compared: only ``message_contains`` is stable)."""
    doc = decode(payload)
    expected = vec.get("error") or vec["auth_fail"]
    problems: list[str] = []
    code = doc.get("code", doc.get("reason"))
    if code != expected.get("code", expected.get("reason")):
        problems.append(f"code {code!r}, vector has {expected}")
    for fragment in expected.get("message_contains", []):
        if fragment not in str(doc.get("message", "")):
            problems.append(f"message {doc.get('message')!r} lacks {fragment!r}")
    shaped = dict(vec)
    shaped["response"] = {
        **vec["response"],
        "payload_json": {
            k: v for k, v in vec["response"]["payload_json"].items() if k != "message"
        },
    }
    without_message = serialize({k: v for k, v in doc.items() if k != "message"})
    problems += respcheck.response_problems(shaped, without_message, rewrite)
    return problems


def check_answer(
    vec: dict[str, Any], opcode: int, payload: bytes, rewrite: Rewrite | None = None
) -> None:
    want = vec["response"]
    assert opcodes.NAMES.get(opcode, hex(opcode)) == want["opcode_name"], (
        f"{vec['name']}: answered {opcodes.NAMES.get(opcode)}, vector has {want['opcode_name']}"
    )
    if want["opcode_name"] in ("ERROR", "AUTH_FAIL"):
        problems = error_problems(vec, payload, rewrite)
    else:
        problems = respcheck.response_problems(vec, payload, rewrite)
    assert not problems, f"{vec['name']}: " + "; ".join(problems)


def run_setup(rig: Rig, vec: dict[str, Any]) -> None:
    for step in vec.get("setup", []):
        reply = rig.raw(step["opcode"], step["opcode_name"], ejson.to_python(step["payload_json"]))
        doc = decode(reply.payload)
        assert doc.get("success", True) is not False, f"setup {step['opcode_name']}: {doc}"


def replay_on_rig(vec: dict[str, Any], mode: str, rig: Rig, rewrite: Rewrite | None = None) -> None:
    run_setup(rig, vec)
    rig.tap.clear()
    request = vec["request"]
    payload = ejson.to_python(request["payload_json"]) if "payload_json" in request else {}
    adapter = replay_api.ADAPTERS[request["opcode_name"]]
    used_raw = {"yes": False}

    def go(client: Any) -> Any:
        sent = adapter(client, payload, vec)
        if sent == replay_api.RAW:
            used_raw["yes"] = True
            return replay_api.send_raw(client, vec, payload)
        return sent

    failure: errors.HoardDBError | None = None
    result: Any = None
    try:
        result = rig.call(go)
    except errors.HoardDBError as caught:
        failure = caught

    # what the driver sent
    sent = [f for f in rig.tap.requests() if f.opcode == request["opcode"]]
    assert sent, (
        f"{vec['name']}: the driver sent no {request['opcode_name']} (failure: {failure!r})"
    )
    if "payload_json" in request:
        replay.check_request(vec, decode(sent[0].payload))
    else:  # an empty request, or raw bytes on purpose
        assert sent[0].payload == bytes.fromhex(request.get("payload_hex", ""))
    if vec["response"]["opcode_name"] != "ERROR" or vec["error"]["code"] not in RETRIED_CODES:
        assert len(sent) == 1, f"{vec['name']}: {len(sent)} requests, one expected"

    # what the server answered
    answer = rig.tap.answer_to(sent[0])
    assert answer is not None, f"{vec['name']}: the server never answered"
    check_answer(vec, answer.opcode, answer.payload, rewrite)
    wire = decode(answer.payload)

    # what the driver made of it
    if vec["response"]["opcode_name"] == "ERROR":
        cls = replay.ERROR_CLASSES.get(vec["error"]["code"], errors.HoardDBError)
        assert isinstance(failure, cls), f"{vec['name']}: raised {failure!r}, want {cls.__name__}"
        assert failure.wire_code == vec["error"]["code"]
        assert failure.message == wire["message"], (
            "the server's sentence must pass through verbatim"
        )
    elif failure is not None:
        refused = (errors.InvalidArgumentError, errors.NotFoundError)
        assert replay_api_in_band(vec), f"{vec['name']}: the driver raised {failure!r}"
        assert isinstance(failure, refused)
        assert failure.message == wire["message"]
    else:
        check = replay_api.EXPECT.get(request["opcode_name"])
        if check is not None and not used_raw["yes"]:
            expected = ejson.to_python(vec["response"]["payload_json"])
            if rewrite is not None:
                expected = respcheck.rewritten(expected, rewrite)
            check(result, respcheck.wild(expected, vec["response"].get("vary", [])))


def replay_api_in_band(vec: dict[str, Any]) -> bool:
    """A success frame whose payload refuses (the replay module's own definition)."""
    import test_replay

    return bool(test_replay.in_band_failure(vec))
