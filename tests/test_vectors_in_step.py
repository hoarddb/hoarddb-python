# SPDX-License-Identifier: Apache-2.0
"""The driver vectors under ``testdata/`` are a copy of the wire contract, never a fork of it.

The file is copied here so that this repository stands on its own. In the tree that also holds the
contract's source, one directory up, the copy must equal it byte for byte: a reply shape that
changes there and not here leaves every replay test pinned to an answer no server gives. Where the
source is not present (a checkout of this repository alone), there is nothing to compare with.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

import vectors

SOURCE = vectors.ROOT.parent / "testdata" / "driver-vectors" / "v1.json"
REFRESH = "cp ../testdata/driver-vectors/v1.json testdata/driver-vectors/v1.json"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.skipif(not SOURCE.exists(), reason="the vectors' source is not part of this checkout")
def test_the_driver_vectors_equal_their_source_byte_for_byte() -> None:
    assert digest(vectors.DRIVER_VECTORS) == digest(SOURCE), (
        f"{vectors.DRIVER_VECTORS} differs from {SOURCE}. Refresh the copy from the driver's "
        f"directory with `{REFRESH}`, then make every test that replays it pass: a test that "
        "turns red is an answer this driver reads differently from the server."
    )


def test_every_pop_vector_answers_a_list() -> None:
    """The shape a pop must be read as: ``items``, and no single-entry member."""
    pops = [
        v
        for v in vectors.driver_vectors(kind="exchange")
        if v["request"]["opcode_name"].endswith("_POP")
        and v["response"].get("payload_json", {}).get("found") is True
    ]
    assert len(pops) >= 10
    for vec in pops:
        answer: dict[str, Any] = vec["response"]["payload_json"]
        assert isinstance(answer.get("items"), list) and answer["items"], vec["name"]
        assert "data" not in answer and "element" not in answer, vec["name"]
