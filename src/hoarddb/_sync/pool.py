# SPDX-License-Identifier: Apache-2.0
"""The blocking pool: two lanes, lazily opened, a bounded queue of waiters (spec 6).

A ``threading.Condition`` over a set of slot holders and a deque of idle connections per lane. No
thread and no timer is ever started: liveness work (the poll probe, the idle limit, the stale
``PING``, the token refresh) happens at checkout. A slot is owned by a ``Claim`` object that is
added to the lane's holder set in one operation, so an asynchronous exception (a
``KeyboardInterrupt`` lands between any two bytecodes) cannot leak a slot: whatever ``checkout``
raises, its ``finally`` hands back exactly the slots the set says it holds.
"""

from __future__ import annotations

import os
import random
import threading
import time
from collections.abc import Callable

from hoarddb import errors
from hoarddb._core import diagnostics, dialplan, handshake, messages, retry
from hoarddb._core.call import Lane
from hoarddb._core.config import ResolvedOptions
from hoarddb._core.failure import Context
from hoarddb._core.interrupt import Interrupted
from hoarddb._core.kinds import Kind
from hoarddb._core.messages import AuthOkResponse
from hoarddb._core.token import Credential, Rejection, TokenManager
from hoarddb._deadline import Deadline, monotonic
from hoarddb._poolstate import (
    CLOSE_WAIT,
    PING_DEADLINE,
    STALE_AFTER,
    Claim,
    LaneState,
    ServerFacts,
)
from hoarddb._secret import Secret
from hoarddb._sync.connection import Connection
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


class _Tokens:
    """The token manager behind a lock: the manager itself holds none (token.py)."""

    def __init__(self, manager: TokenManager, lock: threading.Lock) -> None:
        self._m = manager
        self._lock = lock

    def credential(self, now_mono: float) -> Credential:
        with self._lock:
            return self._m.credential(now_mono)

    @property
    def token(self) -> Secret | None:
        with self._lock:
            return self._m.token

    def rejected(self, reason: str, *, jitter: float) -> Rejection:
        with self._lock:
            return self._m.rejected(reason, jitter=jitter)

    def adopt(self, ok: AuthOkResponse, *, now_mono: float, now_wall: float) -> None:
        with self._lock:
            self._m.adopt(ok, now_mono=now_mono, now_wall=now_wall)

    def refresh_due(self, now_mono: float) -> bool:
        with self._lock:
            return self._m.refresh_due(now_mono)

    def begin_refresh(self) -> bool:
        with self._lock:
            return self._m.begin_refresh()

    def end_refresh(self) -> None:
        with self._lock:
            self._m.end_refresh()


