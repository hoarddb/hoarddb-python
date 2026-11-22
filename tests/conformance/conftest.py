# SPDX-License-Identifier: Apache-2.0
"""Fixtures for the conformance legs (spec 11.1, 11.2): a real server, never a fake one.

A real-server test fails loudly when ``HOARDDB_SERVER_BIN`` is unset; the no-server run is
``pytest -m "not server"``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import pytest

import faketls
import realkit
import serverkit

_STARTUPS: list[tuple[str, float]] = []


@pytest.fixture(scope="session")
def server_bin() -> Path:
    try:
        return serverkit.server_binary()
    except serverkit.ServerUnavailableError as problem:
        pytest.fail(str(problem), pytrace=False)


@pytest.fixture(scope="session")
def tap_identity(tmp_path_factory: pytest.TempPathFactory) -> faketls.Identity:
    """The proxy's own key: drivers under test are pinned to it, not to the server."""
    return faketls.make_identity(tmp_path_factory.mktemp("tap"), "tap")


@pytest.fixture(scope="session")
def servers(server_bin: Path, tap_identity: faketls.Identity) -> Iterator[realkit.Servers]:
    pool = realkit.Servers(server_bin, tap_identity)
    try:
        yield pool
    finally:
        _STARTUPS.extend(pool.startup_seconds)
        pool.close()


@pytest.fixture(scope="session")
def cluster(server_bin: Path) -> Iterator[serverkit.Cluster]:
    """Three members and a newcomer that no member has added, shared by the read-only tests."""
    began = time.monotonic()
    group, confirmed = serverkit.start_cluster(server_bin, realkit.wait_until_confirmed)
    try:
        _STARTUPS.append(("three nodes started, membership confirmed", time.monotonic() - began))
        _STARTUPS.append(("  of which waiting for membership", confirmed))
        yield group
    finally:
        group.cleanup()


@pytest.fixture
def own_cluster(server_bin: Path) -> Iterator[Callable[..., serverkit.Cluster]]:
    """A cluster for one test, for tests that stop nodes; removed when the test ends."""
    made: list[serverkit.Cluster] = []

    def make(newcomer: bool = False) -> serverkit.Cluster:
        group, _ = serverkit.start_cluster(
            server_bin, realkit.wait_until_confirmed, newcomer=newcomer
        )
        made.append(group)
        return group

    try:
        yield make
    finally:
        for group in made:
            group.cleanup()


@pytest.fixture
def own(servers: realkit.Servers) -> Iterator[Callable[..., serverkit.Node]]:
    """Start servers that belong to one test (``own(env)``); each is stopped when the test ends."""
    made: list[serverkit.Node] = []

    def make(extra_env: Mapping[str, str] | None = None, name: str = "own") -> serverkit.Node:
        node = servers.dedicated(extra_env, name=name)
        made.append(node)
        return node

    try:
        yield make
    finally:
        for node in made:
            servers.release(node)


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    if realkit.RESULT_LINES:
        terminalreporter.section("conformance results")
        for line in realkit.RESULT_LINES:
            terminalreporter.write_line(line)
    if _STARTUPS:
        terminalreporter.section("real-server start-up times")
        for label, seconds in _STARTUPS:
            terminalreporter.write_line(f"{seconds:6.3f} s  {label}")
