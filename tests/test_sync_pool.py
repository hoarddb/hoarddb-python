# SPDX-License-Identifier: Apache-2.0
"""P9: the blocking pool against the fake server: lanes, waiters, liveness, hosts, tokens."""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Iterator, Sequence
from typing import Any

import pytest

import fakehoard
import faketls
from hoarddb import errors
from hoarddb._core import config, opcodes
from hoarddb._core.messages import PingRequest
from hoarddb._deadline import Deadline, monotonic
from hoarddb._sync.pool import Pool
from hoarddb.options import PoolOptions, TlsOptions


@pytest.fixture(scope="module")
def identity(tmp_path_factory: pytest.TempPathFactory) -> faketls.Identity:
    return faketls.make_identity(tmp_path_factory.mktemp("id"), "pool")


def make_options(
    ports: Sequence[int], identity: faketls.Identity, **extra: Any
) -> config.ResolvedOptions:
    hosts = ",".join(f"127.0.0.1:{p}" for p in ports)
    tls = extra.pop("tls", TlsOptions(fingerprints=[identity.fingerprint]))
    return config.resolve_options(f"hoarddb://admin:pw@{hosts}", {"tls": tls, **extra}, {})


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def new_pool(
    server: fakehoard.FakeHoard,
    identity: faketls.Identity,
    pool: PoolOptions | None = None,
    **kw: Any,
) -> Pool:
    extra: dict[str, Any] = {"pool": pool} if pool is not None else {}
    return Pool(make_options([server.port], identity, **extra), **kw)


@pytest.fixture
def server(identity: faketls.Identity) -> Iterator[fakehoard.FakeHoard]:
    with fakehoard.FakeHoard(identity) as s:
        yield s


def checkout(pool: Pool, lane: str = "point", seconds: float | None = 5.0) -> Any:
    return pool.checkout(lane, Deadline(seconds), op="get", kind="READ")  # type: ignore[arg-type]


# --- lazy opening and reuse ----------------------------------------------------------------------


def test_creating_a_pool_opens_nothing_and_warm_opens_exactly_one(
    server: fakehoard.FakeHoard, identity: faketls.Identity
) -> None:
    pool = new_pool(server, identity)
    try:
        assert server.connections == []
        facts = pool.warm(Deadline(5.0))
        assert facts.version == "0.0.0-test" and "auth_token" in facts.capabilities
        assert facts.password_min_length == 12
        assert len(server.connections) == 1
        assert pool.stats().point_idle == 1
    finally:
        pool.close()


def test_a_hundred_sequential_calls_use_one_connection(
    server: fakehoard.FakeHoard, identity: faketls.Identity
) -> None:
    pool = new_pool(server, identity)
    try:
        for _ in range(100):
            lease = checkout(pool)
            pool.release(lease, reusable=True)
        assert len(server.connections) == 1
        stats = pool.stats()
        assert stats.checkouts == 100 and stats.dials == 1 and stats.point_in_use == 0
    finally:
        pool.close()


def test_the_lanes_are_separate_and_open_lazily(
    server: fakehoard.FakeHoard, identity: faketls.Identity
) -> None:
    pool = new_pool(server, identity, PoolOptions(size=1, bulk_size=1))
    try:
        point = checkout(pool, "point")
        bulk = checkout(pool, "bulk")  # a different lane is not starved by the point lane
        assert len(server.connections) == 2
        pool.release(point, reusable=True)
        pool.release(bulk, reusable=True)
    finally:
        pool.close()


# --- the bounded queue ---------------------------------------------------------------------------


def test_a_full_lane_makes_the_next_caller_wait_for_a_release(
    server: fakehoard.FakeHoard, identity: faketls.Identity
) -> None:
    pool = new_pool(server, identity, PoolOptions(size=1))
    try:
        first = checkout(pool)
        got: list[Any] = []
        t = threading.Thread(target=lambda: got.append(checkout(pool)))
        t.start()
        assert server.wait_for(lambda: pool.stats().waiting == 1)
        pool.release(first, reusable=True)
        t.join(5)
        assert got and got[0].conn is first.conn  # the same connection, handed on
        pool.release(got[0], reusable=True)
    finally:
        pool.close()


