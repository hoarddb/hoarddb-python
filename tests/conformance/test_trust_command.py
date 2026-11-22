# SPDX-License-Identifier: Apache-2.0
"""``python -m hoarddb trust`` against the real server: the fingerprint it shows is the one on the
server's ``TCP driver listener listening`` line, and after ``yes`` a bare URI connects."""

from __future__ import annotations

import os
import pty
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

import hoarddb
from realkit import Servers
from serverkit import ROOT_PASSWORD, ROOT_USER, Node

pytestmark = pytest.mark.server

SRC = str(Path(__file__).resolve().parents[2] / "src")


def run_on_a_terminal(argv: list[str], home: Path, answer: str) -> tuple[int, str]:
    """Run ``python -m hoarddb ...`` with a pseudo-terminal for stdin and stdout and type
    ``answer`` at the first prompt (bounded: the process is killed after 30 s)."""
    master, slave = pty.openpty()
    env = {**os.environ, "HOME": str(home), "PYTHONPATH": SRC}
    process = subprocess.Popen(
        [sys.executable, "-m", "hoarddb", *argv],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        close_fds=True,
    )
    os.close(slave)
    seen = b""
    typed = False
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                seen += chunk
                if not typed and b"anything else to refuse" in seen:
                    os.write(master, answer.encode() + b"\n")
                    typed = True
            elif process.poll() is not None:
                break
        process.wait(timeout=max(1.0, deadline - time.monotonic()))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)
    return int(process.returncode), seen.decode("utf-8", "replace")


def test_the_command_shows_the_servers_own_fingerprint_and_a_bare_uri_then_works(
    servers: Servers, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node: Node = servers.node()
    target = f"127.0.0.1:{node.driver_port}"
    code, shown = run_on_a_terminal(["trust", target], tmp_path, "yes")
    assert code == 0, shown
    assert node.fingerprint in shown, "the fingerprint on the screen is the one the server logged"
    known = tmp_path / ".hoarddb" / "known_servers"
    assert known.read_text().split()[0] == target
    # a bare URI (no pin) now connects: the shared file vouches for the key
    monkeypatch.setenv("HOME", str(tmp_path))
    with hoarddb.connect(f"hoarddb://{ROOT_USER}:{ROOT_PASSWORD}@{target}") as client:
        client.ping()  # raises if the key were not trusted


def test_a_refusal_leaves_the_shared_file_absent_and_a_bare_uri_still_refuses(
    servers: Servers, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = servers.node()
    target = f"127.0.0.1:{node.driver_port}"
    code, shown = run_on_a_terminal(["trust", target], tmp_path, "no")
    assert code == 1 and "Nothing was written" in shown
    assert not (tmp_path / ".hoarddb").exists()
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(hoarddb.TlsTrustError) as refused:
        hoarddb.connect(f"hoarddb://{ROOT_USER}:{ROOT_PASSWORD}@{target}")
    assert "python -m hoarddb trust" in refused.value.message
