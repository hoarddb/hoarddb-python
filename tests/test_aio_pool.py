# SPDX-License-Identifier: Apache-2.0
"""P9: the asyncio pool against the fake server: the same rules as the blocking pool's tests."""

from __future__ import annotations

import asyncio
import os
import socket
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, TypeVar

import pytest

import fakehoard
import faketls
from hoarddb import errors
from hoarddb._aio.pool import Pool
from hoarddb._core import config, opcodes
from hoarddb._core.failure import Context
from hoarddb._core.messages import PingRequest
from hoarddb._deadline import Deadline, monotonic
from hoarddb.options import PoolOptions, TlsOptions

T = TypeVar("T")


def run(coro: Callable[[], Awaitable[T]]) -> T:
    async def main() -> T:
        before = {t for t in asyncio.all_tasks() if not t.done()}
        result = await coro()
        await asyncio.sleep(0)
        leaked = {t for t in asyncio.all_tasks() if not t.done()} - before
        current = asyncio.current_task()
        leaked = {t for t in leaked if t is not current}
        assert not leaked, f"tasks outlived the pool: {leaked}"
        return result

    return asyncio.run(main())


@pytest.fixture(scope="module")
def identity(tmp_path_factory: pytest.TempPathFactory) -> faketls.Identity:
    return faketls.make_identity(tmp_path_factory.mktemp("id"), "pool")


def make_options(
    ports: Sequence[int], identity: faketls.Identity, **extra: Any
) -> config.ResolvedOptions:
    hosts = ",".join(f"127.0.0.1:{p}" for p in ports)
    tls = extra.pop("tls", TlsOptions(fingerprints=[identity.fingerprint]))
    return config.resolve_options(f"hoarddb://admin:pw@{hosts}", {"tls": tls, **extra}, {})


def new_pool(
    server: fakehoard.FakeHoard,
    identity: faketls.Identity,
    pool: PoolOptions | None = None,
    **kw: Any,
) -> Pool:
    extra: dict[str, Any] = {"pool": pool} if pool is not None else {}
    return Pool(make_options([server.port], identity, **extra), **kw)


async def checkout(pool: Pool, lane: str = "point", seconds: float | None = 5.0) -> Any:
    return await pool.checkout(lane, Deadline(seconds), op="get", kind="READ")  # type: ignore[arg-type]


async def until(predicate: Callable[[], bool], tries: int = 500) -> bool:
    for _ in range(tries):
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


def sleeper() -> tuple[list[float], Callable[[float], Awaitable[None]]]:
    calls: list[float] = []

    async def sleep(seconds: float) -> None:
        calls.append(seconds)

    return calls, sleep


# --- lazy opening and reuse ----------------------------------------------------------------------


def test_creating_a_pool_opens_nothing_and_warm_opens_exactly_one(
    identity: faketls.Identity,
) -> None:
    async def main() -> None:
        with fakehoard.FakeHoard(identity) as server:
            pool = new_pool(server, identity)
            assert server.connections == []
            facts = await pool.warm(Deadline(5.0))
            assert facts.version == "0.0.0-test" and "auth_token" in facts.capabilities
            assert len(server.connections) == 1 and pool.stats().point_idle == 1
            await pool.close()

    run(main)


def test_a_hundred_sequential_calls_use_one_connection(identity: faketls.Identity) -> None:
    async def main() -> None:
        with fakehoard.FakeHoard(identity) as server:
            pool = new_pool(server, identity)
            for _ in range(100):
                pool.release(await checkout(pool), reusable=True)
            assert len(server.connections) == 1
            stats = pool.stats()
            assert stats.checkouts == 100 and stats.dials == 1 and stats.point_in_use == 0
            await pool.close()

    run(main)


def test_the_lanes_are_separate(identity: faketls.Identity) -> None:
    async def main() -> None:
        with fakehoard.FakeHoard(identity) as server:
            pool = new_pool(server, identity, PoolOptions(size=1, bulk_size=1))
            point, bulk = await checkout(pool, "point"), await checkout(pool, "bulk")
            assert len(server.connections) == 2
            pool.release(point, reusable=True)
            pool.release(bulk, reusable=True)
            await pool.close()

    run(main)


