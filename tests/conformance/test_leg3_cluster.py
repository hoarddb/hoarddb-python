# SPDX-License-Identifier: Apache-2.0
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false, reportUnknownLambdaType=false
"""Leg 3 on three nodes: every ``topology: cluster3`` vector with ``replay: true``, sent to
``node1`` or, for the four whose notes say so, to the ``newcomer`` that no member has added.

The vectors were captured on nodes with fixed keys and fixed loopback ports, normalised to
``127.0.0.1:4433`` and so on; this run's nodes have their own, so the harness maps the vector's
ports and fingerprints to the running cluster's (``rewrite``) and compares everything else as in
leg 3 on one node. The vectors a stopped or restarted node needs (``replay: false``) are leg 9.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from realkit import Servers
from replaykit import replay_on_rig
from serverkit import Cluster
from vectors import driver_vectors

pytestmark = pytest.mark.server

CLUSTER = [
    v for v in driver_vectors(kind="exchange") if v["topology"] == "cluster3" and v["replay"]
]
# the vectors whose notes put them on the fourth node
NEWCOMER = {
    "health of a node not yet added",
    "get at a node not yet added",
    "put at a node not yet added",
}
# captured before the fourth node was started: its answer lists three nodes, so it runs on a
# cluster that has no newcomer (the other nodes list a newcomer they have heard of, as the
# history vector's answer shows)
BEFORE_THE_NEWCOMER = {"list nodes"}
VECTOR_DRIVER_PORTS = {"7433": 0, "7434": 1, "7435": 2, "7436": 3}
VECTOR_RING_PORTS = {"4433": 0, "4435": 1, "4437": 2, "4439": 3}


def rewrite_for(group: Cluster) -> Any:
    """Map the vectors' fixed ports and keys to this cluster's."""
    nodes = group.nodes
    ports: dict[str, str] = {}
    for old, index in VECTOR_DRIVER_PORTS.items():
        if index < len(nodes):
            ports[old] = str(nodes[index].driver_port)
    for old, index in VECTOR_RING_PORTS.items():
        if index < len(nodes):
            ports[old] = str(nodes[index].ring_port)
    keys: dict[str, str] = {}
    for vec in driver_vectors(kind="exchange"):
        if vec["request"]["opcode_name"] != "LIST_NODES":
            continue
        for member in vec["response"]["payload_json"].get("nodes", []):
            if member.get("fingerprint"):
                keys[member["fingerprint"]] = group.fingerprints[member["nodeid"]]

    def rewrite(text: str) -> str:
        for old, new in keys.items():
            text = text.replace(old, new)
        return re.sub(
            r"127\.0\.0\.1:(\d+)", lambda m: f"127.0.0.1:{ports.get(m.group(1), m.group(1))}", text
        )

    return rewrite


@pytest.mark.parametrize("vec", CLUSTER, ids=lambda v: v["name"])
def test_the_vector_replays_against_three_nodes(
    vec: dict[str, Any], mode: str, servers: Servers, request: pytest.FixtureRequest
) -> None:
    if vec["name"] in BEFORE_THE_NEWCOMER:
        group: Cluster = request.getfixturevalue("own_cluster")(newcomer=False)
    else:
        group = request.getfixturevalue("cluster")
    node = group.newcomer if vec["name"] in NEWCOMER else group.members[0]
    assert node is not None
    # the newcomer answers UNAVAILABLE for ever and the driver retries until its deadline: keep
    # that deadline short
    rig = servers.rig(mode, node, request_timeout=3.0 if node is group.newcomer else 30.0)
    try:
        replay_on_rig(vec, mode, rig, rewrite_for(group))
    finally:
        if node is not group.newcomer:  # a node that has not been added answers no list at all
            rig.drop_every_database()
        if vec["name"] in BEFORE_THE_NEWCOMER:
            servers.forget(rig)


def test_there_are_ten_replayable_cluster_vectors_and_the_newcomer_ones_are_among_them() -> None:
    assert len(CLUSTER) == 10
    assert {v["name"] for v in CLUSTER} >= NEWCOMER
