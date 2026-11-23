# SPDX-License-Identifier: Apache-2.0
"""Every example is run as a subprocess against the pinned server and its output is compared line
for line with the golden text below: an example that no test runs does not exist. The README's
quick-start blocks are run the same way, straight from the README text (spec G-2)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import readmekit
from realkit import Servers
from serverkit import ROOT_PASSWORD, ROOT_USER, Node

pytestmark = pytest.mark.server

ROOT = readmekit.ROOT

GOLDEN: dict[str, list[str]] = {
    "hash": [
        "DOCUMENT_EXISTS not_applied",
        "Ada",
        "None",
        "True",
        "37",
        "['Grace']",
        "2",
        "True False",
    ],
    "btree": [
        "['2026-01-01', '2026-01-02', '2026-01-03', '2026-02-01']",
        "['2026-01-02', '2026-01-03']",
        "2026-01-01 2026-02-01",
    ],
    "fifo": [
        "['welcome-email', 'resize-photo']",
        "welcome-email",
        "resize-photo",
        "send-invoice",
        "[]",
    ],
    "lifo": ["delete line", "type b", "type a", "[]"],
    "heap": ["site is down", "slow report", "typo in footer", "[]"],
    "blob": ["hello.txt 14", "hello, hoardDB 14 {'type': 'text/plain'}", "None"],
    "store_fields": ["2 1", "tea", "['books']"],
    "typed_bucket": ["1815", "refused: True"],
}


def environment(node: Node, *, pinned: bool = True) -> dict[str, str]:
    """What the README's step 3 sets: the URI with the pin, and the credentials."""
    query = f"?fingerprint={node.fingerprint}" if pinned else ""
    return {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "HOARDDB_URI": f"hoarddb://127.0.0.1:{node.driver_port}/app{query}",
        "HOARDDB_ROOT_USER": ROOT_USER,
        "HOARDDB_ROOT_PASSWORD": ROOT_PASSWORD,
    }


def run_file(path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-W", "error", str(path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=path.parent,
    )


def test_every_example_file_has_a_golden_output() -> None:
    on_disk = {p.stem for p in (ROOT / "examples").glob("*.py")}
    assert on_disk == set(GOLDEN) | {"quickstart", "quickstart_async"}


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_the_example_prints_exactly_its_golden_output(name: str, servers: Servers) -> None:
    node = servers.node()
    done = run_file(ROOT / "examples" / f"{name}.py", environment(node))
    assert done.stderr == "", done.stderr
    assert done.returncode == 0
    assert done.stdout.splitlines() == GOLDEN[name]


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_the_example_can_be_run_twice_in_a_row(name: str, servers: Servers) -> None:
    node = servers.node()
    env = environment(node)
    first = run_file(ROOT / "examples" / f"{name}.py", env)
    second = run_file(ROOT / "examples" / f"{name}.py", env)
    assert first.stdout == second.stdout and second.returncode == 0


@pytest.mark.parametrize("name", ["quickstart", "quickstart_async"])
def test_the_quickstart_prints_ada_twice(name: str, servers: Servers) -> None:
    node = servers.node()
    done = run_file(ROOT / "examples" / f"{name}.py", environment(node))
    assert done.returncode == 0 and done.stderr == ""
    assert done.stdout.splitlines() == ["Ada", "Ada"]


def test_the_readme_blocks_run_in_order_exactly_as_printed(
    servers: Servers, tmp_path: Path
) -> None:
    """Steps 3 and the typed block of the README, straight from its text, in one shell."""
    node = servers.node()
    env = environment(node)
    blocks = readmekit.readme_blocks()
    outputs: dict[str, list[str]] = {}
    for name in ("quickstart", "quickstart-async", "typed"):
        script = tmp_path / f"{name}.py"
        script.write_text(blocks[name], encoding="utf-8")
        done = run_file(script, env)
        assert done.returncode == 0 and done.stderr == "", (name, done.stderr)
        outputs[name] = done.stdout.splitlines()
    assert outputs["quickstart"] == ["Ada", "Ada"]
    assert outputs["quickstart-async"] == ["Ada", "Ada"]
    assert outputs["typed"][0] == "welcome-email"
    assert outputs["typed"][1].split()[:2] == ["users", "hash"]


def test_without_a_pin_the_quickstart_stops_with_the_trust_command(
    servers: Servers, tmp_path: Path
) -> None:
    node = servers.node()
    env = {**environment(node, pinned=False), "HOME": str(tmp_path)}
    done = run_file(ROOT / "examples" / "quickstart.py", env)
    assert done.returncode != 0
    assert "python -m hoarddb trust" in done.stderr
    assert node.fingerprint in done.stderr, "the error shows the key it refused"
