# SPDX-License-Identifier: Apache-2.0
"""P9a: the dial plan as a pure generator: host failover, token fallback, OVERLOADED backoff."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import pytest

from hoarddb import errors
from hoarddb._core import config, dialplan
from hoarddb._core.dialplan import Open, Sleep
from hoarddb._core.messages import AuthOkResponse
from hoarddb._core.token import TokenManager
from hoarddb._secret import Secret

WALL = 1_700_000_000.0


def opts(uri: str = "hoarddb://u:p@a,b,c", **extra: Any) -> config.ResolvedOptions:
    return config.resolve_options(uri, extra, {})


def ok(token: str | None = "t1", caps: tuple[str, ...] = ("auth_token",)) -> AuthOkResponse:
    return AuthOkResponse(
        success=True,
        token=Secret(token) if token else None,
        token_expires_at=int(WALL) + 900,
        token_deadline=int(WALL) + 43_200,
        capabilities=list(caps),
        server_version="9.9",
    )


def auth_error(reason: str) -> errors.AuthenticationError:
    return errors.AuthenticationError(
        f"auth {reason}", reason=reason, retryable=reason in ("RATE_LIMITED", "INTERNAL")
    )


def refused(host: str) -> errors.ConnectionFailedError:
    return errors.ConnectionFailedError(f"{host} refused", retryable=True)


def overloaded() -> errors.OverloadedError:
    return errors.OverloadedError("full", retryable=True, request_id="00" * 16)


class Harness:
    """Drives a plan the way a runner does, scripting what each effect meets."""

    def __init__(
        self,
        options: config.ResolvedOptions,
        tokens: TokenManager | None = None,
        *,
        kind: str = "READ",
        remaining: float | None = 30.0,
    ) -> None:
        self.options = options
        self.tokens = tokens or TokenManager(have_password=options.password is not None)
        self.effects: list[Any] = []
        self.plan: Generator[Any, Any, Any] = dialplan.plan(
            options,
            self.tokens,
            kind=kind,  # type: ignore[arg-type]
            remaining=lambda: remaining,
            rng=lambda: 1.0,
            mono=lambda: 100.0,
            wall=lambda: WALL,
        )

    def run(self, script: list[Any]) -> Any:
        """``script`` answers each effect in order: an exception is thrown in, else sent."""
        answers = iter(script)
        try:
            effect = next(self.plan)
            while True:
                self.effects.append(effect)
                answer = next(answers)
                if isinstance(effect, Sleep) or not isinstance(answer, BaseException):
                    effect = self.plan.send(answer)
                else:
                    effect = self.plan.throw(answer)
        except StopIteration as done:
            return done.value


CONN = object()


def test_the_first_host_is_tried_first_with_its_pin_and_the_password() -> None:
    h = Harness(opts("hoarddb://u:p@a,b"))
    conn, auth_ok = h.run([(CONN, ok())])
    assert conn is CONN and auth_ok.server_version == "9.9"
    (effect,) = h.effects
    assert isinstance(effect, Open)
    assert (effect.index, str(effect.host), effect.credential) == (0, "a:7433", "password")
    assert effect.token is None


def test_a_successful_auth_gives_the_token_to_the_manager() -> None:
    h = Harness(opts())
    h.run([(CONN, ok("tok"))])
    assert h.tokens.credential(101.0) == "token"
    assert h.tokens.token is not None and h.tokens.token.reveal() == "tok"


def test_a_failed_dial_moves_to_the_next_host_in_the_written_order() -> None:
    h = Harness(opts())
    h.run([refused("a"), refused("b"), (CONN, ok())])
    assert [str(e.host) for e in h.effects] == ["a:7433", "b:7433", "c:7433"]


def test_one_pass_over_the_hosts_ends_in_connection_failed_naming_them_all() -> None:
    h = Harness(opts())
    with pytest.raises(errors.ConnectionFailedError) as caught:
        h.run([refused("a"), refused("b"), refused("c")])
    text = str(caught.value)
    assert "a:7433" in text and "b:7433" in text and "c:7433" in text
    assert caught.value.retryable is True
    assert len(h.effects) == 3  # never a second pass inside the call


def test_a_failed_dial_of_a_write_says_nothing_was_applied() -> None:
    h = Harness(opts("hoarddb://u:p@a"), kind="WRITE")
    with pytest.raises(errors.ConnectionFailedError) as caught:
        h.run([refused("a")])
    assert caught.value.write_outcome == "not_applied"


def test_a_zero_id_overloaded_stays_on_the_host_and_backs_off() -> None:
    h = Harness(opts("hoarddb://u:p@a,b"))
    h.run([overloaded(), None, overloaded(), None, (CONN, ok())])
    kinds = [(type(e).__name__, getattr(e, "index", None)) for e in h.effects]
    assert kinds == [("Open", 0), ("Sleep", None), ("Open", 0), ("Sleep", None), ("Open", 0)]
    sleeps = [e.seconds for e in h.effects if isinstance(e, Sleep)]
    assert sleeps == [0.1, 0.2]  # full jitter at 1.0: 100 ms doubling


@pytest.mark.parametrize("kind", ["READ", "WRITE", "DDL"])
def test_the_overload_retry_applies_to_every_kind(kind: str) -> None:
    h = Harness(opts("hoarddb://u:p@a"), kind=kind)
    h.run([overloaded(), None, (CONN, ok())])
    assert len(h.effects) == 3


def test_overloaded_gives_up_when_the_wait_would_pass_the_deadline() -> None:
    h = Harness(opts("hoarddb://u:p@a"), remaining=0.05)
    with pytest.raises(errors.OverloadedError):
        h.run([overloaded()])


def test_overloaded_is_bounded_even_with_no_deadline() -> None:
    h = Harness(opts("hoarddb://u:p@a"), remaining=None)
    with pytest.raises(errors.OverloadedError):
        h.run([overloaded(), None] * 200)
    assert len(h.effects) < 100


@pytest.mark.parametrize(
    "reason", ["CREDENTIALS", "PROTOCOL_MISMATCH", "RATE_LIMITED", "ALREADY_AUTHED"]
)
def test_these_auth_failures_stop_the_call_at_once(reason: str) -> None:
    h = Harness(opts())
    with pytest.raises(errors.AuthenticationError) as caught:
        h.run([auth_error(reason)])
    assert caught.value.reason == reason
    assert len(h.effects) == 1  # not retried, not tried on another host


def test_internal_tries_the_next_host() -> None:
    h = Harness(opts())
    h.run([auth_error("INTERNAL"), (CONN, ok())])
    assert [str(e.host) for e in h.effects] == ["a:7433", "b:7433"]


def test_an_expired_token_falls_back_to_the_password_once_on_a_new_connection() -> None:
    tokens = TokenManager(have_password=True)
    tokens.adopt(ok("old"), now_mono=0.0, now_wall=WALL)
    h = Harness(opts("hoarddb://u:p@a"), tokens)
    h.run([auth_error("TOKEN_EXPIRED"), None, (CONN, ok("new"))])
    first, pause, second = h.effects
    assert first.credential == "token" and first.token.reveal() == "old"
    assert isinstance(pause, Sleep) and 0 < pause.seconds <= 1.0  # staggered by the jitter
    assert second.credential == "password" and str(second.host) == "a:7433"
    assert tokens.token is not None and tokens.token.reveal() == "new"


def test_a_second_token_refusal_in_a_row_is_surfaced() -> None:
    tokens = TokenManager(have_password=True)
    tokens.adopt(ok("old"), now_mono=0.0, now_wall=WALL)
    h = Harness(opts("hoarddb://u:p@a"), tokens)
    with pytest.raises(errors.AuthenticationError) as caught:
        h.run([auth_error("TOKEN_INVALID"), None, auth_error("TOKEN_INVALID")])
    assert caught.value.reason == "TOKEN_INVALID"


def test_a_rejected_token_without_a_password_is_surfaced() -> None:
    tokens = TokenManager(have_password=False)
    tokens.supplied(Secret("given"))
    h = Harness(opts("hoarddb://u@a?authToken=given"), tokens)
    with pytest.raises(errors.AuthenticationError):
        h.run([auth_error("TOKEN_EXPIRED")])


def test_tokens_unsupported_discards_every_token_and_uses_the_password() -> None:
    tokens = TokenManager(have_password=True)
    tokens.adopt(ok("old"), now_mono=0.0, now_wall=WALL)
    h = Harness(opts("hoarddb://u:p@a"), tokens)
    h.run([auth_error("TOKEN_UNSUPPORTED"), (CONN, ok("ignored"))])
    assert h.effects[-1].credential == "password"
    assert tokens.token is None  # and the server's token is ignored from then on


def test_a_server_without_the_auth_token_capability_gives_no_token() -> None:
    h = Harness(opts("hoarddb://u:p@a"))
    h.run([(CONN, ok("t", caps=()))])
    assert h.tokens.token is None
    assert h.tokens.credential(101.0) == "password"


def test_a_supplied_token_is_the_first_credential() -> None:
    tokens = TokenManager(have_password=True)
    tokens.supplied(Secret("given"))
    h = Harness(opts("hoarddb://u:p@a"), tokens)
    h.run([(CONN, ok())])
    assert h.effects[0].credential == "token" and h.effects[0].token.reveal() == "given"


def test_only_a_dead_token_and_no_password_has_nothing_to_present() -> None:
    tokens = TokenManager(have_password=False)
    tokens.adopt(ok("old"), now_mono=0.0, now_wall=WALL)
    h = Harness(opts("hoarddb://u:p@a"), tokens)
    h.plan = dialplan.plan(  # the token's life is over by the time of the dial
        h.options, tokens, kind="READ", remaining=lambda: 30.0, rng=lambda: 1.0,
        mono=lambda: 5000.0, wall=lambda: WALL,
    )  # fmt: skip
    with pytest.raises(errors.ConfigError, match="password"):
        h.run([])


def test_other_errors_propagate_unchanged() -> None:
    h = Harness(opts())
    trust = errors.TlsTrustError("no", code="TLS_KEY_CHANGED")
    with pytest.raises(errors.TlsTrustError):
        h.run([trust])
    assert len(h.effects) == 1


def test_each_host_uses_its_own_pin() -> None:
    fp1 = "SHA256:" + "1" * 64
    fp2 = "SHA256:" + "2" * 64
    h = Harness(opts(f"hoarddb://u:p@a,b?fingerprint={fp1}&fingerprint={fp2}"))
    h.run([refused("a"), (CONN, ok())])
    assert [e.pin for e in h.effects] == [fp1, fp2]
