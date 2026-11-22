# SPDX-License-Identifier: Apache-2.0
"""Text every write's docstring carries (spec 8.4): the one place an async caller loses an outcome
without knowing it. The blocking twin carries the same words, because the two docstrings must be
equal."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])

SHIELD = """

    A write that is cancelled after it was sent has an unknown outcome (the server cannot be told
    to stop, so the connection is closed). Under asyncio, shield a write whose outcome you need::

        write = asyncio.ensure_future(users.put("ada", doc))
        try:
            result = await asyncio.shield(write)
        except asyncio.CancelledError:
            result = await write        # the write was not cancelled; learn its outcome first
            raise                       # then let the cancellation continue

    A timeout on a sent write is also an unknown outcome: read it back before resending, and do
    not resend a push, a pop or an increment. The driver never retries a write."""


def writes(fn: F) -> F:
    """Append the shield note to a write's docstring."""
    fn.__doc__ = (fn.__doc__ or "").rstrip() + SHIELD
    return fn
