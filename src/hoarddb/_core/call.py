# SPDX-License-Identifier: Apache-2.0
"""``Call``, ``Reply`` and ``Op``: how an operation talks to a runner (spec 3.3).

An operation is a plain function returning a generator. It yields :class:`Call` objects and
receives :class:`Reply` objects; it never touches a socket, a thread, a clock or an event loop.
The sync and the asyncio runner drive the same generator, so the two surfaces cannot drift.
"""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

from hoarddb._core.kinds import Kind
from hoarddb._core.messages import Request
from hoarddb._core.validate import check_timeout

Lane = Literal["point", "bulk"]

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Call:
    """One round trip. ``kind`` is the column the retry table reads; ``lane`` says which half of
    the pool carries it; ``timeout`` (seconds) overrides ``request_timeout`` and bounds the call
    including its retries."""

    op: str
    target: str | None
    request: Request
    lane: Lane
    kind: Kind
    timeout: float | None = None

    def __post_init__(self) -> None:
        check_timeout(self.timeout, operation=self.op)  # every call is built here, sent nowhere yet


@dataclass(frozen=True, slots=True)
class Reply:
    """The success response of a call (typed), and the request ID it answered."""

    response: Any
    request_id: bytes


Op = Generator[Call, Reply, T]
