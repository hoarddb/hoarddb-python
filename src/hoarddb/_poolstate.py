# SPDX-License-Identifier: Apache-2.0
"""The bookkeeping the blocking and the asyncio pool share: one claim per held slot, one lane's
idle connections and holders, and what the server said at ``AUTH``. Neither type does I/O."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Final, Generic, TypeVar

from hoarddb._core.call import Lane

C = TypeVar("C")

STALE_AFTER: Final = 60.0  # an idle connection this old gets one PING before it is used
PING_DEADLINE: Final = 10.0
CLOSE_WAIT: Final = 5.0


class Claim(Generic[C]):
    """One slot held by one checkout. Hashable by identity; ``conn`` is the connection the claim
    has taken or dialled, kept here so a failed checkout can close it. The claim is added to the
    lane's holder set in a single operation, so an asynchronous exception (a ``KeyboardInterrupt``
    lands between any two bytecodes, a cancellation at any ``await``) cannot leak a slot."""

    __slots__ = ("conn",)

    def __init__(self) -> None:
        self.conn: C | None = None


class LaneState(Generic[C]):
    __slots__ = ("failures", "holders", "idle", "name", "not_before", "size", "waiting")

    def __init__(self, name: Lane, size: int) -> None:
        self.name: Lane = name
        self.size = size
        self.idle: deque[C] = deque()
        self.holders: set[Claim[C]] = set()
        self.waiting = 0
        self.failures = 0  # consecutive failed dials on this lane
        self.not_before = 0.0  # monotonic time before which no new dial starts


@dataclass(frozen=True, slots=True)
class ServerFacts:
    version: str
    capabilities: tuple[str, ...]
    password_min_length: int
