# SPDX-License-Identifier: Apache-2.0
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false
"""The real-server legs trust ``respcheck`` to say when an answer disagrees with a vector, so it is
tested here, without a server, against doctored copies of a real vector's answer."""

from __future__ import annotations

from typing import Any

import pytest

import respcheck
import vectors
from hoarddb._bson import deserialize, serialize


def vector(name: str) -> dict[str, Any]:
    (found,) = [v for v in vectors.driver_vectors(kind="exchange") if v["name"] == name]
    return found


def answer(vec: dict[str, Any]) -> dict[str, Any]:
    return deserialize(bytes.fromhex(vec["response"]["payload_hex"]))


def test_the_captured_answer_agrees_with_its_own_vector() -> None:
    vec = vector("put on a hash bucket")
    assert respcheck.response_problems(vec, bytes.fromhex(vec["response"]["payload_hex"])) == []


def test_a_varying_value_may_differ_but_a_fixed_one_may_not() -> None:
    vec = vector("put on a hash bucket")
    doc = answer(vec)
    assert "seq" in vec["response"]["vary"]
    wider = serialize({**doc, "seq": 2**40})  # a different value, still an int64
    assert respcheck.response_problems(vec, wider) == []
    problems = respcheck.response_problems(
        vec, serialize({**doc, "seq": 2**40, "id": "someone-else"})
    )
    assert any(p.startswith("id:") for p in problems)


def test_a_varying_value_of_another_bson_type_is_reported() -> None:
    vec = vector("put on a hash bucket")
    doc = answer(vec)
    problems = respcheck.response_problems(vec, serialize({**doc, "seq": "one"}))
    assert any(p.startswith("seq: BSON type") for p in problems)


def test_a_missing_varying_field_is_reported() -> None:
    vec = vector("put on a hash bucket")
    doc = {k: v for k, v in answer(vec).items() if k != "seq"}
    problems = respcheck.response_problems(vec, serialize(doc))
    assert any(p.startswith("seq:") for p in problems)


def test_a_field_the_vector_does_not_list_is_reported() -> None:
    vec = vector("put on a hash bucket")
    problems = respcheck.response_problems(vec, serialize({**answer(vec), "surprise": 1}))
    assert any("surprise" in p for p in problems)


def test_an_additive_field_may_be_present() -> None:
    vec = vector("put on a hash bucket")
    assert "copies_confirmed" in vec["response"]["additive_fields"]
    doc = {**answer(vec), "seq": 2**40, "copies_confirmed": 1, "replication_factor": 1}
    assert respcheck.response_problems(vec, serialize(doc)) == []


def test_a_field_listed_as_absent_must_be_absent() -> None:
    vec = {**vector("put on a hash bucket")}
    vec["response"] = {**vec["response"], "absent_fields": ["message"]}
    doc = {**answer(vec), "message": "x"}
    assert any("absent_fields" in p for p in respcheck.response_problems(vec, serialize(doc)))


def test_an_element_type_inside_an_array_of_documents_is_walked() -> None:
    payload = serialize({"nodes": [{"joinedat": 1, "name": "a"}, {"joinedat": 2, "name": "b"}]})
    found = respcheck.typemap(payload)
    assert found["nodes[]"] == {0x03}
    assert found["nodes[].joinedat"] == {0x10}
    assert found["nodes[].name"] == {0x02}


def test_wild_equals_anything_and_replaces_only_the_varying_paths() -> None:
    shaped = respcheck.wild({"id": "a", "seq": 1, "rows": [{"t": 5, "n": 1}]}, ["seq", "rows[].t"])
    assert shaped["seq"] == 12345 and shaped["rows"][0]["t"] == "anything"
    assert shaped["id"] == "a" and shaped["rows"][0]["n"] == 1


@pytest.mark.parametrize("extra", [0, 1.5, "s", None])
def test_wild_is_equal_to_every_kind_of_value(extra: object) -> None:
    assert respcheck.Wild() == extra


# --- GET_FAST: a status byte and a document whose field order the server does not keep ----------
# (`wire.md` section 3: a stored document's fields come back in an order the server chooses, which
# can differ between two documents written alike; a driver compares field by field, never by bytes)


def fast_found() -> tuple[dict[str, Any], bytes]:
    vec = vector("get_fast: found")
    return vec, bytes.fromhex(vec["response"]["payload_hex"])


def test_a_get_fast_answer_in_another_field_order_agrees_with_its_vector() -> None:
    vec, captured = fast_found()
    status, doc = captured[:1], deserialize(captured[1:])
    reordered = status + serialize(dict(reversed(list(doc.items()))))
    assert reordered != captured
    assert respcheck.response_problems(vec, reordered) == []


def test_a_get_fast_answer_with_another_value_or_status_is_reported() -> None:
    vec, captured = fast_found()
    status, doc = captured[:1], deserialize(captured[1:])
    changed = status + serialize({**doc, "Name": "Grace"})
    assert respcheck.response_problems(vec, changed) != []
    assert respcheck.response_problems(vec, b"\x01" + captured[1:]) != []
    assert respcheck.response_problems(vec, b"") != []


def test_a_get_fast_miss_is_a_bare_status_byte() -> None:
    vec = vector("get_fast: not found")
    captured = bytes.fromhex(vec["response"]["payload_hex"])
    assert captured == b"\x01", "a miss carries the status byte and no document"
    assert respcheck.response_problems(vec, captured) == []
    assert respcheck.response_problems(vec, captured + serialize({"a": 1})) != []
    assert respcheck.response_problems(vec, b"\x00") != []