# --- the bounded queue ---------------------------------------------------------------------------


def test_a_full_lane_makes_the_next_caller_wait_for_a_release(identity: faketls.Identity) -> None:
    async def main() -> None:
        with fakehoard.FakeHoard(identity) as server:
            pool = new_pool(server, identity, PoolOptions(size=1))
            first = await checkout(pool)
            task = asyncio.ensure_future(checkout(pool))
            assert await until(lambda: pool.stats().waiting == 1)
            pool.release(first, reusable=True)
            second = await task
            assert second.conn is first.conn
            pool.release(second, reusable=True)
            await pool.close()

    run(main)


def test_a_wait_that_expires_is_a_pool_timeout_with_the_arithmetic(
    identity: faketls.Identity,
) -> None:
    async def main() -> None:
        with fakehoard.FakeHoard(identity) as server:
            pool = new_pool(server, identity, PoolOptions(size=1))
            held = await checkout(pool)
            loop = asyncio.get_running_loop()
            started = loop.time()
            with pytest.raises(errors.PoolTimeoutError) as caught:
                await checkout(pool, seconds=0.2)
            assert 0.15 < loop.time() - started < 3
            assert "0.2 s" in str(caught.value) and "1 in use" in str(caught.value)
            assert pool.stats().waiting == 0
            pool.release(held, reusable=True)
            await pool.close()

    run(main)


def test_a_full_queue_is_refused_at_once(identity: faketls.Identity) -> None:
    async def main() -> None:
        with fakehoard.FakeHoard(identity) as server:
            pool = new_pool(server, identity, PoolOptions(size=1, max_waiters=1))
            held = await checkout(pool)
            waiter = asyncio.ensure_future(checkout(pool, seconds=2.0))
            assert await until(lambda: pool.stats().waiting == 1)
            loop = asyncio.get_running_loop()
            started = loop.time()
            with pytest.raises(errors.PoolTimeoutError, match="max_waiters"):
                await checkout(pool, seconds=30.0)
            assert loop.time() - started < 1.0
            pool.release(held, reusable=True)
            pool.release(await waiter, reusable=True)
            await pool.close()

    run(main)


def test_a_cancelled_waiter_passes_its_wakeup_on(identity: faketls.Identity) -> None:
    async def main() -> None:
        with fakehoard.FakeHoard(identity) as server:
            pool = new_pool(server, identity, PoolOptions(size=1))
            held = await checkout(pool)
            first = asyncio.ensure_future(checkout(pool))
            second = asyncio.ensure_future(checkout(pool))
            assert await until(lambda: pool.stats().waiting == 2)
            pool.release(held, reusable=True)  # wakes the first waiter...
            first.cancel()  # ...which is cancelled before it runs
            with pytest.raises(asyncio.CancelledError):
                await first
            lease = await asyncio.wait_for(second, 5.0)  # the second must not be stranded
            pool.release(lease, reusable=True)
            assert pool.stats().point_in_use == 0 and pool.stats().waiting == 0
            await pool.close()

    run(main)


# --- liveness at checkout ------------------------------------------------------------------------


def test_an_idle_connection_the_server_closed_is_replaced_not_used(
    identity: faketls.Identity,
) -> None:
    async def main() -> None:
        with fakehoard.FakeHoard(identity) as server:
            pool = new_pool(server, identity)
            pool.release(await checkout(pool), reusable=True)
            server.connections[0].sock.shutdown(socket.SHUT_RDWR)
            await asyncio.sleep(0.1)
            lease = await checkout(pool)
            assert len(server.connections) == 2 and pool.stats().discarded == 1
            pool.release(lease, reusable=True)
            await pool.close()

    run(main)


