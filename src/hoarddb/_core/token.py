# SPDX-License-Identifier: Apache-2.0
"""The token rules (auth.md section 3, spec 5.2) as a state machine with no clock inside.

Times are passed in. The wall clock is read once, at receipt, to turn the server's absolute expiry
times into lifetimes; from then on only the monotonic clock is compared, so a wall clock stepped by
NTP cannot make a token look young or dead. There is no timer: the runner asks :meth:`refresh_due`
at checkout. The manager holds no lock; the runner serialises calls to the methods that mutate it,
and ``begin_refresh`` / ``end_refresh`` make the refresh single-flight.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from hoarddb._core.messages import AuthOkResponse
from hoarddb._secret import Secret

# A fallback to the password is staggered by up to this many seconds so a pool of connections does
# not stampede the server's deliberately slow password check.
FALLBACK_JITTER_MAX: Final = 1.0

# Only these two say the token itself is dead; every other refusal (a rate limit, a protocol
# mismatch, ...) says nothing about the token, and answering it with the password would present
# the password to a server that is already refusing us.
_TOKEN_IS_DEAD: Final = frozenset({"TOKEN_EXPIRED", "TOKEN_INVALID"})

Credential = Literal["token", "password", "none"]


@dataclass(frozen=True, slots=True)
class Rejection:
    """What to do after a token AUTH was refused."""

    use_password: bool
    delay: float = 0.0


class TokenManager:
    __slots__ = (
        "_deadline",
        "_expires",
        "_fallback_available",
        "_have_password",
        "_in_flight",
        "_received",
        "_token",
        "_ttl",
        "_unsupported",
    )

    def __init__(self, *, have_password: bool) -> None:
        self._have_password = have_password
        self._token: Secret | None = None
        self._received: float | None = None  # monotonic time of receipt
        self._ttl = 0.0  # seconds of life the token had at receipt
        self._expires: float | None = None  # monotonic time the token itself expires
        self._deadline: float | None = None  # monotonic time the refresh chain ends
        self._fallback_available = True  # each token earns one password fallback
        self._unsupported = False  # the server said it has no tokens
        self._in_flight = False

    def __repr__(self) -> str:
        return f"TokenManager(token={'***' if self._token else None})"

    @property
    def token(self) -> Secret | None:
        return self._token

    def supplied(self, token: Secret) -> None:
        """A token the program gave (the URI's ``authToken``): used until the server mints one."""
        self._token = token
        self._received = self._expires = self._deadline = None
        self._ttl = 0.0
        self._fallback_available = True

    def adopt(self, ok: AuthOkResponse, *, now_mono: float, now_wall: float) -> None:
        """Take the token of a successful AUTH. Ignored entirely without the ``auth_token``
        capability, and for ever after the server has said it has no tokens."""
        if self._unsupported or ok.token is None or "auth_token" not in ok.capabilities:
            self._token = None
            self._received = self._expires = self._deadline = None
            return
        ttl = max(0.0, ok.token_expires_at - now_wall)
        self._token = ok.token
        self._received = now_mono
        self._ttl = ttl
        self._expires = now_mono + ttl
        self._deadline = now_mono + max(0.0, ok.token_deadline - now_wall)
        self._fallback_available = True  # a new token earns the next fallback

    def credential(self, now_mono: float) -> Credential:
        """What a *new* connection should present."""
        if self._token is not None and not self._unsupported and not self._past(now_mono):
            return "token"
        return "password" if self._have_password else "none"

    def _past(self, now_mono: float) -> bool:
        if self._deadline is not None and now_mono >= self._deadline:
            return True
        return self._expires is not None and now_mono >= self._expires

    def refresh_due(self, now_mono: float) -> bool:
        """True once half the token's life has passed (and nobody is refreshing already)."""
        if self._token is None or self._received is None or self._in_flight:
            return False
        if self._past(now_mono):
            return False
        return now_mono >= self._received + self._ttl / 2

    def begin_refresh(self) -> bool:
        """Become the one refresher; False when a refresh is already in flight."""
        if self._in_flight:
            return False
        self._in_flight = True
        return True

    def end_refresh(self) -> None:
        self._in_flight = False

    def rejected(self, reason: str, *, jitter: float) -> Rejection:
        """A token AUTH was refused with ``reason`` (``AuthenticationError.reason``)."""
        if reason == "TOKEN_UNSUPPORTED":
            self._unsupported = True
            self._token = None
            self._received = self._expires = self._deadline = None
            return Rejection(use_password=self._have_password)
        if reason not in _TOKEN_IS_DEAD:
            return Rejection(use_password=False)  # the token is kept; the caller surfaces the error
        self._token = None  # a refused token is never presented again
        self._received = self._expires = self._deadline = None
        if self._have_password and self._fallback_available:
            self._fallback_available = False
            return Rejection(use_password=True, delay=jitter * FALLBACK_JITTER_MAX)
        return Rejection(use_password=False)
