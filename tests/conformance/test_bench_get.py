# SPDX-License-Identifier: Apache-2.0
"""``scripts/bench_get.py`` prints one line with p50 and p99 of ``get`` for either client."""

from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest

import readmekit
from conformance.test_examples import environment
from realkit import Servers

pytestmark = pytest.mark.server

LINE = re.compile(
    r"^get \((?P<kind>blocking|asyncio)\): n=(?P<n>\d+) p50=(?P<p50>[\d.]+) us "
    r"p99=(?P<p99>[\d.]+) us min=(?P<min>[\d.]+) us max=(?P<max>[\d.]+) us$"
)


@pytest.mark.parametrize(("flag", "kind"), [([], "blocking"), (["--async"], "asyncio")])
def test_the_script_prints_ordered_percentiles(
    flag: list[str], kind: str, servers: Servers
) -> None:
    node = servers.node()
    done = subprocess.run(
        [
            sys.executable,
            str(readmekit.ROOT / "scripts" / "bench_get.py"),
            "--count",
            "300",
            "--warmup",
            "20",
            *flag,
        ],
        env=environment(node),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    match = LINE.match(done.stdout.strip())
    assert match, done.stdout
    p50, p99, low, high = (float(match[k]) for k in ("p50", "p99", "min", "max"))
    assert match["kind"] == kind and match["n"] == "300"
    assert 0 < low <= p50 <= p99 <= high


def test_a_count_too_small_for_a_p99_is_refused() -> None:
    done = subprocess.run(
        [sys.executable, str(readmekit.ROOT / "scripts" / "bench_get.py"), "--count", "10"],
        env={**os.environ, "PYTHONPATH": str(readmekit.ROOT / "src")},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 2 and "at least 100" in done.stderr
