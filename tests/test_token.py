# SPDX-License-Identifier: Apache-2.0
"""P5b: the token rules (auth.md section 3, spec 5.2) on a monotonic clock that is passed in."""

from __future__ import annotations

import pytest

from hoarddb._core import token
from hoarddb._core.messages import AuthOkResponse
from hoarddb._secret import Secret

WALL = 1_700_000_000.0


def auth_ok(
    value: str = "tok-1",
    expires_in: int = 100,
    deadline_in: int = 43_200,
    caps: tuple[str, ...] = ("auth_token", "write_concern"),
    wall: float = WALL,
) -> AuthOkResponse:
    return AuthOkResponse(
        success=True,
        token=Secret(value),
        token_expires_at=int(wall) + expires_in,
        token_deadline=int(wall) + deadline_in,
        capabilities=list(caps),
    )


def manager(have_password: bool = True) -> token.TokenManager:
    return token.TokenManager(have_password=have_password)


def test_before_any_token_a_new_connection_uses_the_password() -> None:
    assert manager().credential(0.0) == "password"


def test_after_the_first_auth_new_connections_present_the_token() -> None:
    m = manager()
    m.adopt(auth_ok(), now_mono=1000.0, now_wall=WALL)
    assert m.credential(1001.0) == "token"
    assert m.token is not None and m.token.reveal() == "tok-1"


def test_the_token_is_ignored_when_the_server_lacks_the_capability() -> None:
    m = manager()
    m.adopt(auth_ok(caps=("write_concern",)), now_mono=0.0, now_wall=WALL)
    assert m.token is None
    assert m.credential(1.0) == "password"
    assert not m.refresh_due(1000.0)


def test_refresh_is_due_at_half_the_lifetime_on_the_monotonic_clock() -> None:
    m = manager()
    m.adopt(auth_ok(expires_in=100), now_mono=1000.0, now_wall=WALL)
    assert not m.refresh_due(1049.9)
    assert m.refresh_due(1050.0)
    assert m.refresh_due(1099.0)


def test_the_wall_clock_is_read_once_at_receipt_so_stepping_it_cannot_matter() -> None:
    m = manager()
    # the token arrives 100 s from expiry by the wall clock at that instant
    m.adopt(auth_ok(expires_in=100), now_mono=5.0, now_wall=WALL)
    # an NTP step of a day changes nothing: refresh_due and credential take only the monotonic time
    assert not m.refresh_due(5.0 + 49.0)
    assert m.refresh_due(5.0 + 50.0)


def test_a_short_lived_token_is_refreshed_at_half_of_its_short_life() -> None:
    m = manager()
    m.adopt(auth_ok(expires_in=2), now_mono=10.0, now_wall=WALL)
    assert not m.refresh_due(10.9)
    assert m.refresh_due(11.0)
    assert not m.refresh_due(12.0)  # past its own expiry: nothing left to refresh with


def test_refresh_is_single_flight() -> None:
    m = manager()
    m.adopt(auth_ok(), now_mono=0.0, now_wall=WALL)
    assert m.begin_refresh() is True
    assert m.begin_refresh() is False  # someone else is already refreshing
    assert not m.refresh_due(1000.0)  # and nobody else is asked to
    m.end_refresh()
    assert m.begin_refresh() is True


def test_a_refreshed_token_replaces_the_old_one_and_restarts_the_clock() -> None:
    m = manager()
    m.adopt(auth_ok("a", expires_in=100), now_mono=0.0, now_wall=WALL)
    m.adopt(auth_ok("b", expires_in=100, wall=WALL + 60), now_mono=60.0, now_wall=WALL + 60)
    assert m.token is not None and m.token.reveal() == "b"
    assert not m.refresh_due(60.0 + 49.0)
    assert m.refresh_due(60.0 + 50.0)


def test_past_the_token_deadline_the_password_is_used_again() -> None:
    m = manager()
    m.adopt(auth_ok(expires_in=1000, deadline_in=300), now_mono=0.0, now_wall=WALL)
    assert m.credential(299.0) == "token"
    assert m.credential(300.0) == "password"
    assert not m.refresh_due(300.0)  # no refresh past the end of the chain


def test_an_expired_token_is_not_presented() -> None:
    m = manager()
    m.adopt(auth_ok(expires_in=100), now_mono=0.0, now_wall=WALL)
    assert m.credential(100.0) == "password"


