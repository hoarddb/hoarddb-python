# SPDX-License-Identifier: Apache-2.0
"""Leg 1 (codec) needs no server: the frame, ``exchange`` (request encode, response decode) and
``decode`` vectors are replayed by ``tests/test_frame.py``, ``tests/test_messages.py`` and
``tests/test_failure.py``. This module pins what the real-server legs rely on: the vector file and
the fetched server binary come from the same server commit (testing.md section 3)."""

from __future__ import annotations

import json
from collections import Counter

from vectors import ROOT, driver_vectors


def test_the_vectors_and_the_pinned_server_come_from_the_same_commit() -> None:
    pinned = json.loads((ROOT / "testing" / "server.json").read_text(encoding="utf-8"))
    vectors = json.loads(
        (ROOT / "testdata" / "driver-vectors" / "v1.json").read_text(encoding="utf-8")
    )
    assert vectors["server_commit"] == pinned["server_commit"]
    assert pinned["tag"] == f"test-server-{pinned['server_commit'][:12]}"


def test_the_vector_file_has_the_shape_the_legs_were_written_for() -> None:
    kinds = Counter(v["kind"] for v in driver_vectors())
    assert kinds["frame"] == 7 and kinds["trust"] == 9 and kinds["decode"] == 14
    exchanges = driver_vectors(kind="exchange")
    replayable = [v for v in exchanges if v["replay"]]
    assert len(replayable) == 182  # 172 single node and 10 on three nodes
    assert Counter(v["topology"] for v in replayable) == {"single": 172, "cluster3": 10}