class Pool:
    def __init__(
        self,
        options: ResolvedOptions,
        *,
        clock: Callable[[], float] = monotonic,
        wall: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._options = options
        self._clock, self._wall, self._sleep, self._rng = clock, wall, sleep, rng
        self._pid = os.getpid()
        self._cond = threading.Condition(threading.Lock())
        self._lanes: dict[Lane, _State] = {
            "point": _State("point", options.pool.size),
            "bulk": _State("bulk", options.pool.bulk_size),
        }
        manager = TokenManager(have_password=options.password is not None)
        if options.auth_token is not None:
            manager.supplied(options.auth_token)
        self._tokens = _Tokens(manager, threading.Lock())
        self._closed = False
        self._forked = False
        self._counters = {"dials": 0, "dial_failures": 0, "discarded": 0, "checkouts": 0}
        self.server: ServerFacts | None = None

    # -- the surface the runner and the client use ---------------------------------------------

    @property
    def tokens(self) -> _Tokens:
        return self._tokens

    @property
    def options(self) -> ResolvedOptions:
        return self._options

    def checkout(self, lane_name: Lane, deadline: Deadline, *, op: str, kind: Kind) -> Lease:
        """A connection from the lane: an idle one that passes the liveness checks, else a new one
        (one pass over the hosts). Waits for a slot at most until the call's deadline."""
        self._check_pid()
        lane = self._lanes[lane_name]
        claim = _Slot()
        try:
            self._take_slot(lane, claim, deadline)
            conn = self._connection_for(lane, claim, deadline, op, kind)
            with self._cond:
                self._counters["checkouts"] += 1
            return Lease(conn, claim, lane)
        except BaseException:
            self._abandon(lane, claim)
            raise

    def release(self, lease: Lease, *, reusable: bool) -> None:
        """Give a checked-out connection back: it is pooled again only if the exchange left it
        healthy and ``reusable`` says so; otherwise it is closed. The slot is always freed."""
        conn, lane = lease.conn, lease.state
        pooled = False
        try:
            with self._cond:
                lane.holders.discard(lease.claim)
                if reusable and conn.healthy and not self._closed and not self._forked:
                    lane.idle.append(conn)
                    pooled = True
                self._cond.notify()
        finally:
            if not pooled:
                self._close(conn)

    def stats(self) -> PoolStats:
        with self._cond:
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

    def warm(self, deadline: Deadline) -> ServerFacts:
        """Open the first connection (it must ``AUTH`` to fail fast and learn the capabilities)."""
        lease = self.checkout("point", deadline, op="connect", kind="READ")
        self.release(lease, reusable=True)
        assert self.server is not None
        return self.server

    def mark_forked(self) -> None:
        """After ``os.fork()`` in the child: the connections are the parent's. From now on this
        pool only drops descriptors (never ``shutdown``, never a write)."""
        self._forked = True

    def close(self, timeout: float = CLOSE_WAIT) -> None:
        """Idempotent. Waits for in-flight calls up to ``timeout`` seconds, then closes every
        socket. A busy socket is closed, never half-closed with ``shutdown``."""
        with self._cond:
            if self._closed:
                return
            self._closed = True
            self._cond.notify_all()
            if not self._forked:
                end = self._clock() + timeout
                while any(lane.holders for lane in self._lanes.values()):
                    left = end - self._clock()
                    if left <= 0:
                        break
                    self._cond.wait(left)
            victims: list[Connection] = []
            for lane in self._lanes.values():
                victims.extend(lane.idle)
                lane.idle.clear()
                victims.extend(c.conn for c in lane.holders if c.conn is not None)
        for conn in victims:
            self._close(conn)

    # -- internals -----------------------------------------------------------------------------

    def _check_pid(self) -> None:
        if os.getpid() != self._pid:
            raise diagnostics.forked_client(self._pid, os.getpid())

    def _close(self, conn: Connection) -> None:
        with self._cond:
            self._counters["discarded"] += 1
        conn.close(forked=self._forked)

    def _abandon(self, lane: _State, claim: _Slot) -> None:
        """A checkout failed or was interrupted: close what it held and free its slot."""
        try:
            if claim.conn is not None:
                self._close(claim.conn)
        finally:
            with self._cond:
                lane.holders.discard(claim)
                self._cond.notify()

    def _take_slot(self, lane: _State, claim: _Slot, deadline: Deadline) -> None:
        with self._cond:
            if self._closed:
                raise diagnostics.client_closed()
            if len(lane.holders) < lane.size:
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
                        raise diagnostics.pool_timeout(
                            deadline.seconds or 0.0, len(lane.holders), lane.waiting, lane.name
                        )
                    self._cond.wait(left)
                    if self._closed:
                        raise diagnostics.client_closed()
                    if len(lane.holders) < lane.size:
                        lane.holders.add(claim)
                        return
            finally:
                lane.waiting -= 1

    def _connection_for(
        self,
        lane: _State,
        claim: _Slot,
        deadline: Deadline,
        op: str,
        kind: Kind,
    ) -> Connection:
        while True:
            with self._cond:
                if self._closed:
                    raise diagnostics.client_closed()
                conn = lane.idle.pop() if lane.idle else None  # most recently used first
            if conn is None:
                break
            claim.conn = conn
            if self._usable(conn, deadline) and self._refreshed(conn, deadline):
                return conn
            claim.conn = None  # closed by the check that refused it
        return self._dial(lane, claim, deadline, op, kind)

    def _usable(self, conn: Connection, deadline: Deadline) -> bool:
        """The liveness work at checkout (spec 6.2): idle limit, poll probe, one PING if stale."""
        idle_for = self._clock() - conn.last_used
        if idle_for > self._options.pool.idle_timeout or not conn.probe():
            self._close(conn)
            return False
        if idle_for >= STALE_AFTER:
            try:
                conn.exchange(
                    messages.PingRequest(),
                    Context(operation="ping", kind="READ"),
                    Deadline(deadline.cap(PING_DEADLINE)),
                )
            except (Interrupted, errors.HoardDBError):
                self._close(conn)
                return False
        return True

    def _refreshed(self, conn: Connection, deadline: Deadline) -> bool:
        """Refresh the token on this connection when half its life has passed (single flight)."""
        tokens = self._tokens
        if not tokens.refresh_due(self._clock()) or not tokens.begin_refresh():
            return True
        try:
            auth = handshake.auth_request(self._options, "token", tokens.token)
            ok = conn.authenticate_again(
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
                self._sleep(deadline.cap(outcome.delay))
            return False  # the next dial presents the password
        except (Interrupted, errors.ConnectionFailedError):
            self._close(conn)
            return False
        finally:
            tokens.end_refresh()

    def _dial(
        self,
        lane: _State,
        claim: _Slot,
        deadline: Deadline,
        op: str,
        kind: Kind,
    ) -> Connection:
        """One new connection: the lane's backoff, then the dial plan over the host list."""
        self._lane_backoff(lane, deadline, op, kind)
        plan = dialplan.plan(
            self._options,
            self._tokens,
            kind=kind,
            remaining=deadline.remaining,
            rng=self._rng,
            mono=self._clock,
            wall=self._wall,
        )
        with self._cond:
            self._counters["dials"] += 1
        try:
            conn, ok = self._drive(plan, deadline)
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
        with self._cond:
            lane.failures = 0
            lane.not_before = 0.0
        self.server = ServerFacts(ok.server_version, tuple(ok.capabilities), ok.password_min_length)
        return conn

    def _drive(
        self, plan: dialplan.DialPlan, deadline: Deadline
    ) -> tuple[Connection, AuthOkResponse]:
        """Run the plan's effects: a ``Sleep`` sleeps; an ``Open`` dials."""
        options = self._options
        try:
            effect = next(plan)
            while True:
                if isinstance(effect, dialplan.Sleep):
                    self._sleep(effect.seconds)
                    effect = plan.send(None)
                    continue
                auth = handshake.auth_request(options, effect.credential, effect.token)
                try:
                    opened = Connection.open(effect.host, effect.pin, options, auth, deadline)
                except errors.HoardDBError as failure:
                    effect = plan.throw(failure)
                else:
                    effect = plan.send(opened)
        except StopIteration as done:
            result: tuple[Connection, AuthOkResponse] = done.value
            return result

    def _dial_failed(self, lane: _State) -> None:
        with self._cond:
            self._counters["dial_failures"] += 1
            lane.failures += 1
            lane.not_before = self._clock() + retry.backoff_delay(lane.failures, self._rng())

    def _lane_backoff(self, lane: _State, deadline: Deadline, op: str, kind: Kind) -> None:
        """After a failed dial the lane waits (jittered, 100 ms doubling to 5 s) before the next;
        that wait is bounded by the call's deadline and is not a retry of the call."""
        with self._cond:
            wait = lane.not_before - self._clock()
        if wait <= 0:
            return
        left = deadline.remaining()
        if left is not None and wait >= left:
            raise diagnostics.request_timeout(
                op, None, deadline.seconds or 0.0, kind, sent=False
            ) from None
        self._sleep(wait)
