# SPDX-License-Identifier: Apache-2.0
"""P4: the server-generated BSON corpus, byte for byte (testing.md section 1.3).

``refusal`` is matched, never ``reason``: an input can break several rules and two correct decoders
may name different ones. ``server_decoder: accepted`` marks inputs the spec is stricter about
(invalid UTF-8, nesting past 100): the driver follows the spec.
"""

from __future__ import annotations

import hashlib
import struct
from datetime import datetime
from typing import Any

import pytest

import bsonvec
from hoarddb import errors
from hoarddb._bson import deserialize, serialize
from vectors import Vector

CORPUS = bsonvec.corpus()


def _of(kind: str) -> list[Vector]:
    return [v for v in CORPUS if v["kind"] == kind]


REFUSAL_CLASS: dict[str, type[errors.HoardDBError]] = {
    "ProtocolError": errors.ProtocolError,
    "InvalidArgumentError": errors.InvalidArgumentError,
}


def test_the_corpus_is_the_one_this_test_was_written_for() -> None:
    header = bsonvec.corpus_header()
    assert header["format"] == "hoarddb-bson-vectors/1"
    assert header["limits"]["max_depth"] == 100
    kinds = {k: len(_of(k)) for k in {v["kind"] for v in CORPUS}}
    assert kinds == {
        "encode": 112,
        "decode": 13,
        "reject": 53,
        "datetime_floor": 11,
        "size": 6,
        "encode_refuse": 1,
    }


@pytest.mark.parametrize("vec", _of("encode"), ids=lambda v: v["name"])
def test_encode_vectors_write_exactly_these_bytes_and_read_back(vec: Vector) -> None:
    document = bsonvec.to_python(vec["document"])
    assert serialize(document).hex() == vec["hex"]
    back = deserialize(bytes.fromhex(vec["hex"]))
    assert bsonvec.canon(back) == bsonvec.canon(bsonvec.to_python(vec["document"], decoded=True))


@pytest.mark.parametrize("vec", _of("decode"), ids=lambda v: v["name"])
def test_decode_vectors_give_the_document_back(vec: Vector) -> None:
    expected = bsonvec.to_python(vec.get("last_wins_document", vec["document"]), decoded=True)
    got = deserialize(bytes.fromhex(vec["hex"]))
    assert bsonvec.canon(got) == bsonvec.canon(expected)
    if (
        vec["reencodes_identically"] and "last_wins_document" not in vec
    ):  # a map cannot repeat a key
        assert serialize(got).hex() == vec["hex"]


@pytest.mark.parametrize("vec", _of("reject"), ids=lambda v: v["name"])
def test_reject_vectors_are_refused_with_the_named_class(vec: Vector) -> None:
    cls = REFUSAL_CLASS[vec["refusal"]]
    with pytest.raises(errors.HoardDBError) as info:
        deserialize(bytes.fromhex(vec["hex"]))
    assert type(info.value) is cls


def test_the_inputs_the_server_accepts_but_the_spec_refuses_are_refused() -> None:
    stricter = [v for v in _of("reject") if v["server_decoder"] == "accepted"]
    assert len(stricter) == 11  # invalid UTF-8 (7) and nesting past 100 (4)
    for vec in stricter:
        with pytest.raises(errors.ProtocolError):
            deserialize(bytes.fromhex(vec["hex"]))


@pytest.mark.parametrize("vec", _of("encode_refuse"), ids=lambda v: v["name"])
def test_encode_refuse_vectors_write_nothing(vec: Vector) -> None:
    document: dict[str, Any] = {}
    for key, value in vec["document"]["$doc"]:
        document[key] = bsonvec.to_python(value)
    with pytest.raises(errors.InvalidArgumentError) as info:
        serialize(document)
    assert vec["refusal"] == "InvalidArgumentError"
    assert "NUL" in info.value.message


@pytest.mark.parametrize("vec", _of("datetime_floor"), ids=lambda v: v["name"])
def test_datetime_floor_vectors(vec: Vector) -> None:
    instant = datetime.fromisoformat(vec["instant"])
    assert serialize({"t": instant}).hex() == vec["hex"]
    (millis,) = struct.unpack_from("<q", bytes.fromhex(vec["hex"]), 7)
    assert millis == int(vec["expected_millis"])


@pytest.mark.parametrize("vec", _of("size"), ids=lambda v: v["name"])
def test_size_vectors(vec: Vector) -> None:
    recipe = vec["recipe"]
    data = serialize({recipe["key"]: recipe["fill"] * recipe["fill_bytes"]})
    assert len(data) == vec["document_bytes"]
    assert hashlib.sha256(data).hexdigest() == vec["sha256"]
    assert (len(data) + 17 <= 32 * 1024 * 1024) is vec["fits_one_frame"]


def test_a_document_at_the_default_limit_reads_back() -> None:
    vec = next(v for v in _of("size") if v["name"] == "size/document-24-mib")
    recipe = vec["recipe"]
    data = serialize({recipe["key"]: recipe["fill"] * recipe["fill_bytes"]})
    assert deserialize(data) == {recipe["key"]: recipe["fill"] * recipe["fill_bytes"]}
