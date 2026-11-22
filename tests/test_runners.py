# SPDX-License-Identifier: Apache-2.0
"""P10: the two runners, one scenario table, run through both: retry table, deadlines, drive.

Every test takes the ``mode`` fixture and runs once with the blocking runner and once with the
asyncio runner, so the two cannot disagree about what a failure means.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import fakehoard
from hoarddb import errors
from hoarddb._core import config, frame, opcodes
from hoarddb._core.call import Call, Op
from hoarddb._core.kinds import Kind
from hoarddb._core.messages import CountRequest, PutRequest
from hoarddb.options import TlsOptions
from runnerkit import COUNT_OK, PUT_OK, H, count_op, err, harness, put_op, script

# --- driving the generator -----------------------------------------------------------------------


def test_the_return_value_of_the_operation_is_the_result(mode: str, identity: Any) -> None:
    with harness(mode, identity, script(COUNT_OK)) as (h, server):
        assert h.run(count_op()) == 7
        assert len(server.frames_with(opcodes.COUNT)) == 1


def test_an_operation_may_make_several_calls_and_choose_the_next_from_the_last(
    mode: str, identity: Any
) -> None:
    def op() -> Op[int]:
        first = yield from count_op()
        second = yield from count_op()
        return first * 100 + second

    answers = [
        [fakehoard.Reply(opcodes.COUNT, {"count": 3})],
        [fakehoard.Reply(opcodes.COUNT, {"count": 4})],
    ]
    with harness(mode, identity, script(*answers)) as (h, _):
        assert h.run(op()) == 304


def test_an_operation_that_finishes_without_a_call_never_touches_the_pool(
    mode: str, identity: Any
) -> None:
    def op() -> Op[str]:
        return "nothing to ask"
        yield  # pragma: no cover - makes this a generator

    with harness(mode, identity, script(COUNT_OK)) as (h, server):
        assert h.run(op()) == "nothing to ask"
        assert server.connections == []


def test_a_refusal_is_thrown_into_the_operation_so_it_can_recover(mode: str, identity: Any) -> None:
    def op() -> Op[str]:
        try:
            yield from count_op()
        except errors.NotFoundError as missing:
            reply = yield Call(
                op="count",
                target="d/b",
                request=CountRequest(database="d", bucket="b"),
                lane="point",
                kind="READ",
            )
            return f"recovered after {missing.code} with {reply.response.count}"
        return "unreachable"

    with harness(mode, identity, script(err(1, "bucket not found"), COUNT_OK)) as (h, _):
        assert h.run(op()) == "recovered after NOT_FOUND with 7"


def test_an_unhandled_refusal_reaches_the_caller_and_the_pool_is_whole(
    mode: str, identity: Any
) -> None:
    with harness(mode, identity, script(err(3, "permission denied"))) as (h, _):
        with pytest.raises(errors.PermissionDeniedError):
            h.run(count_op())
        stats = h.stats()
        assert stats.point_in_use == 0 and stats.point_idle == 0  # code 3: connection discarded


def test_a_survivor_refusal_leaves_the_connection_in_the_pool(mode: str, identity: Any) -> None:
    with harness(mode, identity, script(err(1, "bucket not found"), COUNT_OK)) as (h, server):
        with pytest.raises(errors.NotFoundError):
            h.run(count_op())
        assert h.run(count_op()) == 7
        assert len(server.connections) == 1


# --- the retry table, row by row -----------------------------------------------------------------


def test_unavailable_on_a_read_is_retried_with_backoff(mode: str, identity: Any) -> None:
    with harness(mode, identity, script(err(6, "no holder"), err(6, "no holder"), COUNT_OK)) as (
        h,
        _,
    ):
        assert h.run(count_op()) == 7
        assert h.sleeps == [0.1, 0.2]


def test_unavailable_on_a_read_gives_up_at_the_deadline_with_the_last_error(
    mode: str, identity: Any
) -> None:
    with harness(mode, identity, script(err(6, "no holder"))) as (h, _):
        with pytest.raises(errors.UnavailableError) as caught:
            h.run(count_op(timeout=0.15))
        assert caught.value.retryable


def test_unavailable_on_a_write_is_retried_only_when_nothing_was_applied(
    mode: str, identity: Any
) -> None:
    refused = err(6, "not the holder", write_outcome="not_applied")
    with harness(mode, identity, script(refused, PUT_OK)) as (h, _):
        assert h.run(put_op()) == "k"
        assert h.sleeps == [0.1]
    with harness(mode, identity, script(err(6, "unsure", write_outcome="unknown"), PUT_OK)) as (
        h,
        server,
    ):
        with pytest.raises(errors.UnavailableError) as caught:
            h.run(put_op())
        assert caught.value.write_outcome == "unknown" and not caught.value.retryable
        assert len(server.frames_with(opcodes.PUT)) == 1


def test_an_overloaded_answer_with_the_requests_own_id_retries_a_read_never_a_write(
    mode: str, identity: Any
) -> None:
    with harness(mode, identity, script(err(13, "busy"), COUNT_OK)) as (h, _):
        assert h.run(count_op()) == 7
    with harness(mode, identity, script(err(13, "busy"), PUT_OK)) as (h, server):
        with pytest.raises(errors.OverloadedError):
            h.run(put_op())
        assert len(server.frames_with(opcodes.PUT)) == 1


def test_not_authenticated_on_a_read_redials_once_then_resends_once(
    mode: str, identity: Any
) -> None:
    with harness(mode, identity, script(err(12, "login first"), COUNT_OK)) as (h, server):
        assert h.run(count_op()) == 7
        assert len(server.connections) == 2  # discarded and redialled
    with harness(mode, identity, script(err(12, "login first"))) as (h, server):
        with pytest.raises(errors.NotAuthenticatedError):
            h.run(count_op())
        assert len(server.frames_with(opcodes.COUNT)) == 2  # one resend, no loop


def test_not_authenticated_on_a_write_is_never_resent(mode: str, identity: Any) -> None:
    with harness(mode, identity, script(err(12, "login first"), PUT_OK)) as (h, server):
        with pytest.raises(errors.NotAuthenticatedError):
            h.run(put_op())
        assert len(server.frames_with(opcodes.PUT)) == 1


@pytest.mark.parametrize("code", [7, 14, 4, 15])
def test_these_refusals_are_never_retried(mode: str, identity: Any, code: int) -> None:
    extra: dict[str, Any] = {"max_result_bytes": 10, "retained_bytes": 20} if code == 14 else {}
    with harness(mode, identity, script(err(code, "no", **extra), PUT_OK)) as (h, server):
        with pytest.raises(errors.HoardDBError):
            h.run(count_op() if code == 14 else put_op())
        assert len(server.frames) == 2  # AUTH and the one request


def test_a_read_lost_after_the_write_is_retried_once_on_a_new_connection(
    mode: str, identity: Any
) -> None:
    with harness(mode, identity, script([fakehoard.Close()], COUNT_OK)) as (h, server):
        assert h.run(count_op()) == 7
        assert len(server.connections) == 2


def test_a_read_lost_twice_is_connection_failed_and_retryable(mode: str, identity: Any) -> None:
    with harness(mode, identity, script([fakehoard.Close()])) as (h, server):
        with pytest.raises(errors.ConnectionFailedError) as caught:
            h.run(count_op())
        assert caught.value.retryable and caught.value.write_outcome is None
        assert len(server.frames_with(opcodes.COUNT)) == 2


@pytest.mark.parametrize(
    ("kind", "needle"), [("WRITE", "do not resend a push"), ("DDL", "list_buckets")]
)
def test_a_write_or_ddl_lost_after_the_write_is_never_retried_and_says_unknown(
    mode: str, identity: Any, kind: Kind, needle: str
) -> None:
    with harness(mode, identity, script([fakehoard.Close()], PUT_OK)) as (h, server):
        with pytest.raises(errors.ConnectionFailedError) as caught:
            h.run(put_op(kind))
        assert caught.value.write_outcome == "unknown" and not caught.value.retryable
        assert needle in str(caught.value)
        assert len(server.frames_with(opcodes.PUT)) == 1


def test_a_failed_dial_ends_the_call_after_one_pass(mode: str, identity: Any) -> None:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    options = config.resolve_options(
        f"hoarddb://admin:pw@127.0.0.1:{port}",
        {"tls": TlsOptions(fingerprints=[identity.fingerprint])},
        {},
    )
    h = H(mode, options, 5.0)
    try:
        with pytest.raises(errors.ConnectionFailedError) as caught:
            h.run(put_op())
        assert caught.value.write_outcome == "not_applied" and caught.value.retryable
        assert h.stats().dials == 1  # never a second pass inside the call
    finally:
        h.close()


# --- deadlines -----------------------------------------------------------------------------------


def test_a_silent_server_is_a_read_timeout_that_is_retryable(mode: str, identity: Any) -> None:
    with harness(mode, identity, script([fakehoard.Hang()])) as (h, _):
        with pytest.raises(errors.RequestTimeoutError) as caught:
            h.run(count_op(timeout=0.3))
        assert caught.value.retryable and caught.value.write_outcome is None
        assert isinstance(caught.value, TimeoutError)
        assert h.stats().point_in_use == 0


def test_a_timeout_on_a_sent_write_means_the_outcome_is_unknown(mode: str, identity: Any) -> None:
    def op() -> Op[None]:
        yield Call(
            op="put",
            target="d/b",
            request=PutRequest(database="d", bucket="b", key="k", data={}),
            lane="point",
            kind="WRITE",
            timeout=0.3,
        )

    with harness(mode, identity, script([fakehoard.Hang()])) as (h, _):
        with pytest.raises(errors.RequestTimeoutError) as caught:
            h.run(op())
        assert caught.value.write_outcome == "unknown" and not caught.value.retryable
        assert "do not resend a push" in str(caught.value)


def test_a_deadline_already_spent_sends_nothing_and_says_it_is_safe_to_repeat(
    mode: str, identity: Any
) -> None:
    def op() -> Op[None]:
        yield Call(
            op="put",
            target="d/b",
            request=PutRequest(database="d", bucket="b", key="k", data={}),
            lane="point",
            kind="WRITE",
            timeout=1e-9,  # positive: 0 is refused; this is spent before anything is written
        )

    with harness(mode, identity, script(PUT_OK)) as (h, server):
        with pytest.raises(errors.HoardDBError) as caught:
            h.run(op())
        assert caught.value.write_outcome == "not_applied" or caught.value.retryable
        assert server.frames_with(opcodes.PUT) == []


def test_the_default_request_timeout_applies_when_a_call_gives_none(
    mode: str, identity: Any
) -> None:
    hang = script([fakehoard.Hang()])
    with (
        harness(mode, identity, hang, request_timeout=0.3) as (h, _),
        pytest.raises(errors.RequestTimeoutError),
    ):
        h.run(count_op())


# --- interrupts and the pool ---------------------------------------------------------------------


def test_a_frame_too_large_is_refused_locally_and_the_connection_stays(
    mode: str, identity: Any
) -> None:
    big = {"blob": b"x" * (frame.MAX_LENGTH + 1)}
    with harness(mode, identity, script(PUT_OK)) as (h, server):
        with pytest.raises(errors.FrameTooLargeError):
            h.run(put_op(doc=big))
        assert h.run(put_op()) == "k"
        assert len(server.connections) == 1


def test_a_keyboard_interrupt_in_the_middle_of_a_blocking_call_discards_the_connection(
    identity: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hoarddb._sync.connection import Connection

    def boom(self: Connection, deadline: Any) -> frame.Frame:
        raise KeyboardInterrupt

    with harness("sync", identity, script(COUNT_OK)) as (h, server):
        monkeypatch.setattr(Connection, "_read_frame", boom)
        with pytest.raises(KeyboardInterrupt):
            h.run(count_op())
        monkeypatch.undo()
        stats = h.stats()
        assert stats.point_in_use == 0 and stats.point_idle == 0
        assert h.run(count_op()) == 7  # a new connection answers; no stale reply is read
        assert len(server.connections) == 2


def test_cancelling_an_async_call_discards_the_connection_and_frees_the_slot(
    identity: Any,
) -> None:
    with harness("async", identity, script([fakehoard.Hang()])) as (h, server):
        loop = h.loop
        assert loop is not None

        async def scenario() -> None:
            task = asyncio.ensure_future(h.runner.run(count_op(timeout=30.0)))
            for _ in range(500):
                if len(server.frames_with(opcodes.COUNT)) == 1:
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        loop.run_until_complete(scenario())
        stats = h.stats()
        assert stats.point_in_use == 0 and stats.point_idle == 0


def test_lanes_route_calls_to_their_own_connections(mode: str, identity: Any) -> None:
    with harness(mode, identity, script(COUNT_OK)) as (h, server):
        h.run(count_op(lane="point"))
        h.run(count_op(lane="bulk"))
        stats = h.stats()
        assert stats.point_idle == 1 and stats.bulk_idle == 1
        assert len(server.connections) == 2


def test_a_retry_that_cannot_start_reports_what_the_server_said_not_the_failed_dial(
    mode: str, identity: Any
) -> None:
    def greeting(index: int) -> Any:
        return [fakehoard.Hang()] if index >= 1 else None  # later dials never answer AUTH

    steps = script(err(6, "no holder"), COUNT_OK)
    with (
        harness(
            mode, identity, steps, greeting_fn=greeting, client_options={"dial_timeout": 0.3}
        ) as (
            h,
            _,
        ),
        pytest.raises(errors.UnavailableError) as caught,
    ):
        h.run(count_op())
    assert caught.value.message == "no holder"
    assert isinstance(caught.value.__cause__, errors.ConnectionFailedError)