def test_a_wait_that_expires_is_a_pool_timeout_with_the_arithmetic(
    server: fakehoard.FakeHoard, identity: faketls.Identity
) -> None:
    pool = new_pool(server, identity, PoolOptions(size=1))
    try:
        held = checkout(pool)
        started = time.monotonic()
        with pytest.raises(errors.PoolTimeoutError) as caught:
            checkout(pool, seconds=0.2)
        assert 0.15 < time.monotonic() - started < 3
        text = str(caught.value)
        assert "0.2 s" in text and "1 in use" in text and "point" in text
        assert "PoolOptions" in text  # says how to fix it
        assert pool.stats().waiting == 0
        pool.release(held, reusable=True)
    finally:
        pool.close()


def test_a_full_queue_is_refused_at_once(
    server: fakehoard.FakeHoard, identity: faketls.Identity
) -> None:
    pool = new_pool(server, identity, PoolOptions(size=1, max_waiters=1))
    try:
        held = checkout(pool)
        waiter = threading.Thread(target=lambda: _expect_timeout(pool))
        waiter.start()
        assert server.wait_for(lambda: pool.stats().waiting == 1)
        started = time.monotonic()
        with pytest.raises(errors.PoolTimeoutError, match="max_waiters"):
            checkout(pool, seconds=30.0)
        assert time.monotonic() - started < 1.0  # refused, not queued
        pool.release(held, reusable=True)
        waiter.join(10)
    finally:
        pool.close()


def _expect_timeout(pool: Pool) -> None:
    try:
        lease = checkout(pool, seconds=3.0)
        pool.release(lease, reusable=True)
    except errors.PoolTimeoutError:
        pass


# --- liveness at checkout ------------------------------------------------------------------------


def test_an_idle_connection_the_server_closed_is_replaced_not_used(
    server: fakehoard.FakeHoard, identity: faketls.Identity
) -> None:
    pool = new_pool(server, identity)
    try:
        pool.release(checkout(pool), reusable=True)
        server.connections[0].sock.shutdown(socket.SHUT_RDWR)
        server.wait_for(lambda: server.connections[0].closed)
        time.sleep(0.05)
        lease = checkout(pool)
        assert len(server.connections) == 2
        assert pool.stats().discarded == 1
        pool.release(lease, reusable=True)
    finally:
        pool.close()


def test_a_connection_idle_past_the_limit_is_closed_instead_of_reused(
    server: fakehoard.FakeHoard, identity: faketls.Identity
) -> None:
    pool = new_pool(server, identity, PoolOptions(idle_timeout=0.1))
    try:
        pool.release(checkout(pool), reusable=True)
        time.sleep(0.2)
        pool.release(checkout(pool), reusable=True)
        assert len(server.connections) == 2
    finally:
        pool.close()


def test_a_stale_but_young_connection_gets_one_ping_before_use(
    server: fakehoard.FakeHoard, identity: faketls.Identity
) -> None:
    pool = new_pool(server, identity)
    try:
        lease = checkout(pool)
        lease.conn.last_used = monotonic() - 61.0
        pool.release(lease, reusable=True)
        again = checkout(pool)
        assert again.conn is lease.conn
        assert len(server.frames_with(opcodes.PING)) == 1
        pool.release(again, reusable=True)
        pool.release(checkout(pool), reusable=True)  # fresh again: no further ping
        assert len(server.frames_with(opcodes.PING)) == 1
    finally:
        pool.close()


