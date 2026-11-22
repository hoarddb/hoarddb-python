# SPDX-License-Identifier: Apache-2.0
"""The asyncio pool: the same two lanes and the same rules as the blocking pool (spec 6).

It is bound to the event loop that created it and starts no task. Every state change is made with
no ``await`` in the middle, so on the single-threaded loop it needs no lock, and the cleanup that
frees a slot after an error or a cancellation is synchronous: a second ``cancel()`` cannot
interrupt it. Waiters are futures in a queue, not an ``asyncio.Condition``, for the same reason.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from hoarddb import errors
from hoarddb._aio.connection import Connection
from hoarddb._core import diagnostics, dialplan, handshake, messages, retry
from hoarddb._core.call import Lane
from hoarddb._core.config import ResolvedOptions
from hoarddb._core.failure import Context
from hoarddb._core.interrupt import Interrupted
from hoarddb._core.kinds import Kind
from hoarddb._core.messages import AuthOkResponse
from hoarddb._core.token import TokenManager
from hoarddb._deadline import Deadline, monotonic
from hoarddb._poolstate import (
    CLOSE_WAIT,
    PING_DEADLINE,
    STALE_AFTER,
    Claim,
    LaneState,
    ServerFacts,
)
from hoarddb.types import PoolStats

_Slot = Claim[Connection]
_State = LaneState[Connection]


class Lease:
    """A checked-out connection and the slot it occupies; give it back with ``Pool.release``."""

    __slots__ = ("claim", "conn", "lane", "state")

    def __init__(self, conn: Connection, claim: _Slot, state: _State) -> None:
        self.conn = conn
        self.claim = claim
        self.state = state
        self.lane: Lane = state.name


async def _real_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class Pool:
    def __init__(
        self,
        options: ResolvedOptions,
        *,
        clock: Callable[[], float] = monotonic,
        wall: Callable[[], float] = time.time,
        sleep: Callable[[float], Any] = _real_sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._loop = asyncio.get_running_loop()
        self._options = options
        self._clock, self._wall, self._sleep, self._rng = clock, wall, sleep, rng
        self._pid = os.getpid()
        self._lanes: dict[Lane, _State] = {
            "point": LaneState("point", options.pool.size),
            "bulk": LaneState("bulk", options.pool.bulk_size),
        }
        self._waiters: dict[Lane, deque[asyncio.Future[None]]] = {
            "point": deque(),
            "bulk": deque(),
        }
        self._tokens = TokenManager(have_password=options.password is not None)
        if options.auth_token is not None:
            self._tokens.supplied(options.auth_token)
        self._closed = False
        self._forked = False
        self._counters = {"dials": 0, "dial_failures": 0, "discarded": 0, "checkouts": 0}
        self.server: ServerFacts | None = None

    # -- the surface the runner and the client use ---------------------------------------------

    @property
    def tokens(self) -> TokenManager:
        return self._tokens

    @property
    def options(self) -> ResolvedOptions:
        return self._options

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._loop

    async def checkout(self, lane_name: Lane, deadline: Deadline, *, op: str, kind: Kind) -> Lease:
        """A connection from the lane: an idle one that passes the liveness checks, else a new one
        (one pass over the hosts). Waits for a slot at most until the call's deadline."""
        self._check_pid()
        self._check_loop()
        lane = self._lanes[lane_name]
        claim: _Slot = Claim()
        try:
            await self._take_slot(lane, claim, deadline)
            conn = await self._connection_for(lane, claim, deadline, op, kind)
            self._counters["checkouts"] += 1
            return Lease(conn, claim, lane)
        except BaseException:
            self._abandon(lane, claim)
            raise

    def release(self, lease: Lease, *, reusable: bool) -> None:
        """Give a checked-out connection back (synchronous, so a cancelled caller can always do
        it): pooled again only if healthy and ``reusable``; otherwise closed. The slot is freed."""
        conn, lane = lease.conn, lease.state
        lane.holders.discard(lease.claim)
        if reusable and conn.healthy and not self._closed and not self._forked:
            lane.idle.append(conn)
        else:
            self._close(conn)
        self._wake(lane)

    def stats(self) -> PoolStats:
        point, bulk = self._lanes["point"], self._lanes["bulk"]
        return PoolStats(
            point_size=point.size,
            bulk_size=bulk.size,
            point_in_use=len(point.holders),
            bulk_in_use=len(bulk.holders),
            point_idle=len(point.idle),
            bulk_idle=len(bulk.idle),
            waiting=point.waiting + bulk.waiting,
            dials=self._counters["dials"],
            dial_failures=self._counters["dial_failures"],
            discarded=self._counters["discarded"],
            checkouts=self._counters["checkouts"],
            closed=self._closed,
        )

    async def warm(self, deadline: Deadline) -> ServerFacts:
        """Open the first connection (it must ``AUTH`` to fail fast and learn the capabilities)."""
        lease = await self.checkout("point", deadline, op="connect", kind="READ")
        self.release(lease, reusable=True)
        assert self.server is not None
        return self.server

    def mark_forked(self) -> None:
        """After ``os.fork()`` in the child: the connections are the parent's. From now on this
        pool only drops descriptors (never ``shutdown``, never a write)."""
        self._forked = True

    async def close(self, timeout: float = CLOSE_WAIT) -> None:
        """Idempotent. Waits for in-flight calls up to ``timeout`` seconds, then aborts every
        transport. Waiters are woken with ``ClientClosedError``."""
        if self._closed:
            return
        self._closed = True
        for lane in self._lanes.values():
            self._wake_all(lane)
        if not self._forked:
            end = self._clock() + timeout
            while any(lane.holders for lane in self._lanes.values()) and self._clock() < end:
                await asyncio.sleep(0.01)
        self.close_now()

    def close_now(self) -> None:
        """Abort every transport at once, without waiting (synchronous: a finalizer may call it)."""
        self._closed = True
        victims: list[Connection] = []
        for lane in self._lanes.values():
            victims.extend(lane.idle)
            lane.idle.clear()
            victims.extend(c.conn for c in lane.holders if c.conn is not None)
            self._wake_all(lane)
        for conn in victims:
            self._close(conn)

    # -- internals -----------------------------------------------------------------------------

    def _check_pid(self) -> None:
        if os.getpid() != self._pid:
            raise diagnostics.forked_client(self._pid, os.getpid())

    def loop_check(self) -> None:
        """Refuse a call from another event loop (the client is bound to the loop that made it)."""
        self._check_loop()

    def _check_loop(self) -> None:
        running = asyncio.get_running_loop()
        if running is not self._loop:
            raise errors.ConfigError(
                f"This client was created on the event loop {self._loop!r} and is being used "
                f"from {running!r}. Create the client inside the loop that uses it.",
                code="WRONG_EVENT_LOOP",
                operation="call",
            )

    def _close(self, conn: Connection) -> None:
        self._counters["discarded"] += 1
        conn.close(forked=self._forked)

    def _abandon(self, lane: _State, claim: _Slot) -> None:
        """A checkout failed or was cancelled: close what it held and free its slot."""
        try:
            if claim.conn is not None:
                self._close(claim.conn)
        finally:
            lane.holders.discard(claim)
            self._wake(lane)

    def _wake(self, lane: _State) -> None:
        queue = self._waiters[lane.name]
        while queue:
            waiter = queue.popleft()
            if not waiter.done():
                waiter.set_result(None)
                return

    def _wake_all(self, lane: _State) -> None:
        queue = self._waiters[lane.name]
        while queue:
            waiter = queue.popleft()
            if not waiter.done():
                waiter.set_result(None)

    async def _take_slot(self, lane: _State, claim: _Slot, deadline: Deadline) -> None:
        if self._closed:
            raise diagnostics.client_closed()
        queue = self._waiters[lane.name]
        if len(lane.holders) < lane.size and not queue:
            lane.holders.add(claim)
            return
        if lane.waiting >= self._options.pool.max_waiters:
            raise diagnostics.pool_queue_full(
                self._options.pool.max_waiters, len(lane.holders), lane.name
            )
        lane.waiting += 1
        try:
            while True:
                left = deadline.remaining()
                if left is not None and left <= 0:
                    raise self._pool_timeout(lane, deadline)
                waiter: asyncio.Future[None] = self._loop.create_future()
                queue.append(waiter)
                try:
                    async with asyncio.timeout(left):
                        await waiter
                except TimeoutError:
                    raise self._pool_timeout(lane, deadline) from None
                except BaseException:
                    if waiter.done() and not waiter.cancelled():
                        self._wake(lane)  # we were woken but are leaving: pass the slot on
                    raise
                finally:
                    if waiter in queue:
                        queue.remove(waiter)
                if self._closed:
                    raise diagnostics.client_closed()
                if len(lane.holders) < lane.size:
                    lane.holders.add(claim)
                    return
        finally:
            lane.waiting -= 1

    def _pool_timeout(self, lane: _State, deadline: Deadline) -> errors.PoolTimeoutError:
        return diagnostics.pool_timeout(
            deadline.seconds or 0.0, len(lane.holders), lane.waiting, lane.name
        )

    async def _connection_for(
        self, lane: _State, claim: _Slot, deadline: Deadline, op: str, kind: Kind
    ) -> Connection:
        while True:
            if self._closed:
                raise diagnostics.client_closed()
            conn = lane.idle.pop() if lane.idle else None  # most recently used first
            if conn is None:
                break
            claim.conn = conn
            if await self._usable(conn, deadline) and await self._refreshed(conn, deadline):
                return conn
            claim.conn = None  # closed by the check that refused it
        return await self._dial(lane, claim, deadline, op, kind)

    async def _usable(self, conn: Connection, deadline: Deadline) -> bool:
        """The liveness work at checkout (spec 6.2): idle limit, the protocol's flags, one PING
        if stale."""
        idle_for = self._clock() - conn.last_used
        if idle_for > self._options.pool.idle_timeout or not conn.probe():
            self._close(conn)
            return False
        if idle_for >= STALE_AFTER:
            try:
                await conn.exchange(
                    messages.PingRequest(),
                    Context(operation="ping", kind="READ"),
                    Deadline(deadline.cap(PING_DEADLINE)),
                )
            except (Interrupted, errors.HoardDBError):
                self._close(conn)
                return False
        return True

    async def _refreshed(self, conn: Connection, deadline: Deadline) -> bool:
        """Refresh the token on this connection when half its life has passed (single flight)."""
        tokens = self._tokens
        if not tokens.refresh_due(self._clock()) or not tokens.begin_refresh():
            return True
        try:
            auth = handshake.auth_request(self._options, "token", tokens.token)
            ok = await conn.authenticate_again(
                auth, deadline.within(self._options.dial_timeout), self._options.username
            )
            tokens.adopt(ok, now_mono=self._clock(), now_wall=self._wall())
            return True
        except errors.AuthenticationError as bad:
            self._close(conn)
            outcome = tokens.rejected(bad.reason, jitter=self._rng())
            if bad.reason not in ("TOKEN_EXPIRED", "TOKEN_INVALID", "TOKEN_UNSUPPORTED"):
                raise
            if outcome.delay > 0:
                await self._sleep(deadline.cap(outcome.delay))
            return False  # the next dial presents the password
        except (Interrupted, errors.ConnectionFailedError):
            self._close(conn)
            return False
        finally:
            tokens.end_refresh()

    async def _dial(
        self, lane: _State, claim: _Slot, deadline: Deadline, op: str, kind: Kind
    ) -> Connection:
        """One new connection: the lane's backoff, then the dial plan over the host list."""
        await self._lane_backoff(lane, deadline, op, kind)
        plan = dialplan.plan(
            self._options,
            self._tokens,
            kind=kind,
            remaining=deadline.remaining,
            rng=self._rng,
            mono=self._clock,
            wall=self._wall,
        )
        self._counters["dials"] += 1
        try:
            conn, ok = await self._drive(plan, deadline)
        except Interrupted:
            raise diagnostics.request_timeout(
                op, None, deadline.seconds or 0.0, kind, sent=False
            ) from None
        except errors.HoardDBError as failure:
            if isinstance(failure, errors.ConnectionFailedError | errors.OverloadedError):
                self._dial_failed(lane)
            raise
        finally:
            plan.close()
        claim.conn = conn
        lane.failures = 0
        lane.not_before = 0.0
        self.server = ServerFacts(ok.server_version, tuple(ok.capabilities), ok.password_min_length)
        return conn

    async def _drive(
        self, plan: dialplan.DialPlan, deadline: Deadline
    ) -> tuple[Connection, AuthOkResponse]:
        """Run the plan's effects: a ``Sleep`` sleeps; an ``Open`` dials."""
        options = self._options
        try:
            effect = next(plan)
            while True:
                if isinstance(effect, dialplan.Sleep):
                    await self._sleep(effect.seconds)
                    effect = plan.send(None)
                    continue
                auth = handshake.auth_request(options, effect.credential, effect.token)
                try:
                    opened = await Connection.open(effect.host, effect.pin, options, auth, deadline)
                except errors.HoardDBError as failure:
                    effect = plan.throw(failure)
                else:
                    effect = plan.send(opened)
        except StopIteration as done:
            result: tuple[Connection, AuthOkResponse] = done.value
            return result

    def _dial_failed(self, lane: _State) -> None:
        self._counters["dial_failures"] += 1
        lane.failures += 1
        lane.not_before = self._clock() + retry.backoff_delay(lane.failures, self._rng())

    async def _lane_backoff(self, lane: _State, deadline: Deadline, op: str, kind: Kind) -> None:
        """After a failed dial the lane waits (jittered, 100 ms doubling to 5 s) before the next;
        that wait is bounded by the call's deadline and is not a retry of the call."""
        wait = lane.not_before - self._clock()
        if wait <= 0:
            return
        left = deadline.remaining()
        if left is not None and wait >= left:
            raise diagnostics.request_timeout(op, None, deadline.seconds or 0.0, kind, sent=False)
        await self._sleep(wait)
