# SPDX-License-Identifier: Apache-2.0
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false
"""Building blocks for the conformance legs: servers by group, clients through a tap, raw senders.

``Servers`` starts one server per distinct (topology, environment) group and one client per
(server, surface); ``Rig`` is a public client of either surface wired through a recording tap, so a
test sees both what the driver sent and what the server answered.
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import faketls
import hoarddb
import replay_api
import serverkit
from clientkit import PC
from hoarddb._bson import deserialize, serialize
from hoarddb._core import frame, opcodes
from serverkit import Cluster, Node
from tapkit import RawConn, Tap, password_auth_payload
from vectors import DRIVER_VECTORS

RESULT_LINES: list[str] = []  # one line per measured leg, printed in the run summary

AUTH_OPCODE = 0x01
ERROR_OPCODE = 0xA2


def vectors_server_env() -> dict[str, str]:
    """The environment every replaying server starts with, on top of the handbook's."""
    data = json.loads(DRIVER_VECTORS.read_text(encoding="utf-8"))
    env: dict[str, str] = dict(data["server_env"])
    return env


class Rig:
    """A public client of one surface, dialled through a tap onto one node."""

    def __init__(
        self,
        mode: str,
        node: Node,
        identity: faketls.Identity,
        *,
        user: str = serverkit.ROOT_USER,
        password: str = serverkit.ROOT_PASSWORD,
        **options: Any,
    ) -> None:
        self.mode = mode
        self.node = node
        self.tap = Tap(identity, node.driver_port)
        self.tap.__enter__()
        try:
            uri = f"hoarddb://{user}:{password}@127.0.0.1:{self.tap.port}"
            options.setdefault("request_timeout", 30.0)
            self.pc = PC(mode, self.tap, identity, uri=uri, **options)
        except BaseException:
            self.tap.__exit__(None, None, None)
            raise

    def close(self) -> None:
        try:
            self.pc.close()
        finally:
            self.tap.__exit__(None, None, None)

    def call(self, fn: Any) -> Any:
        return self.pc.call(fn)

    def raw(self, opcode: int, name: str, document: Mapping[str, Any] | None) -> Any:
        """Any opcode with the given document, through the client's own pool (private sender)."""
        fake = {"request": {"opcode": opcode, "opcode_name": name}}
        return self.pc.call(lambda c: replay_api.send_raw(c, fake, dict(document or {})))

    def drop_every_database(self) -> int:
        """Leave the server empty, so the next vector sees what a fresh server would show."""
        names = [d.name for d in self.pc.call(lambda c: c.list_databases())]
        for name in names:

            def drop(client: Any, doomed: str = name) -> Any:
                return client.drop_database(doomed)

            self.pc.call(drop)
        return len(names)


class Direct:
    """A port to dial: the node itself, with no tap in between (large payloads, raw timing)."""

    def __init__(self, node: Node) -> None:
        self.port = node.driver_port


def connect_direct(mode: str, node: Node, identity: faketls.Identity, **options: Any) -> PC:
    """A public client pinned to the node's own key, dialling it straight."""
    uri = options.pop("uri", node.uri())
    options.setdefault("tls", hoarddb.TlsOptions(fingerprints=[node.fingerprint]))
    options.setdefault("request_timeout", 60.0)
    return PC(mode, Direct(node), identity, uri=uri, **options)