def test_a_close_that_arrived_while_the_loop_was_not_running_is_found_at_checkout(
    identity: faketls.Identity,
) -> None:
    """The protocol's flags only change when the loop runs. A server that closes between two
    loop iterations (or while the program is busy elsewhere) leaves the FIN unread in the socket,
    and the flags still say "open": the probe must look at the socket as the blocking one does."""

    async def main() -> None:
        with fakehoard.FakeHoard(identity) as server:
            pool = new_pool(server, identity)
            pool.release(await checkout(pool), reusable=True)
            server.connections[0].sock.shutdown(socket.SHUT_RDWR)
            time.sleep(0.2)  # blocks the loop on purpose: the close must stay unread
            lease = await checkout(pool)
            assert len(server.connections) == 2, "a new connection must have been dialled"
            assert pool.stats().discarded == 1
            pool.release(lease, reusable=True)
            await pool.close()

    run(main)


def test_a_connection_idle_past_the_limit_is_closed_instead_of_reused(
    identity: faketls.Identity,
) -> None:
    async def main() -> None:
        with fakehoard.FakeHoard(identity) as server:
            pool = new_pool(server, identity, PoolOptions(idle_timeout=0.1))
            pool.release(await checkout(pool), reusable=True)
            await asyncio.sleep(0.2)
            pool.release(await checkout(pool), reusable=True)
            assert len(server.connections) == 2
            await pool.close()

    run(main)


def test_a_stale_but_young_connection_gets_one_ping_before_use(identity: faketls.Identity) -> None:
    async def main() -> None:
        with fakehoard.FakeHoard(identity) as server:
            pool = new_pool(server, identity)
            lease = await checkout(pool)
            lease.conn.last_used = monotonic() - 61.0
            pool.release(lease, reusable=True)
            again = await checkout(pool)
            assert again.conn is lease.conn
            assert len(server.frames_with(opcodes.PING)) == 1
            pool.release(again, reusable=True)
            pool.release(await checkout(pool), reusable=True)
            assert len(server.frames_with(opcodes.PING)) == 1
            await pool.close()

    run(main)


def test_a_connection_released_unreusable_is_closed(identity: faketls.Identity) -> None:
    async def main() -> None:
        with fakehoard.FakeHoard(identity) as server:
            pool = new_pool(server, identity)
            lease = await checkout(pool)
            pool.release(lease, reusable=False)
            assert not lease.conn.healthy and pool.stats().point_idle == 0
            pool.release(await checkout(pool), reusable=True)
            assert len(server.connections) == 2
            await pool.close()

    run(main)


# --- hosts and dial backoff ----------------------------------------------------------------------


def _dead_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port: int = s.getsockname()[1]
    s.close()
    return port


def test_a_dead_first_host_fails_over_to_the_next_in_written_order(
    identity: faketls.Identity,
) -> None:
    async def main() -> None:
        with fakehoard.FakeHoard(identity) as server:
            pool = Pool(make_options([_dead_port(), server.port], identity))
            lease = await checkout(pool)
            assert lease.conn.host.port == server.port
            pool.release(lease, reusable=True)
            await pool.close()

    run(main)


def test_one_pass_over_dead_hosts_fails_and_the_lane_backs_off_before_the_next_dial(
    identity: faketls.Identity,
) -> None:
    async def main() -> None:
        calls, sleep = sleeper()
        pool = Pool(
            make_options([_dead_port(), _dead_port()], identity), sleep=sleep, rng=lambda: 1.0
        )
        for _ in range(3):
            with pytest.raises(errors.ConnectionFailedError):
                await checkout(pool)
        assert [round(x, 1) for x in calls] == [0.1, 0.2]
        assert pool.stats().dial_failures == 3
        await pool.close()

    run(main)


def test_a_zero_id_overloaded_refusal_is_redialled_on_the_same_host_with_backoff(
    identity: faketls.Identity,
) -> None:
    refusal = fakehoard.Reply(opcodes.ERROR, {"code": 13, "message": "full"}, bytes(16))

    def greeting(index: int) -> Sequence[fakehoard.Action] | None:
        return [refusal] if index < 2 else None

    async def main() -> None:
        with fakehoard.FakeHoard(identity, greeting_fn=greeting) as server:
            calls, sleep = sleeper()
            pool = new_pool(server, identity, sleep=sleep, rng=lambda: 1.0)
            lease = await checkout(pool)
            assert len(server.connections) == 3 and calls == [0.1, 0.2]
            pool.release(lease, reusable=True)
            await pool.close()

    run(main)


