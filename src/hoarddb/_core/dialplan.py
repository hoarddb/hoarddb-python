# SPDX-License-Identifier: Apache-2.0
"""How one new connection is obtained, as a pure generator (spec 5.2, 6.3, 8.1, 8.3).

The plan yields :class:`Open` (dial this host with this credential) and :class:`Sleep`, and is sent
what the runner met: ``(connection, AuthOkResponse)`` for an ``Open``, anything for a ``Sleep``; an
exception the dial raised is thrown in. It decides *what to do next*: the next host after a failed
dial (one pass, never a second), the same host after a zero-ID ``OVERLOADED`` (with backoff), the
password on a new connection after a dead token (once per token, staggered), and when to stop. It
holds no socket, clock or lock: the clocks and the random number are passed in.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from dataclasses import dataclass
from typing import Any, Protocol

from hoarddb import errors
from hoarddb._core import diagnostics, retry
from hoarddb._core.config import ResolvedOptions
from hoarddb._core.failure import ZERO_ID
from hoarddb._core.interrupt import Interrupted
from hoarddb._core.kinds import Kind
from hoarddb._core.messages import AuthOkResponse
from hoarddb._core.token import Credential, Rejection
from hoarddb._core.uri import HostPort
from hoarddb._secret import Secret

_ZERO_HEX = ZERO_ID.hex()
_TOKEN_REASONS = frozenset({"TOKEN_EXPIRED", "TOKEN_INVALID", "TOKEN_UNSUPPORTED"})


class Tokens(Protocol):
    """The part of the token manager the plan uses (the pool puts a lock in front of it)."""

    def credential(self, now_mono: float) -> Credential: ...

    @property
    def token(self) -> Secret | None: ...

    def rejected(self, reason: str, *, jitter: float) -> Rejection: ...

    def adopt(self, ok: AuthOkResponse, *, now_mono: float, now_wall: float) -> None: ...


@dataclass(frozen=True, slots=True)
class Open:
    """Dial ``host`` (the ``index``-th of the written list) and ``AUTH`` with ``credential``."""

    index: int
    host: HostPort
    pin: str | None
    credential: Credential
    token: Secret | None


@dataclass(frozen=True, slots=True)
class Sleep:
    seconds: float


DialEffect = Open | Sleep
DialPlan = Generator[DialEffect, Any, Any]


def plan(
    options: ResolvedOptions,
    tokens: Tokens,
    *,
    kind: Kind,
    remaining: Callable[[], float | None],
    rng: Callable[[], float],
    mono: Callable[[], float],
    wall: Callable[[], float],
) -> DialPlan:
    """Obtain one authenticated connection; returns what the last ``Open`` was sent."""
    hosts = options.hosts
    walk = retry.HostPass(len(hosts))
    state = retry.RetryState()
    failures: list[diagnostics.DialFailed] = []

    def wait(seconds: float) -> float:
        left = remaining()
        return seconds if left is None else min(seconds, left)

    while True:
        left = remaining()
        if left is not None and left <= 0:
            raise Interrupted("timeout", sent=False, reason="the deadline passed while connecting")
        credential = tokens.credential(mono())
        if credential == "none":
            raise errors.ConfigError(
                "The token this client holds has expired and no password was given, so there is "
                "nothing to authenticate with. Pass password=... (or set HOARDDB_ROOT_PASSWORD), "
                "or a fresh auth_token=.",
                code="NO_PASSWORD",
                operation="connect",
            )
        index = walk.current
        token = tokens.token if credential == "token" else None
        try:
            result = yield Open(index, hosts[index], options.pin_for(index), credential, token)
        except errors.AuthenticationError as bad:
            if bad.reason in _TOKEN_REASONS and credential == "token":
                outcome = tokens.rejected(bad.reason, jitter=rng())
                if not outcome.use_password:
                    raise
                if outcome.delay > 0:
                    yield Sleep(wait(outcome.delay))
                continue  # the same host, the password, a new connection
            if bad.reason == "INTERNAL":  # not ready to check logins: another node may be
                failures.append(_dial_failure(hosts[index], bad.message, kind))
                if walk.failed():
                    continue
                raise _pass_over(options, failures, kind) from None
            raise
        except errors.OverloadedError as busy:
            if busy.request_id != _ZERO_HEX:
                raise
            decision = retry.decide(
                kind,
                retry.Failure("error_frame", code=13, zero_id=True, dial_phase=True),
                state,
                remaining(),
                rng(),
            )
            if decision.action is not retry.Action.RETRY_AFTER:
                raise
            state = decision.state
            walk.overloaded()  # the host is alive and full: stay on it
            yield Sleep(decision.delay)
            continue
        except errors.ConnectionFailedError as lost:
            reason = lost.reason if isinstance(lost, diagnostics.DialFailed) else lost.message
            failures.append(_dial_failure(hosts[index], reason, kind))
            if walk.failed():
                continue
            raise _pass_over(options, failures, kind) from None
        tokens.adopt(result[1], now_mono=mono(), now_wall=wall())
        return result


def _dial_failure(host: HostPort, reason: str, kind: Kind) -> diagnostics.DialFailed:
    return diagnostics.connection_failed([str(host)], reason, "connect", kind)


def _pass_over(
    options: ResolvedOptions, failures: list[diagnostics.DialFailed], kind: Kind
) -> errors.ConnectionFailedError:
    """The one pass over the host list ended with every dial refused or unreachable."""
    names = [str(h) for h in options.hosts]
    if len(failures) == 1:
        return failures[0]
    reason = "; ".join(f"{name}: {f.reason}" for name, f in zip(names, failures, strict=False))
    return diagnostics.connection_failed(names, reason, "connect", kind)
