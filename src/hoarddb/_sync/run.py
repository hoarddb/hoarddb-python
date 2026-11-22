# SPDX-License-Identifier: Apache-2.0
"""The blocking runner: drives an operation's generator and applies the retry table (spec 3.3, 8).

For each ``Call`` an operation yields, :meth:`Runner.execute` checks a connection out of the pool,
exchanges one frame, and asks :func:`hoarddb._core.retry.decide` what a failure means. The pool
slot is given back in a ``finally`` and a connection is reused only when the exchange completed.
A refusal is *thrown into* the generator, so an operation that can recover (a cursor halving its
page) does so in one place, shared with the asyncio runner.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import replace
from typing import TypeVar

from hoarddb import errors
from hoarddb._core import retry
from hoarddb._core.call import Call, Op, Reply
from hoarddb._core.failure import Context
from hoarddb._core.interrupt import Interrupted, interrupted
from hoarddb._deadline import Deadline
from hoarddb._sync.pool import Pool

T = TypeVar("T")


class Runner:
    def __init__(
        self,
        pool: Pool,
        request_timeout: float,
        *,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._pool = pool
        self._request_timeout = request_timeout
        self._sleep = sleep
        self._rng = rng

    def run(self, op: Op[T]) -> T:
        """Drive ``op`` to its return value."""
        try:
            try:
                call = next(op)
            except StopIteration as done:
                return done.value  # type: ignore[no-any-return]
            while True:
                try:
                    reply = self.execute(call)
                except errors.HoardDBError as failure:
                    try:
                        call = op.throw(failure)
                    except StopIteration as done:
                        return done.value  # type: ignore[no-any-return]
                    continue
                try:
                    call = op.send(reply)
                except StopIteration as done:
                    return done.value  # type: ignore[no-any-return]
        finally:
            op.close()

    def execute(self, call: Call) -> Reply:
        """One call, with the deadline and the retry table. Raises the driver's own errors."""
        deadline = Deadline(call.timeout if call.timeout is not None else self._request_timeout)
        ctx = Context(operation=call.op, target=call.target, kind=call.kind)
        state = retry.RetryState()
        last_refusal: errors.HoardDBError | None = None
        while True:
            state = replace(state, has_token=self._pool.tokens.token is not None)
            try:
                lease = self._pool.checkout(call.lane, deadline, op=call.op, kind=call.kind)
            except (
                errors.ConnectionFailedError,
                errors.RequestTimeoutError,
                errors.PoolTimeoutError,
            ) as unlucky:
                if last_refusal is not None:
                    # The server answered, we retried, and the retry could not even start in time:
                    # what the caller needs to know is what the server said.
                    raise last_refusal from unlucky
                raise
            completed = False
            try:
                try:
                    reply = lease.conn.exchange(call.request, ctx, deadline)
                    completed = True
                    return reply
                except Interrupted as lost:
                    failure, error = interrupted(
                        call, lease.conn.host, lost, deadline.seconds or 0.0
                    )
                    if failure is None:  # a timeout: the deadline is spent, nothing to retry
                        raise error from None
                    decision = retry.decide(
                        call.kind, failure, state, deadline.remaining(), self._rng()
                    )
                    if decision.action is retry.Action.RAISE:
                        raise error from None
                    state = decision.state
                except errors.HoardDBError as err:
                    completed = lease.conn.healthy
                    if err.wire_code is None:
                        raise
                    decision = retry.decide(
                        call.kind,
                        retry.Failure(
                            "error_frame",
                            code=err.wire_code,
                            write_outcome=err.write_outcome,
                        ),
                        state,
                        deadline.remaining(),
                        self._rng(),
                    )
                    if decision.action is retry.Action.RAISE:
                        raise
                    state = decision.state
                    last_refusal = err
            finally:
                self._pool.release(lease, reusable=completed)
            if decision.action is retry.Action.RETRY_AFTER:
                self._sleep(decision.delay)
            # RETRY_NEW_CONNECTION and REAUTH_THEN_RESEND: the next checkout dials, and a new
            # connection authenticates with the token (or the password) itself.
