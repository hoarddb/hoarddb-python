# SPDX-License-Identifier: Apache-2.0
"""``realkit.logins_served`` counts the driver's logins and leaves out the harness's own, which the
server logs exactly like any other (a stand-in node supplies the log)."""

from __future__ import annotations

from typing import Any

import pytest

import realkit


class StandInNode:
    driver_port = 1

    def __init__(self, lines: list[dict[str, Any]]) -> None:
        self._lines = lines

    def log_lines(self) -> list[dict[str, Any]]:
        return self._lines


def login(addr: str) -> dict[str, Any]:
    return {"msg": "client authenticated", "addr": addr, "auth_method": "password"}


@pytest.fixture(autouse=True)
def _clean_sources() -> Any:
    before = set(realkit.HARNESS_SOURCES)
    yield
    realkit.HARNESS_SOURCES.clear()
    realkit.HARNESS_SOURCES.update(before)


def test_a_login_from_the_harness_is_not_counted() -> None:
    realkit.HARNESS_SOURCES.add("127.0.0.1:50001")
    node = StandInNode(
        [login("127.0.0.1:50001"), login("127.0.0.1:50002"), login("127.0.0.1:50003")]
    )
    assert realkit.logins_served(node) == 2  # type: ignore[arg-type]


def test_only_logins_are_counted() -> None:
    node = StandInNode([login("127.0.0.1:1"), {"msg": "something else", "addr": "127.0.0.1:2"}])
    assert realkit.logins_served(node) == 1  # type: ignore[arg-type]


def test_a_node_health_poll_registers_its_own_source(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeSocket:
        def getsockname(self) -> tuple[str, int]:
            return ("127.0.0.1", 40404)

    class FakeConn:
        sock = FakeSocket()

        def __enter__(self) -> FakeConn:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    class Answer:
        opcode = 0

    def connect(port: int) -> FakeConn:
        return FakeConn()

    def authenticated(conn: FakeConn) -> Answer:
        return Answer()

    monkeypatch.setattr(realkit, "RawConn", connect)
    monkeypatch.setattr(realkit, "authenticate", authenticated)
    realkit.node_health(StandInNode([]))  # type: ignore[arg-type]
    assert "127.0.0.1:40404" in realkit.HARNESS_SOURCES
