# SPDX-License-Identifier: Apache-2.0
"""The retry decision table (errors.md section 6, spec 8.1) as one pure function.

Nothing here sleeps, reads a clock or draws a random number: the runner passes in the time left and
a jitter in [0, 1), and does what the returned :class:`Decision` says. A write is never retried
unless the server said ``not_applied``; a failed dial ends the call after one pass over the hosts.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum, auto
from typing import Final, Literal

from hoarddb._core.kinds import Kind

BACKOFF_BASE: Final = 0.1
BACKOFF_CAP: Final = 5.0
# A call with no deadline would otherwise retry an overloaded server for ever. At the 5 s cap this
# is a little over two minutes of waiting, then the last error is raised.
MAX_BACKOFF_RETRIES: Final = 30

_UNAVAILABLE: Final = 6
_UNSUPPORTED_VERSION: Final = 10
_NOT_AUTHENTICATED: Final = 12
_OVERLOADED: Final = 13

# The codes after which the server leaves the connection open (spec 6.2 item 5, taken as the Go
# driver takes it): not found, already exists, result too large, field type.
_SURVIVORS: Final = frozenset({1, 2, 14, 16})


def backoff_cap(failures: int) -> float:
    """The most we wait before attempt ``failures + 1``: 100 ms doubling to a 5 s cap."""
    exponent = min(max(failures, 1), 12) - 1
    return float(min(BACKOFF_CAP, BACKOFF_BASE * 2**exponent))


def backoff_delay(failures: int, jitter: float) -> float:
    """Full jitter: ``jitter`` in [0, 1) times the cap. Also the pool's per-lane dial delay."""
    return jitter * backoff_cap(failures)


def keeps_connection(code: int, *, zero_id: bool) -> bool:
    """Whether the connection that answered an ``ERROR`` frame may be reused."""
    return not zero_id and code in _SURVIVORS


class Action(Enum):
    RAISE = auto()  # give up; the error is the one that was just seen
    RETRY_NEW_CONNECTION = auto()  # discard this connection, resend now on another
    RETRY_AFTER = auto()  # wait ``delay`` seconds, then resend
    REAUTH_THEN_RESEND = auto()  # a token AUTH on this connection, then resend once


@dataclass(frozen=True, slots=True)
class RetryState:
    failures: int = 0  # backoff retries so far
    reconnected: bool = False  # the one "retry once on a new connection" has been used
    reauthed: bool = False  # the one NOT_AUTHENTICATED recovery has been used
    has_token: bool = False  # a token is held, so re-authentication is a token AUTH


@dataclass(frozen=True, slots=True)
class Failure:
    """What went wrong, in the table's own terms."""

    event: Literal["dial_failed", "closed_before_write", "lost_after_write", "error_frame"]
    code: int = 0  # the ERROR frame's code
    zero_id: bool = False  # the frame's request ID was all zeros
    dial_phase: bool = False  # it arrived while dialling (the answer to AUTH)
    write_outcome: str | None = None


@dataclass(frozen=True, slots=True)
class Decision:
    action: Action
    state: RetryState
    delay: float = 0.0
    redial: bool = False  # the retry is a fresh dial, not a resend on a live connection
    protocol_violation: bool = False  # raise a ProtocolError, outcome unknown for a write
    reason: str = ""


def _give_up(state: RetryState, reason: str, *, violation: bool = False) -> Decision:
    return Decision(Action.RAISE, state, protocol_violation=violation, reason=reason)


def _after(
    state: RetryState, remaining: float | None, jitter: float, reason: str, *, redial: bool = False
) -> Decision:
    if state.failures >= MAX_BACKOFF_RETRIES:
        return _give_up(state, f"gave up after {MAX_BACKOFF_RETRIES} retries with backoff")
    failures = state.failures + 1
    delay = backoff_delay(failures, jitter)
    if remaining is not None and (remaining <= 0 or delay > remaining):
        return _give_up(state, "the wait would pass the call's deadline")
    return Decision(
        Action.RETRY_AFTER, replace(state, failures=failures), delay, redial=redial, reason=reason
    )


def decide(
    kind: Kind,
    failure: Failure,
    state: RetryState,
    remaining: float | None,
    jitter: float,
) -> Decision:
    """The table. ``remaining`` is the seconds left in the call's deadline (``None``: unbounded)."""
    if remaining is not None and remaining <= 0:
        return _give_up(state, "the call's deadline has passed")
    read = kind == "READ"

    if failure.event == "dial_failed":
        # One pass over the host list, then ConnectionFailedError: never again in the call.
        return _give_up(state, "a failed dial ends the call after one pass over the hosts")

    if failure.event in ("closed_before_write", "lost_after_write"):
        if read and not state.reconnected:
            return Decision(
                Action.RETRY_NEW_CONNECTION,
                replace(state, reconnected=True),
                reason="a read is retried once on a new connection",
            )
        return _give_up(state, "a write is never retried after a connection problem")

    code = failure.code
    if failure.zero_id:
        if failure.dial_phase and code == _OVERLOADED:
            return _after(
                state, remaining, jitter, "the server's connection cap is full", redial=True
            )
        if failure.dial_phase:
            return _give_up(state, "a connection-level refusal that is not OVERLOADED")
        return _give_up(state, "a zero request ID on an authenticated connection", violation=True)

    if code == _UNAVAILABLE:
        if read or (failure.write_outcome == "not_applied"):
            return _after(state, remaining, jitter, "UNAVAILABLE: nothing was applied")
        return _give_up(state, "UNAVAILABLE with an outcome that is not not_applied")
    if code == _OVERLOADED:
        if read:
            return _after(state, remaining, jitter, "OVERLOADED answered this request")
        return _give_up(state, "OVERLOADED on a write: the outcome is unknown")
    if code == _NOT_AUTHENTICATED:
        if not read or state.reauthed:
            return _give_up(state, "NOT_AUTHENTICATED is recovered once, and only for a read")
        used = replace(state, reauthed=True)
        if state.has_token:
            return Decision(Action.REAUTH_THEN_RESEND, used, reason="a token AUTH, then resend")
        return Decision(Action.RETRY_NEW_CONNECTION, used, reason="discard and redial once")
    return _give_up(state, "the same request fails the same way")


class HostPass:
    """One pass over the host list in the written order (spec 6.3, 8.1).

    A failed dial moves to the next host; when the last host has failed the pass is over and the
    call fails with ``ConnectionFailedError``. A zero-ID ``OVERLOADED`` is not a failed dial: the
    host is alive and full, so the same host is dialled again after the backoff.
    """

    __slots__ = ("_count", "attempted", "current", "exhausted")

    def __init__(self, count: int) -> None:
        if count < 1:
            raise ValueError("a host list is never empty")
        self._count = count
        self.current = 0
        self.attempted: list[int] = [0]
        self.exhausted = False

    def failed(self) -> bool:
        """The dial to ``current`` failed; True when there is a next host to try."""
        if self.current + 1 >= self._count:
            self.exhausted = True
            return False
        self.current += 1
        self.attempted.append(self.current)
        return True

    def overloaded(self) -> None:
        """The host answered but is full: stay on it."""
