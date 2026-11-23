# SPDX-License-Identifier: Apache-2.0
"""Leg 1 (codec) needs no server: the frame, ``exchange`` (request encode, response decode) and
``decode`` vectors are replayed by ``tests/test_frame.py``, ``tests/test_messages.py`` and
``tests/test_failure.py``. This module pins what the real-server legs rely on: the shape of the
vector file, and that it names the server commit it was recorded from (testing.md section 3)."""

from __future__ import annotations

import json
import re
from collections import Counter

from vectors import ROOT, driver_vectors


def test_the_vector_file_names_the_server_commit_it_was_recorded_from() -> None:
    """The vectors are the contract, so they say which server answered them. ``testing/server.json``
    names a download for the real-server legs; it is an earlier commit and no such download is
    published, so the two are not required to agree: build the server (testing/README.md)."""
    vectors = json.loads(
        (ROOT / "testdata" / "driver-vectors" / "v1.json").read_text(encoding="utf-8")
    )
    assert re.fullmatch(r"[0-9a-f]{40}", vectors["server_commit"])
    pinned = json.loads((ROOT / "testing" / "server.json").read_text(encoding="utf-8"))
    assert pinned["tag"] == f"test-server-{pinned['server_commit'][:12]}"


def test_the_vector_file_has_the_shape_the_legs_were_written_for() -> None:
    kinds = Counter(v["kind"] for v in driver_vectors())
    assert kinds["frame"] == 7 and kinds["trust"] == 9 and kinds["decode"] == 16
    exchanges = driver_vectors(kind="exchange")
    replayable = [v for v in exchanges if v["replay"]]
    assert len(replayable) == 277  # 267 single node and 10 on three nodes
    assert Counter(v["topology"] for v in replayable) == {"single": 267, "cluster3": 10}
