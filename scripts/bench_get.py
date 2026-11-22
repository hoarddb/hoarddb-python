# SPDX-License-Identifier: Apache-2.0
"""Time ``get`` against a server and print p50 and p99 (the number the README's quick start is judged by).

    python scripts/bench_get.py [--count 20000] [--warmup 1000] [--async]

The server is reached the way the README does it: ``HOARDDB_URI`` (with the pin) and
``HOARDDB_ROOT_USER`` / ``HOARDDB_ROOT_PASSWORD``. One document is written to a scratch database,
then ``get`` is called ``--warmup`` times (not counted) and ``--count`` times (each timed with
``time.perf_counter_ns``), one at a time on one connection, and the scratch database is dropped.
It prints one line, for example::

    get (blocking): n=20000 p50=118.3 us p99=241.7 us min=96.0 us max=1893.2 us

The number is what one blocking (or asyncio) client sees on THIS machine against THIS server; it is
a measurement to record, not a promise, and it says nothing about another database or driver.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time

import hoarddb
import hoarddb.aio

DATABASE = "bench_get"


def summary(label: str, nanoseconds: list[int]) -> str:
    micros = sorted(n / 1000 for n in nanoseconds)
    cuts = statistics.quantiles(micros, n=100, method="inclusive")
    return (
        f"get ({label}): n={len(micros)} p50={cuts[49]:.1f} us p99={cuts[98]:.1f} us "
        f"min={micros[0]:.1f} us max={micros[-1]:.1f} us"
    )


def blocking(count: int, warmup: int) -> list[int]:
    with hoarddb.connect() as client:
        client.create_database(DATABASE, if_not_exists=True)
        try:
            users = client.database(DATABASE).create_bucket("users", "hash", if_not_exists=True)
            users.put("ada", {"name": "Ada", "age": 36})
            for _ in range(warmup):
                users.get("ada")
            timings: list[int] = []
            for _ in range(count):
                began = time.perf_counter_ns()
                users.get("ada")
                timings.append(time.perf_counter_ns() - began)
            return timings
        finally:
            client.drop_database(DATABASE)


async def asynchronous(count: int, warmup: int) -> list[int]:
    async with hoarddb.aio.connect() as client:
        await client.create_database(DATABASE, if_not_exists=True)
        try:
            users = await client.database(DATABASE).create_bucket(
                "users", "hash", if_not_exists=True
            )
            await users.put("ada", {"name": "Ada", "age": 36})
            for _ in range(warmup):
                await users.get("ada")
            timings: list[int] = []
            for _ in range(count):
                began = time.perf_counter_ns()
                await users.get("ada")
                timings.append(time.perf_counter_ns() - began)
            return timings
        finally:
            await client.drop_database(DATABASE)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="p50 and p99 of get against HOARDDB_URI")
    parser.add_argument("--count", type=int, default=20000, help="timed calls (default 20000)")
    parser.add_argument("--warmup", type=int, default=1000, help="untimed calls first")
    parser.add_argument("--async", dest="use_async", action="store_true", help="the asyncio client")
    args = parser.parse_args(argv)
    if args.count < 100:
        parser.error("--count must be at least 100 for a p99 to mean anything")
    if args.use_async:
        print(summary("asyncio", asyncio.run(asynchronous(args.count, args.warmup))))
    else:
        print(summary("blocking", blocking(args.count, args.warmup)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