# --- tokens --------------------------------------------------------------------------------------


def test_new_connections_present_the_token_not_the_password(identity: faketls.Identity) -> None:
    async def main() -> None:
        with fakehoard.FakeHoard(identity) as server:
            pool = new_pool(server, identity)
            a, b = await checkout(pool), await checkout(pool)
            first = server.connections[0].auth_requests[0]
            second = server.connections[1].auth_requests[0]
            assert first["password"] == "pw" and "token" not in first
            assert second["token"] == "tok-1" and "password" not in second
            pool.release(a, reusable=True)
            pool.release(b, reusable=True)
            await pool.close()

    run(main)


def test_a_refused_token_falls_back_to_the_password_once_on_a_new_connection(
    identity: faketls.Identity,
) -> None:
    def on_auth(conn: fakehoard.Conn, doc: dict[str, Any]) -> Sequence[fakehoard.Action]:
        return [fakehoard.auth_fail(3)] if "token" in doc else [fakehoard.auth_ok()]

    async def main() -> None:
        with fakehoard.FakeHoard(identity, on_auth=on_auth) as server:
            calls, sleep = sleeper()
            pool = new_pool(server, identity, sleep=sleep, rng=lambda: 0.5)
            a, b = await checkout(pool), await checkout(pool)
            docs = [c.auth_requests[0] for c in server.connections]
            assert [("token" in d, "password" in d) for d in docs] == [
                (False, True),
                (True, False),
                (False, True),
            ]
            assert calls == [0.5]
            pool.release(a, reusable=True)
            pool.release(b, reusable=True)
            await pool.close()

    run(main)


@pytest.mark.parametrize("reason", [1, 5, 6, 7])
def test_these_auth_failures_are_not_retried_in_a_loop(
    identity: faketls.Identity, reason: int
) -> None:
    async def main() -> None:
        with fakehoard.FakeHoard(
            identity, on_auth=lambda c, d: [fakehoard.auth_fail(reason)]
        ) as server:
            pool = new_pool(server, identity)
            with pytest.raises(errors.AuthenticationError):
                await checkout(pool)
            assert len(server.connections) == 1
            await pool.close()

    run(main)


def test_a_token_is_refreshed_on_a_held_connection_at_half_its_life(
    identity: faketls.Identity,
) -> None:
    offset = [0.0]
    answers = iter(
        [fakehoard.auth_ok("t1", expires_in=100), fakehoard.auth_ok("t2", expires_in=100)]
    )

    async def main() -> None:
        with fakehoard.FakeHoard(identity, on_auth=lambda c, d: [next(answers)]) as server:
            pool = new_pool(
                server,
                identity,
                PoolOptions(idle_timeout=10_000.0),
                clock=lambda: monotonic() + offset[0],
            )
            pool.release(await checkout(pool), reusable=True)
            offset[0] = 40.0
            pool.release(await checkout(pool), reusable=True)
            assert len(server.connections[0].auth_requests) == 1
            offset[0] = 55.0
            lease = await checkout(pool)
            docs = server.connections[0].auth_requests
            assert len(docs) == 2 and docs[1]["token"] == "t1" and "password" not in docs[1]
            assert len(server.connections) == 1
            pool.release(lease, reusable=True)
            pool.release(await checkout(pool), reusable=True)
            assert len(server.connections[0].auth_requests) == 2
            assert pool.tokens.token is not None and pool.tokens.token.reveal() == "t2"
            await pool.close()

    run(main)


# --- cancellation, close, fork, loop -------------------------------------------------------------