def test_each_token_earns_exactly_one_password_fallback() -> None:
    m = manager()
    m.adopt(auth_ok("a"), now_mono=0.0, now_wall=WALL)
    first = m.rejected("TOKEN_EXPIRED", jitter=0.5)
    assert first.use_password is True
    assert 0.0 < first.delay <= token.FALLBACK_JITTER_MAX
    # the rejected token is not presented again, and a second rejection earns nothing
    assert m.credential(1.0) == "password"
    m.adopt(auth_ok("b"), now_mono=2.0, now_wall=WALL)  # the password AUTH minted a new token
    again = m.rejected("TOKEN_INVALID", jitter=0.25)
    assert again.use_password is True  # the new token earns its own fallback
    # but the same token cannot earn two
    m.adopt(auth_ok("c"), now_mono=3.0, now_wall=WALL)
    assert m.rejected("TOKEN_INVALID", jitter=0.0).use_password is True
    assert m.rejected("TOKEN_INVALID", jitter=0.0).use_password is False


def test_a_rejected_token_without_a_password_is_surfaced() -> None:
    m = manager(have_password=False)
    m.adopt(auth_ok(), now_mono=0.0, now_wall=WALL)
    assert m.rejected("TOKEN_EXPIRED", jitter=0.0).use_password is False


def test_tokens_unsupported_discards_every_token_for_good() -> None:
    m = manager()
    m.adopt(auth_ok(), now_mono=0.0, now_wall=WALL)
    outcome = m.rejected("TOKEN_UNSUPPORTED", jitter=1.0)
    assert outcome.use_password is True
    assert outcome.delay == 0.0
    assert m.token is None
    m.adopt(auth_ok("again"), now_mono=1.0, now_wall=WALL)
    assert m.token is None  # a server that said no tokens is not asked to hand one out again
    assert m.credential(2.0) == "password"


def test_a_program_supplied_token_is_used_for_the_first_connection_then_refreshed() -> None:
    m = manager(have_password=False)
    m.supplied(Secret("from-uri"))
    assert m.credential(0.0) == "token"
    assert not m.refresh_due(10_000.0)  # unknown lifetime: no refresh clock yet
    m.adopt(auth_ok("minted"), now_mono=5.0, now_wall=WALL)
    assert m.token is not None and m.token.reveal() == "minted"
    assert m.refresh_due(5.0 + 50.0)


def test_the_password_is_not_needed_while_a_token_lives() -> None:
    m = manager(have_password=False)
    m.adopt(auth_ok(), now_mono=0.0, now_wall=WALL)
    assert m.credential(1.0) == "token"
    assert m.credential(1_000_000.0) == "none"  # nothing left to present: the caller must say so


def test_the_manager_never_prints_the_token() -> None:
    m = manager()
    m.adopt(auth_ok("tok-sentinel"), now_mono=0.0, now_wall=WALL)
    assert "tok-sentinel" not in repr(m)
    assert "tok-sentinel" not in str(m)


# --- security review M3: only a dead token may fall back to the password ----------------------


@pytest.mark.parametrize(
    "reason",
    [
        "RATE_LIMITED",
        "CREDENTIALS",
        "PROTOCOL_MISMATCH",
        "ALREADY_AUTHED",
        "INTERNAL",
        "SOMETHING_NEW",
    ],
)
def test_a_reason_that_does_not_condemn_the_token_keeps_it_and_does_not_use_the_password(
    reason: str,
) -> None:
    m = manager()
    m.adopt(auth_ok("keep-me"), now_mono=0.0, now_wall=WALL)
    outcome = m.rejected(reason, jitter=0.5)
    assert outcome.use_password is False
    assert outcome.delay == 0.0
    assert m.token is not None
    assert m.token.reveal() == "keep-me"
    assert m.credential(1.0) == "token"
    # the fallback was not spent: a real expiry still earns it
    assert m.rejected("TOKEN_EXPIRED", jitter=0.0).use_password is True


@pytest.mark.parametrize("reason", ["TOKEN_EXPIRED", "TOKEN_INVALID"])
def test_an_expired_or_invalid_token_is_dropped_and_falls_back(reason: str) -> None:
    m = manager()
    m.adopt(auth_ok(), now_mono=0.0, now_wall=WALL)
    assert m.rejected(reason, jitter=0.0).use_password is True
    assert m.token is None
