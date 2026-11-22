# SPDX-License-Identifier: Apache-2.0
"""The asyncio runner: the blocking runner's logic, awaiting (spec 3.3, 8).

``asyncio.CancelledError`` is never swallowed: the exchange has already aborted the transport (the
server cannot be told to stop), the slot is given back in the ``finally``, and the cancellation
continues. For a write that was sent, the connection logged the one WARNING about its outcome.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any, TypeVar

from hoarddb import errors
from hoarddb._aio.pool import Pool
from hoarddb._core import retry
from hoarddb._core.call import Call, Op, Reply
from hoarddb._core.failure import Context
from hoarddb._core.interrupt import Interrupted, interrupted
from hoarddb._deadline import Deadline

T = TypeVar("T")


async def _real_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class Runner:
    def __init__(
        self,
        pool: Pool,
        request_timeout: float,
        *,
        sleep: Callable[[float], Awaitable[Any]] = _real_sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._pool = pool
        self._request_timeout = request_timeout
        self._sleep = sleep
        self._rng = rng

    async def run(self, op: Op[T]) -> T:
        """Drive ``op`` to its return value."""
        try:
            try:
                call = next(op)
            except StopIteration as done:
                return done.value  # type: ignore[no-any-return]
            while True:
                try:
                    reply = await self.execute(call)
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

    async def execute(self, call: Call) -> Reply:
        """One call, with the deadline and the retry table. Raises the driver's own errors."""
        deadline = Deadline(call.timeout if call.timeout is not None else self._request_timeout)
        ctx = Context(operation=call.op, target=call.target, kind=call.kind)
        state = retry.RetryState()
        last_refusal: errors.HoardDBError | None = None
        while True:
            state = replace(state, has_token=self._pool.tokens.token is not None)
            try:
                lease = await self._pool.checkout(call.lane, deadline, op=call.op, kind=call.kind)
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
                    reply = await lease.conn.exchange(call.request, ctx, deadline)
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
                            "error_frame", code=err.wire_code, write_outcome=err.write_outcome
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
                await self._sleep(decision.delay)
