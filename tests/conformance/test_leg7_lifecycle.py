# SPDX-License-Identifier: Apache-2.0
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false, reportUnknownLambdaType=false
"""Leg 7: a server that dies, goes silent or closes an idle connection (``wire.md`` section 9).

Each test has a server of its own: it is killed, restarted or timed out on purpose. The tap can
swallow the server's answers (``blackhole``), which makes a request that the server really received
look unanswered, so "the server died mid-request" does not depend on a race.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

import faketls
from hoarddb import errors
from hoarddb._core import opcodes
from realkit import Rig, wait_for

pytestmark = pytest.mark.server

TIMEOUT = 5.0


def prepared(mode: str, node: Any, identity: faketls.Identity, **options: Any) -> Rig:
    rig = Rig(mode, node, identity, request_timeout=TIMEOUT, **options)
    rig.call(lambda c: c.create_database("life"))
    rig.call(lambda c: c.database("life").create_bucket("docs", "hash"))
    rig.call(lambda c: c.database("life").bucket("docs").put("before", {"n": 1}))
    return rig


def put_after(rig: Rig, key: str) -> Any:
    return rig.call(lambda c: c.database("life").bucket("docs").put(key, {"n": 2}))


def test_a_server_killed_mid_request_is_a_connection_failure_not_a_hang_and_recovers(
    mode: str, own: Any, tap_identity: faketls.Identity
) -> None:
    node = own()
    rig = prepared(mode, node, tap_identity)
    pinned = node.fingerprint
    try:
        rig.tap.clear()
        rig.tap.blackhole.set()  # the server gets the write, the driver never sees the answer

        def kill_once_the_write_arrived() -> None:
            wait_for(lambda: any(f.opcode == opcodes.PUT for f in rig.tap.requests()), "the PUT")
            node.kill()

        killer = threading.Thread(target=kill_once_the_write_arrived)
        killer.start()
        began = time.monotonic()
        with pytest.raises(errors.ConnectionFailedError) as lost:
            put_after(rig, "during")
        elapsed = time.monotonic() - began
        killer.join(timeout=10)
        assert elapsed < TIMEOUT, f"took {elapsed:.1f} s: it must fail inside the request timeout"
        assert lost.value.write_outcome == "unknown", "a write that was sent has an unknown outcome"
        # the next call after a restart succeeds on a new connection, with the same client
        rig.tap.blackhole.clear()
        node.start()
        assert node.fingerprint == pinned, "a restart keeps the node's key"
        assert rig.call(lambda c: c.ping()) is None
        assert rig.call(lambda c: c.database("life").bucket("docs").get("before")) is not None
        assert put_after(rig, "after").id == "after"
    finally:
        rig.close()


def test_a_silent_server_is_a_request_timeout_within_the_deadline(
    mode: str, own: Any, tap_identity: faketls.Identity
) -> None:
    node = own()
    rig = prepared(mode, node, tap_identity)
    try:
        rig.tap.blackhole.set()
        began = time.monotonic()
        with pytest.raises(errors.RequestTimeoutError) as silent:
            rig.call(
                lambda c: c.database("life").bucket("docs").put("silent", {"n": 3}, timeout=1.5)
            )
        elapsed = time.monotonic() - began
        assert 1.4 <= elapsed < 4.0, f"{elapsed:.2f} s for a 1.5 s timeout"
        assert silent.value.write_outcome == "unknown"
        rig.tap.blackhole.clear()
        assert rig.call(lambda c: c.ping()) is None, "the client recovers on a new connection"
    finally:
        rig.close()


def test_an_idle_connection_the_server_closed_is_replaced_at_checkout_not_used(
    mode: str, own: Any, tap_identity: faketls.Identity
) -> None:
    """A WRITE after the idle close: a write is never retried after a connection loss, so it
    succeeds only if the dead connection was found at checkout and never written to."""
    node = own({"HOARDDB_TCP_IDLE_TIMEOUT": "2"})
    rig = prepared(mode, node, tap_identity)
    try:
        before = rig.call(lambda c: c.pool_stats())
        assert before.dials == 1 and before.point_idle == 1
        wait_for(lambda: rig.tap.closed >= 1, "the server to close the idle connection", timeout=15)
        rig.tap.clear()
        written = put_after(rig, "after-idle")
        assert written.id == "after-idle", "the dead connection must not have been used"
        after = rig.call(lambda c: c.pool_stats())
        assert after.dials == 2 and after.discarded == 1 and after.dial_failures == 0
        sent = [f for f in rig.tap.frames("c2s") if f.opcode == opcodes.PUT]
        assert len(sent) == 1, "the write was sent once, on the new connection"
        assert rig.call(lambda c: c.database("life").bucket("docs").get("after-idle")) is not None
    finally:
        rig.close()
