# SPDX-License-Identifier: Apache-2.0
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false, reportUnknownLambdaType=false
"""Leg 9 on three nodes: what the write outcome, the write concern and the membership look like to
a driver (``errors.md`` section 6, ``operations.md``). Tests that stop a node run on a cluster of
their own; the others share the session's cluster, which they leave as they found it."""

from __future__ import annotations

import time
from typing import Any

import pytest

import faketls
import hoarddb
from clientkit import PC
from hoarddb import errors
from hoarddb._core import opcodes
from realkit import (
    RESULT_LINES,
    Rig,
    Servers,
    connect_direct,
    logins_served,
    node_health,
    wait_for,
)
from serverkit import ROOT_PASSWORD, ROOT_USER, Cluster, Node

pytestmark = pytest.mark.server


def prepare(rig: Rig, database: str) -> None:
    rig.call(lambda c: c.create_database(database))
    rig.call(lambda c: c.database(database).create_bucket("users", "hash"))


def write(rig: Rig, database: str, key: str, n: int, **options: Any) -> Any:
    return rig.call(lambda c: c.database(database).bucket("users").put(key, {"n": n}, **options))


def request_for(database: str) -> dict[str, Any]:
    return {"database": database, "bucket": "users", "key": "a", "data": {}}


def rows_written(rig: Rig, database: str) -> int:
    return int(rig.call(lambda c: c.database(database).bucket("users").count()))


def test_an_all_write_with_one_node_stopped_succeeds_with_fewer_copies(
    mode: str, own_cluster: Any, servers: Servers
) -> None:
    group: Cluster = own_cluster()
    first, _, third = group.members
    rig = servers.rig(mode, first)
    try:
        prepare(rig, "stopped")
        whole = rig.call(
            lambda c: (
                c.database("stopped").bucket("users").put("ada", {"n": 1}, write_concern="all")
            )
        )
        assert (whole.copies_confirmed, whole.replication_factor) == (3, 3)
        third.stop()  # SIGTERM; the same data directory and variables bring it back below
        wait_for(lambda: not third.running, "the process to exit")
        down = rig.call(
            lambda c: (
                c.database("stopped").bucket("users").put("bob", {"n": 2}, write_concern="all")
            )
        )
        assert down.replication_factor == 3
        assert down.copies_confirmed is not None and down.copies_confirmed < 3, (
            "success, with the copies that were confirmed below the replication factor"
        )
        RESULT_LINES.append(
            f"leg 9 an `all` write with node3 stopped: copies_confirmed={down.copies_confirmed} "
            f"replication_factor={down.replication_factor} ({rig.mode})"
        )
        third.start()  # back, with the same directory and the same variables
        assert third.fingerprint == group.fingerprints["node3"], "the same key came back"
        wait_for(
            lambda: node_health(third).get("membership") == "confirmed",
            "node3 to rejoin",
            timeout=60,
        )
        # "confirmed" is the membership; the stream that replays what node3 missed catches up a
        # moment later, and an `all` write sent in that gap is answered with two copies. So write
        # until three are confirmed, with a deadline, and assert on the last answer.
        latest: list[Any] = []

        def three_copies() -> bool:
            latest.append(write(rig, "stopped", "cy", 3, write_concern="all"))
            return bool(latest[-1].copies_confirmed == 3)

        wait_for(three_copies, "an `all` write to confirm three copies again", timeout=20)
        assert latest[-1].copies_confirmed == 3, "all three copies are confirmed again"
    finally:
        rig.drop_every_database()
        servers.forget(rig)


def test_an_unknown_write_concern_word_is_refused(
    mode: str, servers: Servers, cluster: Cluster
) -> None:
    rig = servers.rig(mode, cluster.members[0])
    try:
        prepare(rig, "wordy")
        rig.tap.clear()
        with pytest.raises(errors.InvalidArgumentError) as local:
            rig.call(
                lambda c: (
                    c.database("wordy").bucket("users").put("a", {"n": 1}, write_concern="quorum")
                )
            )
        assert "quorum" in local.value.message
        assert rig.tap.requests() == [], "the driver refuses the word before sending anything"
        # the server's own refusal of the same word, through the private raw sender
        with pytest.raises(errors.InvalidArgumentError) as refused:
            rig.raw(
                opcodes.PUT,
                "PUT",
                {
                    "database": "wordy",
                    "bucket": "users",
                    "key": "a",
                    "data": {},
                    "writeconcern": "quorum",
                },
            )
        assert refused.value.wire_code == 4 and refused.value.write_outcome == "not_applied"
        assert 'unknown write concern: "quorum"' in refused.value.message
        assert rows_written(rig, "wordy") == 0
    finally:
        rig.drop_every_database()


