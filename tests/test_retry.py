# SPDX-License-Identifier: Apache-2.0
"""P5b: the retry decision table (errors.md section 6, spec 8.1), row by row, with no I/O."""

from __future__ import annotations

import pytest

from hoarddb._core import retry
from hoarddb._core.failure import ZERO_ID, Context, error_from_payload
from hoarddb._core.kinds import Kind
from hoarddb._core.retry import Action, Failure, RetryState

KINDS: tuple[Kind, ...] = ("READ", "WRITE", "DDL")
FRESH = RetryState()


def decide(
    kind: Kind,
    failure: Failure,
    state: RetryState = FRESH,
    remaining: float | None = 30.0,
    jitter: float = 1.0,
) -> retry.Decision:
    return retry.decide(kind, failure, state, remaining, jitter)


def frame_error(code: int, **kwargs: object) -> Failure:
    return Failure("error_frame", code=code, **kwargs)  # type: ignore[arg-type]


# --- backoff (errors.md section 5; spec 8.1) ---------------------------------------------------


def test_backoff_is_full_jitter_from_100ms_doubling_to_a_5s_cap() -> None:
    caps = [retry.backoff_cap(n) for n in range(1, 9)]
    assert caps == [0.1, 0.2, 0.4, 0.8, 1.6, 3.2, 5.0, 5.0]
    assert retry.backoff_delay(3, 0.0) == 0.0
    assert retry.backoff_delay(3, 1.0) == 0.4
    assert retry.backoff_delay(3, 0.5) == pytest.approx(0.2)
    assert retry.backoff_delay(50, 1.0) == 5.0  # no overflow, no growth past the cap


# --- a failed dial: one pass over the host list, never again in the call ------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_a_failed_dial_is_never_retried_within_the_call(kind: Kind) -> None:
    assert decide(kind, Failure("dial_failed")).action is Action.RAISE


def test_one_pass_over_the_host_list_in_the_written_order() -> None:
    hosts = retry.HostPass(3)
    assert hosts.current == 0
    assert hosts.failed() is True
    assert hosts.current == 1
    assert hosts.failed() is True
    assert hosts.current == 2
    assert hosts.failed() is False  # every host refused: the pass is over
    assert hosts.exhausted
    assert hosts.attempted == [0, 1, 2]


def test_a_single_host_pass_ends_at_once_and_a_new_pass_starts_at_the_first_host() -> None:
    one = retry.HostPass(1)
    assert one.failed() is False
    assert retry.HostPass(3).current == 0  # every new connection starts at the first host


def test_a_zero_id_overload_stays_on_the_same_host_and_does_not_end_the_pass() -> None:
    hosts = retry.HostPass(2)
    hosts.overloaded()
    assert hosts.current == 0
    assert not hosts.exhausted


# --- closed or lost connections ----------------------------------------------------------------


def test_a_read_found_closed_before_the_write_retries_once_on_a_new_connection() -> None:
    first = decide("READ", Failure("closed_before_write"))
    assert first.action is Action.RETRY_NEW_CONNECTION
    assert first.delay == 0.0
    again = decide("READ", Failure("closed_before_write"), first.state)
    assert again.action is Action.RAISE


def test_a_read_lost_after_the_write_retries_once_now_on_a_new_connection() -> None:
    first = decide("READ", Failure("lost_after_write"))
    assert first.action is Action.RETRY_NEW_CONNECTION
    assert decide("READ", Failure("lost_after_write"), first.state).action is Action.RAISE


@pytest.mark.parametrize("kind", ["WRITE", "DDL"])
@pytest.mark.parametrize("event", ["closed_before_write", "lost_after_write"])
def test_a_write_or_ddl_is_never_retried_after_a_connection_problem(kind: Kind, event: str) -> None:
    assert decide(kind, Failure(event)).action is Action.RAISE  # type: ignore[arg-type]


# --- UNAVAILABLE -------------------------------------------------------------------------------


def test_unavailable_on_a_read_retries_with_backoff_until_the_deadline() -> None:
    d = decide("READ", frame_error(6), jitter=1.0)
    assert d.action is Action.RETRY_AFTER
    assert d.delay == 0.1
    assert d.state.failures == 1
    d2 = decide("READ", frame_error(6), d.state, jitter=1.0)
    assert d2.delay == 0.2
    assert decide("READ", frame_error(6), d2.state, remaining=0.05).action is Action.RAISE


@pytest.mark.parametrize("kind", ["WRITE", "DDL"])
def test_unavailable_on_a_write_retries_only_when_nothing_was_applied(kind: Kind) -> None:
    assert decide(kind, frame_error(6, write_outcome="not_applied")).action is Action.RETRY_AFTER
    for outcome in ("unknown", "applied", None):
        assert decide(kind, frame_error(6, write_outcome=outcome)).action is Action.RAISE


# --- OVERLOADED: two shapes --------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_a_zero_id_overloaded_in_the_dial_phase_is_retried_for_every_kind(kind: Kind) -> None:
    d = decide(kind, frame_error(13, zero_id=True, dial_phase=True))
    assert d.action is Action.RETRY_AFTER
    assert d.redial
    assert decide(kind, frame_error(13, zero_id=True, dial_phase=True), remaining=0.0).action is (
        Action.RAISE
    )


@pytest.mark.parametrize("kind", KINDS)
def test_a_zero_id_frame_on_an_authenticated_connection_is_never_resent(kind: Kind) -> None:
    d = decide(kind, frame_error(13, zero_id=True, dial_phase=False))
    assert d.action is Action.RAISE
    assert d.protocol_violation  # a ProtocolError, outcome unknown for a write (OQ-5)