def test_a_stale_connection_that_does_not_answer_the_ping_is_replaced(
    identity: faketls.Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("hoarddb._sync.pool.PING_DEADLINE", 0.2)
    hang: list[fakehoard.Action] = [fakehoard.Hang()]
    with fakehoard.FakeHoard(identity, answer_ping=False, handler=lambda c, f: hang) as server:
        pool = new_pool(server, identity)
        try:
            lease = checkout(pool)
            lease.conn.last_used = monotonic() - 61.0
            pool.release(lease, reusable=True)
            again = checkout(pool)
            assert again.conn is not lease.conn
            assert len(server.connections) == 2
            assert not lease.conn.healthy
            pool.release(again, reusable=True)
        finally:
            pool.close()


def test_a_connection_released_unreusable_is_closed(
    server: fakehoard.FakeHoard, identity: faketls.Identity
) -> None:
    pool = new_pool(server, identity)
    try:
        lease = checkout(pool)
        pool.release(lease, reusable=False)
        assert not lease.conn.healthy
        assert pool.stats().point_idle == 0 and pool.stats().point_in_use == 0
        pool.release(checkout(pool), reusable=True)
        assert len(server.connections) == 2
    finally:
        pool.close()


# --- hosts and dial backoff ----------------------------------------------------------------------


def _dead_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port: int = s.getsockname()[1]
    s.close()
    return port


def test_a_dead_first_host_fails_over_to_the_next_in_written_order(
    server: fakehoard.FakeHoard, identity: faketls.Identity
) -> None:
    options = make_options([_dead_port(), server.port], identity)
    pool = Pool(options)
    try:
        lease = checkout(pool)
        assert lease.conn.host.port == server.port
        pool.release(lease, reusable=True)
    finally:
        pool.close()


def test_one_pass_over_dead_hosts_fails_and_the_lane_backs_off_before_the_next_dial(
    identity: faketls.Identity,
) -> None:
    options = make_options([_dead_port(), _dead_port()], identity)
    sleeps = Sleeps()
    pool = Pool(options, sleep=sleeps, rng=lambda: 1.0)
    try:
        with pytest.raises(errors.ConnectionFailedError):
            checkout(pool)
        assert sleeps.calls == []  # the pass itself does not sleep, and is not repeated
        assert pool.stats().dial_failures == 1
        with pytest.raises(errors.ConnectionFailedError):
            checkout(pool)
        with pytest.raises(errors.ConnectionFailedError):
            checkout(pool)
        # the lane's delay before dials 2 and 3: 100 ms, then 200 ms (full jitter at 1.0)
        assert [round(x, 1) for x in sleeps.calls] == [0.1, 0.2]
        assert pool.stats().dial_failures == 3
    finally:
        pool.close()


def test_a_dial_that_works_resets_the_lane_backoff(
    server: fakehoard.FakeHoard, identity: faketls.Identity
) -> None:
    pool = new_pool(server, identity, rng=lambda: 1.0, sleep=Sleeps())
    try:
        pool.release(checkout(pool), reusable=True)
        assert pool._lanes["point"].failures == 0  # pyright: ignore[reportPrivateUsage]
    finally:
        pool.close()


def test_a_zero_id_overloaded_refusal_is_redialled_on_the_same_host_with_backoff(
    identity: faketls.Identity,
) -> None:
    refusal = fakehoard.Reply(opcodes.ERROR, {"code": 13, "message": "full"}, bytes(16))

    def greeting(index: int) -> Sequence[fakehoard.Action] | None:
        return [refusal] if index < 2 else None

    with fakehoard.FakeHoard(identity, greeting_fn=greeting) as server:
        sleeps = Sleeps()
        pool = new_pool(server, identity, sleep=sleeps, rng=lambda: 1.0)
        try:
            lease = checkout(pool)
            assert len(server.connections) == 3
            assert sleeps.calls == [0.1, 0.2]
            pool.release(lease, reusable=True)
        finally:
            pool.close()


# --- tokens --------------------------------------------------------------------------------------


def test_new_connections_present_the_token_not_the_password(
    server: fakehoard.FakeHoard, identity: faketls.Identity
) -> None:
    pool = new_pool(server, identity)
    try:
        a, b = checkout(pool), checkout(pool)
        first, second = (
            server.connections[0].auth_requests[0],
            server.connections[1].auth_requests[0],
        )
        assert first["password"] == "pw" and "token" not in first
        assert second["token"] == "tok-1" and "password" not in second
        pool.release(a, reusable=True)
        pool.release(b, reusable=True)
    finally:
        pool.close()


def test_a_refused_token_falls_back_to_the_password_once_on_a_new_connection(
    identity: faketls.Identity,
) -> None:
    def on_auth(conn: fakehoard.Conn, doc: dict[str, Any]) -> Sequence[fakehoard.Action]:
        if "token" in doc:
            return [fakehoard.auth_fail(3)]  # TOKEN_INVALID
        return [fakehoard.auth_ok()]

    with fakehoard.FakeHoard(identity, on_auth=on_auth) as server:
        sleeps = Sleeps()
        pool = new_pool(server, identity, sleep=sleeps, rng=lambda: 0.5)
        try:
            a, b = checkout(pool), checkout(pool)
            docs = [c.auth_requests[0] for c in server.connections]
            assert [("token" in d, "password" in d) for d in docs] == [
                (False, True),
                (True, False),
                (False, True),
            ]
            assert sleeps.calls == [0.5]  # staggered by the jitter
            pool.release(a, reusable=True)
            pool.release(b, reusable=True)
        finally:
            pool.close()


@pytest.mark.parametrize("reason", [1, 5, 6, 7])
def test_these_auth_failures_are_not_retried_in_a_loop(
    identity: faketls.Identity, reason: int
) -> None:
    with fakehoard.FakeHoard(
        identity, on_auth=lambda c, d: [fakehoard.auth_fail(reason)]
    ) as server:
        pool = new_pool(server, identity)
        try:
            with pytest.raises(errors.AuthenticationError):
                checkout(pool)
            server.wait_for(lambda: all(c.closed for c in server.connections))
            assert len(server.connections) == 1  # one attempt, one connection, no loop
        finally:
            pool.close()


def test_a_token_is_refreshed_on_a_held_connection_at_half_its_life(
    identity: faketls.Identity,
) -> None:
    offset = [0.0]
    answers = iter(
        [fakehoard.auth_ok("t1", expires_in=100), fakehoard.auth_ok("t2", expires_in=100)]
    )

    with fakehoard.FakeHoard(identity, on_auth=lambda c, d: [next(answers)]) as server:
        pool = new_pool(
            server,
            identity,
            PoolOptions(idle_timeout=10_000.0),
            clock=lambda: monotonic() + offset[0],
        )
        try:
            pool.release(checkout(pool), reusable=True)
            offset[0] = 40.0  # under half of 100 s, and under the 60 s stale limit
            pool.release(checkout(pool), reusable=True)
            assert len(server.connections[0].auth_requests) == 1
            offset[0] = 55.0  # past half
            lease = checkout(pool)
            docs = server.connections[0].auth_requests
            assert len(docs) == 2 and docs[1]["token"] == "t1" and "password" not in docs[1]
            assert len(server.connections) == 1  # refreshed in place
            pool.release(lease, reusable=True)
            pool.release(checkout(pool), reusable=True)
            assert len(server.connections[0].auth_requests) == 2  # single refresh, not a storm
            assert pool.tokens.token is not None and pool.tokens.token.reveal() == "t2"
        finally:
            pool.close()


# --- interrupts, close, fork, threads ------------------------------------------------------------


@pytest.mark.parametrize("point", ["after_slot", "while_dialling", "after_connection"])
def test_a_keyboard_interrupt_anywhere_in_checkout_leaves_every_slot_free(
    server: fakehoard.FakeHoard,
    identity: faketls.Identity,
    point: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = new_pool(server, identity, PoolOptions(size=2))
    try:
        real_take, real_for, real_drive = pool._take_slot, pool._connection_for, pool._drive  # pyright: ignore[reportPrivateUsage]

        def after_slot(*a: Any, **k: Any) -> None:
            real_take(*a, **k)
            raise KeyboardInterrupt

        def while_dialling(*a: Any, **k: Any) -> Any:
            raise KeyboardInterrupt

        def after_connection(*a: Any, **k: Any) -> Any:
            real_for(*a, **k)
            raise KeyboardInterrupt

        if point == "after_slot":
            monkeypatch.setattr(pool, "_take_slot", after_slot)
        elif point == "while_dialling":
            monkeypatch.setattr(pool, "_drive", while_dialling)
        else:
            monkeypatch.setattr(pool, "_connection_for", after_connection)
        for _ in range(3):  # more attempts than slots: a leak would exhaust the lane
            with pytest.raises(KeyboardInterrupt):
                checkout(pool)
        monkeypatch.undo()
        stats = pool.stats()
        assert stats.point_in_use == 0 and stats.bulk_in_use == 0
        lease = checkout(pool, seconds=2.0)  # the pool still works
        pool.release(lease, reusable=True)
        assert real_drive is not None
    finally:
        pool.close()
        server.wait_for(lambda: all(c.closed for c in server.connections))
        assert all(c.closed for c in server.connections)  # and no connection leaked


def test_close_is_idempotent_closes_idle_sockets_and_refuses_new_calls(
    server: fakehoard.FakeHoard, identity: faketls.Identity
) -> None:
    pool = new_pool(server, identity)
    pool.release(checkout(pool), reusable=True)
    pool.close()
    pool.close()
    assert server.wait_for(lambda: server.connections[0].closed)
    with pytest.raises(errors.ClientClosedError):
        checkout(pool)
    assert pool.stats().closed


def test_close_waits_for_an_in_flight_call_then_closes_its_socket(
    server: fakehoard.FakeHoard, identity: faketls.Identity
) -> None:
    pool = new_pool(server, identity)
    lease = checkout(pool)
    started = time.monotonic()
    pool.close(timeout=0.3)
    assert 0.25 < time.monotonic() - started < 3
    assert not lease.conn.healthy
    pool.release(lease, reusable=True)  # a late release after close is harmless
    assert pool.stats().point_in_use == 0


def test_close_wakes_a_waiter_with_client_closed(
    server: fakehoard.FakeHoard, identity: faketls.Identity
) -> None:
    pool = new_pool(server, identity, PoolOptions(size=1))
    held = checkout(pool)
    seen: list[BaseException] = []

    def waiter() -> None:
        try:
            checkout(pool, seconds=10.0)
        except errors.HoardDBError as exc:
            seen.append(exc)

    t = threading.Thread(target=waiter)
    t.start()
    assert server.wait_for(lambda: pool.stats().waiting == 1)
    pool.close(timeout=0.1)
    t.join(5)
    assert len(seen) == 1 and isinstance(seen[0], errors.ClientClosedError)
    pool.release(held, reusable=True)


def test_close_never_half_closes_a_busy_socket(
    server: fakehoard.FakeHoard, identity: faketls.Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(self: Any, how: int) -> None:
        raise AssertionError("shutdown() on a pooled socket")

    monkeypatch.setattr(socket.socket, "shutdown", refuse)
    pool = new_pool(server, identity)
    lease = checkout(pool)
    pool.close(timeout=0.1)
    pool.release(lease, reusable=True)


def test_a_forked_pool_refuses_calls_and_closing_it_leaves_the_parents_connection_alone(
    server: fakehoard.FakeHoard, identity: faketls.Identity
) -> None:
    pool = new_pool(server, identity)
    try:
        lease = checkout(pool)
        pool.release(lease, reusable=True)
        pid = os.fork()
        if pid == 0:
            code = 1
            try:
                pool.mark_forked()
                try:
                    checkout(pool)
                except errors.ForkedClientError:
                    code = 0
                pool.close()
            finally:
                os._exit(code)
        _, status = os.waitpid(pid, 0)
        assert os.WEXITSTATUS(status) == 0
        again = checkout(pool)  # the parent's pooled connection still answers
        assert again.conn is lease.conn
        again.conn.exchange(PingRequest(), _ctx(), Deadline(5.0))
        pool.release(again, reusable=True)
    finally:
        pool.close()


def _ctx() -> Any:
    from hoarddb._core.failure import Context

    return Context(operation="ping", kind="READ")


def test_the_pool_starts_no_thread(server: fakehoard.FakeHoard, identity: faketls.Identity) -> None:
    before = set(threading.enumerate())

    def ours() -> set[threading.Thread]:  # the fake server's own threads are not the driver's
        return set(threading.enumerate()) - before - set(server._threads)  # pyright: ignore[reportPrivateUsage]

    pool = new_pool(server, identity)
    lease = checkout(pool)
    assert ours() == set()
    pool.release(lease, reusable=True)
    pool.close()
    assert ours() == set()
