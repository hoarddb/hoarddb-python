# SPDX-License-Identifier: Apache-2.0
"""Shared kit for the runner and cursor tests: a pool and a runner of either mode, a scripted fake
server, and small operations. ``H.run`` hides whether the loop is blocking or asyncio."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Generator, Sequence
from typing import Any

import fakehoard
import faketls
from hoarddb._aio import pool as aio_pool
from hoarddb._aio import run as aio_run
from hoarddb._core import config, frame, opcodes
from hoarddb._core.call import Call, Op
from hoarddb._core.kinds import Kind
from hoarddb._core.messages import CountRequest, PutRequest
from hoarddb._sync import pool as sync_pool
from hoarddb._sync import run as sync_run
from hoarddb.options import PoolOptions, TlsOptions

Actions = Sequence[fakehoard.Action]


class H:
    """A pool and a runner of one mode, with the sleeps they were asked for recorded."""

    def __init__(self, mode: str, options: config.ResolvedOptions, request_timeout: float) -> None:
        self.mode = mode
        self.sleeps: list[float] = []
        self.loop: asyncio.AbstractEventLoop | None = None
        if mode == "sync":
            self.pool: Any = sync_pool.Pool(options, rng=lambda: 1.0, sleep=self.sleeps.append)
            self.runner: Any = sync_run.Runner(
                self.pool, request_timeout, rng=lambda: 1.0, sleep=self.sleeps.append
            )
        else:
            self.loop = asyncio.new_event_loop()

            async def sleep(seconds: float) -> None:
                self.sleeps.append(seconds)

            async def build() -> None:
                self.pool = aio_pool.Pool(options, rng=lambda: 1.0, sleep=sleep)
                self.runner = aio_run.Runner(
                    self.pool, request_timeout, rng=lambda: 1.0, sleep=sleep
                )

            self.loop.run_until_complete(build())

    def run(self, op: Op[Any]) -> Any:
        if self.loop is None:
            return self.runner.run(op)
        return self.loop.run_until_complete(self.runner.run(op))

    def stats(self) -> Any:
        return self.pool.stats()

    def close(self) -> None:
        if self.loop is None:
            self.pool.close()
        else:
            self.loop.run_until_complete(self.pool.close())
            self.loop.run_until_complete(asyncio.sleep(0))
            self.loop.close()


@contextlib.contextmanager
def harness(
    mode: str,
    identity: faketls.Identity,
    handler: Callable[[fakehoard.Conn, frame.Frame], Actions],
    *,
    request_timeout: float = 5.0,
    pool: PoolOptions | None = None,
    client_options: dict[str, Any] | None = None,
    **server_kw: Any,
) -> Generator[tuple[H, fakehoard.FakeHoard], None, None]:
    with fakehoard.FakeHoard(identity, handler=handler, **server_kw) as server:
        extra: dict[str, Any] = {"pool": pool} if pool is not None else {}
        extra.update(client_options or {})
        options = config.resolve_options(
            f"hoarddb://admin:pw@127.0.0.1:{server.port}",
            {"tls": TlsOptions(fingerprints=[identity.fingerprint]), **extra},
            {},
        )
        h = H(mode, options, request_timeout)
        try:
            yield h, server
        finally:
            h.close()


def count_op(*, kind: Kind = "READ", timeout: float | None = None, lane: str = "point") -> Op[int]:
    reply = yield Call(
        op="count",
        target="d/b",
        request=CountRequest(database="d", bucket="b"),
        lane=lane,  # type: ignore[arg-type]
        kind=kind,
        timeout=timeout,
    )
    return int(reply.response.count)


def put_op(kind: Kind = "WRITE", doc: dict[str, Any] | None = None) -> Op[str]:
    reply = yield Call(
        op="put",
        target="d/b",
        request=PutRequest(database="d", bucket="b", key="k", data=doc or {"a": 1}),
        lane="point",
        kind=kind,
    )
    return str(reply.response.id)


def script(*steps: Actions) -> Callable[[fakehoard.Conn, frame.Frame], Actions]:
    """The handler answers the n-th non-AUTH request (across connections) with the n-th step; the
    last step repeats."""
    seen = [0]

    def handler(conn: fakehoard.Conn, request: frame.Frame) -> Actions:
        step = steps[min(seen[0], len(steps) - 1)]
        seen[0] += 1
        return step

    return handler


COUNT_OK: Actions = [fakehoard.Reply(opcodes.COUNT, {"count": 7})]
PUT_OK: Actions = [fakehoard.Reply(opcodes.PUT, {"id": "k", "success": True})]


def err(code: int, message: str = "no", **extra: Any) -> Actions:
    return [fakehoard.error(code, message, **extra)]
