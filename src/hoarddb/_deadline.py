# SPDX-License-Identifier: Apache-2.0
"""One deadline per call (spec 8.4): it covers pool wait, dial, backoff and retries.

The clock is the monotonic one, read through a function so a test can drive it. A deadline of
``None`` seconds never expires.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Final

Clock = Callable[[], float]
monotonic: Final[Clock] = time.monotonic


class Deadline:
    __slots__ = ("_clock", "_end", "seconds")

    def __init__(self, seconds: float | None, clock: Clock | None = None) -> None:
        self._clock: Clock = clock or monotonic
        self.seconds = seconds
        self._end = None if seconds is None else self._clock() + seconds

    def remaining(self) -> float | None:
        """Seconds left (never negative), or None when unbounded."""
        if self._end is None:
            return None
        return max(0.0, self._end - self._clock())

    def expired(self) -> bool:
        return self._end is not None and self._clock() >= self._end

    def cap(self, seconds: float) -> float:
        """``seconds``, shortened to what is left."""
        left = self.remaining()
        return seconds if left is None else min(seconds, left)

    def within(self, seconds: float) -> Deadline:
        """A deadline that ends at this one or ``seconds`` from now, whichever is sooner (the
        dial's own limit inside the call's)."""
        return Deadline(self.cap(seconds), self._clock)