def test_a_write_at_a_node_not_yet_added_is_unavailable_and_not_applied(
    mode: str, servers: Servers, cluster: Cluster
) -> None:
    assert cluster.newcomer is not None
    rig = servers.rig(mode, cluster.newcomer, request_timeout=3.0)
    began = time.monotonic()
    with pytest.raises(errors.UnavailableError) as refused:
        rig.call(lambda c: c.database("anywhere").bucket("users").put("a", {"n": 1}))
    assert refused.value.wire_code == 6 and refused.value.write_outcome == "not_applied"
    assert "has not been added to the cluster yet" in refused.value.message
    assert "Nothing was read or written" in refused.value.message
    assert time.monotonic() - began < 6.0, (
        "retried with backoff, inside the deadline, then given up"
    )


def test_a_consistent_get_reads_back_an_acknowledged_write_from_every_node(
    mode: str, servers: Servers, cluster: Cluster
) -> None:
    writer = servers.rig(mode, cluster.members[0])
    try:
        prepare(writer, "consist")
        written = writer.call(
            lambda c: (
                c.database("consist").bucket("users").put("ada", {"n": 7}, write_concern="all")
            )
        )
        assert written.copies_confirmed == 3
        for member in cluster.members:
            reader = servers.rig(mode, member)
            reader.tap.clear()
            found = reader.call(
                lambda c: c.database("consist").bucket("users").get("ada", consistent=True)
            )
            assert found is not None and found["n"] == 7, f"read back at {member.name}"
            asked = [f.opcode for f in reader.tap.requests()]
            assert asked == [opcodes.CONSISTENT_GET], "the quorum read, not the plain one"
        # and after an overwrite, the newest value, never the old one
        writer.call(
            lambda c: (
                c.database("consist").bucket("users").put("ada", {"n": 8}, write_concern="all")
            )
        )
        latest = servers.rig(mode, cluster.members[2]).call(
            lambda c: c.database("consist").bucket("users").get("ada", consistent=True)
        )
        assert latest is not None and latest["n"] == 8
    finally:
        writer.drop_every_database()


def test_a_node_that_leaves_does_not_break_the_pool(
    mode: str, own_cluster: Any, tap_identity: faketls.Identity
) -> None:
    group: Cluster = own_cluster()
    one, two, three = group.members
    order = (two, one, three)  # node2 is tried first
    hosts = ",".join(f"127.0.0.1:{n.driver_port}" for n in order)
    pins = [group.fingerprints[n.name] for n in order]

    def answered_by(node: Node) -> int:
        """Logins this node has accepted so far from the driver, by password or by token: the
        harness's own health polls are logins too, and are left out by their source address."""
        return logins_served(node)

    seen = {node.name: answered_by(node) for node in group.members}
    pc: PC = connect_direct(
        mode,
        two,
        tap_identity,
        uri=f"hoarddb://{ROOT_USER}:{ROOT_PASSWORD}@{hosts}",
        tls=hoarddb.TlsOptions(fingerprints=pins),
        request_timeout=20.0,
    )
    try:
        # The harness polls a node's health while a test runs, as wait_until_confirmed does while a
        # cluster comes up; each poll is a login the server logs, and none of them is the driver's.
        for member in group.members:
            node_health(member)
        pc.call(lambda c: c.create_database("leaver"))
        pc.call(lambda c: c.database("leaver").create_bucket("users", "hash"))
        pc.call(lambda c: c.database("leaver").bucket("users").put("before", {"n": 1}))
        assert answered_by(two) == seen["node2"] + 1, "the first host served the pool's connection"
        assert answered_by(one) == seen["node1"] and answered_by(three) == seen["node3"]
        two.stop()  # a member leaves: its pooled connection is closed under the pool
        wait_for(lambda: not two.running, "node2 to exit")
        assert pc.call(lambda c: c.ping()) is None
        written = pc.call(lambda c: c.database("leaver").bucket("users").put("after", {"n": 2}))
        assert written.id == "after", "a write after the member left lands on another member"
        moved = answered_by(one) + answered_by(three) - seen["node1"] - seen["node3"]
        assert moved >= 1, "the pool moved to another member"
        stats = pc.call(lambda c: c.pool_stats())
        assert not stats.closed and stats.discarded >= 1
        assert pc.call(lambda c: c.database("leaver").bucket("users").get("before")) is not None
        assert pc.call(lambda c: c.database("leaver").bucket("users").get("after")) is not None
    finally:
        pc.close()


def test_the_result_of_a_stopped_node_is_visible_in_list_nodes_and_health(
    mode: str, own_cluster: Any, servers: Servers
) -> None:
    group: Cluster = own_cluster()
    rig = servers.rig(mode, group.members[0])
    try:
        listed = rig.call(lambda c: c.list_nodes())
        assert sorted(n.node_id for n in listed) == ["node1", "node2", "node3"]
        health = rig.call(lambda c: c.health())
        assert health.node_id == "node1" and health.membership == "confirmed" and health.healthy
    finally:
        servers.forget(rig)