@pytest.mark.parametrize("point", ["while_waiting", "while_dialling", "after_connection"])
def test_a_cancellation_anywhere_in_checkout_leaves_every_slot_free(
    identity: faketls.Identity, point: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def main() -> None:
        hang: list[fakehoard.Action] = [fakehoard.Hang()]
        with fakehoard.FakeHoard(
            identity,
            on_auth=lambda c, d: hang if point == "while_dialling" else [fakehoard.auth_ok()],
        ) as server:
            pool = new_pool(server, identity, PoolOptions(size=1))
            held = await checkout(pool) if point == "while_waiting" else None
            if point == "after_connection":
                real = pool._connection_for  # pyright: ignore[reportPrivateUsage]

                async def then_cancel(*a: Any, **k: Any) -> Any:
                    await real(*a, **k)
                    raise asyncio.CancelledError

                monkeypatch.setattr(pool, "_connection_for", then_cancel)
            tasks = [asyncio.ensure_future(checkout(pool, seconds=30.0)) for _ in range(3)]
            await asyncio.sleep(0.3)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            monkeypatch.undo()
            if held is not None:
                pool.release(held, reusable=True)
            stats = pool.stats()
            assert stats.point_in_use == 0 and stats.waiting == 0
            await pool.close()

    run(main)


def test_close_is_idempotent_closes_idle_sockets_and_refuses_new_calls(
    identity: faketls.Identity,
) -> None:
    async def main() -> None:
        with fakehoard.FakeHoard(identity) as server:
            pool = new_pool(server, identity)
            pool.release(await checkout(pool), reusable=True)
            await pool.close()
            await pool.close()
            assert await until(lambda: server.connections[0].closed)
            with pytest.raises(errors.ClientClosedError):
                await checkout(pool)
            assert pool.stats().closed

    run(main)


def test_close_waits_for_an_in_flight_call_then_aborts_its_transport(
    identity: faketls.Identity,
) -> None:
    async def main() -> None:
        with fakehoard.FakeHoard(identity) as server:
            pool = new_pool(server, identity)
            lease = await checkout(pool)
            loop = asyncio.get_running_loop()
            started = loop.time()
            await pool.close(timeout=0.3)
            assert 0.25 < loop.time() - started < 3
            assert not lease.conn.healthy
            pool.release(lease, reusable=True)
            assert pool.stats().point_in_use == 0

    run(main)


def test_close_wakes_a_waiter_with_client_closed(identity: faketls.Identity) -> None:
    async def main() -> None:
        with fakehoard.FakeHoard(identity) as server:
            pool = new_pool(server, identity, PoolOptions(size=1))
            held = await checkout(pool)
            waiter = asyncio.ensure_future(checkout(pool, seconds=10.0))
            assert await until(lambda: pool.stats().waiting == 1)
            await pool.close(timeout=0.1)
            with pytest.raises(errors.ClientClosedError):
                await waiter
            pool.release(held, reusable=True)

    run(main)


def test_a_forked_pool_refuses_calls_and_the_parents_connection_still_answers(
    identity: faketls.Identity,
) -> None:
    async def main() -> None:
        with fakehoard.FakeHoard(identity) as server:
            pool = new_pool(server, identity)
            lease = await checkout(pool)
            pool.release(lease, reusable=True)
            pid = os.fork()
            if pid == 0:
                code = 1
                try:
                    pool.mark_forked()
                    pool.close_now()
                    code = 0
                finally:
                    os._exit(code)
            _, status = os.waitpid(pid, 0)
            assert os.WEXITSTATUS(status) == 0
            again = await checkout(pool)
            assert again.conn is lease.conn
            await again.conn.exchange(
                PingRequest(), Context(operation="ping", kind="READ"), Deadline(5.0)
            )
            pool.release(again, reusable=True)
            await pool.close()

    run(main)


def test_a_pool_used_from_a_second_loop_is_a_config_error() -> None:
    holder: list[Pool] = []

    async def first() -> None:
        opts = config.resolve_options("hoarddb://u:p@127.0.0.1:1", {}, {})
        holder.append(Pool(opts))

    asyncio.run(first())

    async def second() -> None:
        with pytest.raises(errors.ConfigError, match="event loop"):
            await holder[0].checkout("point", Deadline(1.0), op="get", kind="READ")

    asyncio.run(second())