def test_a_zero_id_unsupported_version_is_not_retried() -> None:
    assert decide("READ", frame_error(10, zero_id=True, dial_phase=True)).action is Action.RAISE


def test_overloaded_with_the_requests_own_id_retries_a_read_and_never_a_write() -> None:
    assert decide("READ", frame_error(13)).action is Action.RETRY_AFTER
    assert decide("WRITE", frame_error(13)).action is Action.RAISE
    assert decide("DDL", frame_error(13)).action is Action.RAISE


# --- NOT_AUTHENTICATED -------------------------------------------------------------------------


def test_not_authenticated_on_a_read_reauthenticates_once_then_resends_once() -> None:
    first = decide("READ", frame_error(12), RetryState(has_token=True))
    assert first.action is Action.REAUTH_THEN_RESEND
    again = decide("READ", frame_error(12), first.state)
    assert again.action is Action.RAISE


def test_without_a_token_it_discards_and_redials_once() -> None:
    first = decide("READ", frame_error(12), RetryState(has_token=False))
    assert first.action is Action.RETRY_NEW_CONNECTION
    assert decide("READ", frame_error(12), first.state).action is Action.RAISE


@pytest.mark.parametrize("kind", ["WRITE", "DDL"])
def test_not_authenticated_is_never_retried_for_a_write(kind: Kind) -> None:
    assert decide(kind, frame_error(12), RetryState(has_token=True)).action is Action.RAISE


# --- the rows that never retry -----------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "code",
    [0, 1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 14, 15, 16, 99],
)
def test_everything_else_never_retries(kind: Kind, code: int) -> None:
    assert decide(kind, frame_error(code, write_outcome="not_applied")).action is Action.RAISE


@pytest.mark.parametrize("kind", KINDS)
def test_a_write_concern_timeout_is_never_retried_because_the_write_is_stored(kind: Kind) -> None:
    assert decide(kind, frame_error(7, write_outcome="applied")).action is Action.RAISE


@pytest.mark.parametrize("kind", KINDS)
def test_result_too_large_is_not_retried_here_the_cursor_pages_smaller(kind: Kind) -> None:
    assert decide(kind, frame_error(14)).action is Action.RAISE


def test_a_deadline_with_nothing_left_never_retries_anything() -> None:
    for failure in (frame_error(6), frame_error(13), Failure("lost_after_write")):
        assert decide("READ", failure, remaining=0.0).action is Action.RAISE


def test_an_unbounded_deadline_always_has_room() -> None:
    assert decide("READ", frame_error(6), remaining=None).action is Action.RETRY_AFTER


# --- consistency with the error objects --------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("code", [1, 4, 6, 7, 12, 13, 14])
@pytest.mark.parametrize("outcome", [None, "not_applied", "unknown", "applied"])
def test_whatever_the_driver_retries_the_error_also_calls_retryable(
    kind: Kind, code: int, outcome: str | None
) -> None:
    extra = {"write_outcome": outcome} if outcome else {}
    payload = {"code": code, "message": "m", **extra}
    if code == 14:
        payload |= {"max_result_bytes": 1, "retained_bytes": 1}
    rid = bytes(range(1, 17))
    err = error_from_payload(payload, rid, Context("op", None, kind))
    failure = frame_error(code, write_outcome=outcome)
    decision = decide(kind, failure, RetryState(has_token=True))
    if decision.action is not Action.RAISE:
        assert err.retryable, (kind, code, outcome)
    zero = frame_error(13, zero_id=True, dial_phase=True)
    zero_err = error_from_payload({"code": 13, "message": "m"}, ZERO_ID, Context("op", None, kind))
    assert decide(kind, zero).action is Action.RETRY_AFTER
    assert zero_err.retryable


# --- which ERROR frames leave a connection reusable (spec 6.2 item 5) ---------------------------


@pytest.mark.parametrize("code", [1, 2, 14, 16, 17, 18])
def test_the_refusals_the_server_keeps_the_connection_open_after(code: int) -> None:
    """The Go driver's list (not found, already exists, result too large, field type, batch too
    large) and document exists: a refused put is an everyday answer, and dialling again after each
    one costs a TLS handshake and a login. tests/conformance pins 17 and 18 against a server."""
    assert retry.keeps_connection(code, zero_id=False) is True


@pytest.mark.parametrize("code", [0, 3, 4, 5, 6, 7, 8, 9, 10, 12, 13, 15, 19, 99])
def test_every_other_error_discards_the_connection(code: int) -> None:
    assert retry.keeps_connection(code, zero_id=False) is False


def test_a_zero_id_refusal_always_discards() -> None:
    for code in (1, 2, 14, 16, 17, 18):
        assert retry.keeps_connection(code, zero_id=True) is False


# --- security review L11: no unbounded retry loop -------------------------------------------------


def _retries_until_raise(failure: Failure) -> tuple[int, retry.Decision]:
    state = FRESH
    retries = 0
    while retries < 100_000:
        d = decide("READ", failure, state, remaining=None, jitter=0.0)
        if d.action is Action.RAISE:
            return retries, d
        assert d.action is Action.RETRY_AFTER
        state = d.state
        retries += 1
    pytest.fail("100,000 retries and still going")


@pytest.mark.parametrize("code", [6, 13])
def test_a_read_without_a_deadline_still_gives_up_after_a_bounded_number_of_retries(
    code: int,
) -> None:
    retries, d = _retries_until_raise(frame_error(code))
    assert retries == retry.MAX_BACKOFF_RETRIES
    assert "retries" in d.reason


def test_a_zero_id_overload_in_the_dial_phase_is_bounded_too() -> None:
    retries, _ = _retries_until_raise(frame_error(13, zero_id=True, dial_phase=True))
    assert retries == retry.MAX_BACKOFF_RETRIES