class Servers:
    """Servers by group, clients by (server, surface); everything is closed in ``close()``."""

    def __init__(self, binary: Path, identity: faketls.Identity) -> None:
        self.binary = binary
        self.identity = identity
        self._nodes: dict[tuple[tuple[str, str], ...], Node] = {}
        self._rigs: dict[tuple[int, str], Rig] = {}
        self._own: list[Node] = []
        self.startup_seconds: list[tuple[str, float]] = []

    def node(self, extra_env: Mapping[str, str] | None = None) -> Node:
        env = {**vectors_server_env(), **(extra_env or {})}
        key = tuple(sorted(env.items()))
        existing = self._nodes.get(key)
        if existing is not None:
            return existing
        node = Node(self.binary, env, name=f"group{len(self._nodes)}")
        self._nodes[key] = node
        node.start()
        label = ",".join(f"{k}={v}" for k, v in (extra_env or {}).items()) or "base"
        self.startup_seconds.append((label, node.startup_seconds))
        return node

    def dedicated(self, extra_env: Mapping[str, str] | None = None, *, name: str = "own") -> Node:
        """A server of its own for one test (failed logins count against a limiter, a restart or a
        cap would disturb the others); closed with the pool, or earlier with ``release``."""
        node = Node(self.binary, {**vectors_server_env(), **(extra_env or {})}, name=name)
        self._own.append(node)
        node.start()
        self.startup_seconds.append((f"{name} (dedicated)", node.startup_seconds))
        return node

    def forget(self, rig: Rig) -> None:
        """Close one client now (its server is about to go away)."""
        for key, held in list(self._rigs.items()):
            if held is rig:
                del self._rigs[key]
        with contextlib.suppress(Exception):
            rig.close()

    def release(self, node: Node) -> None:
        """Stop a dedicated server now, with the clients that dialled it."""
        for key, rig in list(self._rigs.items()):
            if rig.node is node:
                with contextlib.suppress(Exception):
                    rig.close()
                del self._rigs[key]
        node.cleanup()
        if node in self._own:
            self._own.remove(node)

    def rig(self, mode: str, node: Node, **options: Any) -> Rig:
        """The client of ``mode`` for ``node`` (made on first use, with ``options``)."""
        key = (node.driver_port, mode)
        rig = self._rigs.get(key)
        if rig is None:
            rig = Rig(mode, node, self.identity, **options)
            self._rigs[key] = rig
        return rig

    def close(self) -> None:
        try:
            for rig in list(self._rigs.values()):
                with contextlib.suppress(Exception):  # teardown must reach the kill below
                    rig.close()
        finally:
            for node in [*self._nodes.values(), *self._own]:
                node.cleanup()
            self._own.clear()
            self._rigs.clear()
            self._nodes.clear()


def authenticate(
    conn: RawConn, user: str = serverkit.ROOT_USER, password: str = serverkit.ROOT_PASSWORD
) -> frame.Frame:
    """Password ``AUTH`` on a bare connection; returns the answer (``AUTH_OK`` or ``AUTH_FAIL``)."""
    conn.send(AUTH_OPCODE, frame.new_request_id(), serialize(password_auth_payload(user, password)))
    return conn.read_frame()


# The source address ("host:port") of every login the harness made itself. The server logs each
# login with the address it came from, so a test counting the driver's logins can leave these out.
HARNESS_SOURCES: set[str] = set()


def logins_served(node: Node) -> int:
    """Logins the node has accepted that did not come from the harness (a readiness or health poll):
    the driver's own, by password or by token."""
    return sum(
        1
        for entry in node.log_lines()
        if entry.get("msg") == "client authenticated" and entry.get("addr") not in HARNESS_SOURCES
    )


def node_health(node: Node) -> dict[str, Any]:
    """``HEALTH_CHECK`` on a bare connection (no driver in the way of a readiness check)."""
    with RawConn(node.driver_port) as conn:
        host, port = conn.sock.getsockname()[:2]
        HARNESS_SOURCES.add(f"{host}:{port}")
        if authenticate(conn).opcode != opcodes.AUTH_OK:
            return {}
        conn.send(opcodes.HEALTH_CHECK, frame.new_request_id(), serialize({}))
        return decode(conn.read_frame().payload)


def wait_until_confirmed(group: Cluster, timeout: float = 60.0) -> float:
    """Ready when every member's health says ``membership: confirmed``; bounded, and a timeout
    names each member's state and the end of its log. Returns the seconds it took."""
    began = time.monotonic()
    states: dict[str, str] = {}
    while True:
        group.check_alive()
        states = {}
        for node in group.members:
            try:
                states[node.name] = str(node_health(node).get("membership", ""))
            except (OSError, EOFError):
                states[node.name] = "not answering"
        if all(state == "confirmed" for state in states.values()):
            return time.monotonic() - began
        if time.monotonic() - began > timeout:
            tails = "\n".join(f"--- {n.name}\n{n.log_tail(8)}" for n in group.nodes)
            raise AssertionError(
                f"no confirmed membership after {timeout:.0f} s: {states}\n{tails}"
            )
        time.sleep(0.2)  # polling a health answer; bounded by the deadline above


def decode(payload: bytes) -> dict[str, Any]:
    return deserialize(payload) if payload else {}


def wait_for(predicate: Any, what: str, *, timeout: float = 10.0) -> None:
    """Poll with a deadline, never a bare sleep."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout:.0f} s waiting for {what}")
        time.sleep(0.02)
