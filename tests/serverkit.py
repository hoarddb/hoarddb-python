# SPDX-License-Identifier: Apache-2.0
"""Real hoardDB test servers as local processes (handbook testing.md section 3).

A server is started with ONLY the handbook's environment, in its own temporary directory, and is
ready when its log carries the ``"TCP driver listener listening"`` line with the key fingerprint
(that line is the readiness signal and the pin). Every exit path kills it and removes the
directory: ``running()`` does it in a ``finally``, ``atexit`` does it for a process that dies
between, and a stray server is a defect.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import pty
import secrets
import select
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Generator, Mapping
from pathlib import Path
from typing import cast

BIN_VARIABLE = "HOARDDB_SERVER_BIN"
FIX = f"run: export {BIN_VARIABLE}=$(scripts/fetch-server.sh) (it needs a token in GH_TOKEN)"
READY_MESSAGE = "TCP driver listener listening"
READY_TIMEOUT = 30.0  # seconds; the pinned server starts in well under 1 s on a quiet machine
START_ATTEMPTS = 4  # a start that lost a port race is made again on fresh ports, this many times
CLUSTER_ATTEMPTS = 3  # a cluster whose ring port was lost is made again, this many times
BIND_FAILURE = "address already in use"
STOP_GRACE = 15.0  # SIGTERM, then SIGKILL after this long (handbook testing.md section 3.1)
ROOT_USER = "admin"
ROOT_PASSWORD = "vectors-root-password"  # the vectors' fixed test credential


class ServerUnavailableError(Exception):
    """The test server binary is missing; the message says what to run."""


class ServerStartError(Exception):
    """The server did not become ready; the message carries the end of its log."""


class PortStolenError(ServerStartError):
    """A port chosen with ``free_port()`` was taken by another process before the server bound it
    (``free_port`` closes its probe, so nothing reserves the number in between)."""


def server_binary() -> Path:
    """The binary named by ``HOARDDB_SERVER_BIN``; a real-server test fails loudly without it."""
    value = os.environ.get(BIN_VARIABLE, "")
    if not value:
        raise ServerUnavailableError(f"{BIN_VARIABLE} is not set: {FIX}")
    path = Path(value)
    if not (path.is_file() and os.access(path, os.X_OK)):
        raise ServerUnavailableError(f"{BIN_VARIABLE}={value} is not an executable file: {FIX}")
    return path


def free_port() -> int:
    """A port nothing listens on right now: bind port 0, read the number, close, use it at once."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


def free_ring_port(attempts: int = 20) -> int:
    """A ring port ``R`` whose successor ``R+1`` (the internode listener) is also free."""
    for _ in range(attempts):
        first = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        second = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            first.bind(("127.0.0.1", 0))
            port: int = first.getsockname()[1]
            second.bind(("127.0.0.1", port + 1))
        except OSError:
            continue
        finally:
            first.close()
            second.close()
        return port
    raise ServerStartError(f"no pair of adjacent free ports in {attempts} tries; free some ports")


_live: list[Node] = []


class Node:
    """One server process: its directories, ports, environment and log."""

    def __init__(
        self,
        binary: Path,
        extra_env: Mapping[str, str] | None = None,
        *,
        name: str = "node",
        cluster: bool = False,
        parent: Path | None = None,
    ) -> None:
        self.binary = binary
        self.name = name
        self._cluster = cluster
        self._offset = 0  # where in the log the current attempt began
        self.root = Path(tempfile.mkdtemp(prefix=f"hoarddb-{name}-", dir=parent))
        self.data_dir = self.root / "data"
        self.home = self.root / "home"
        self.log_path = self.root / "server.log"
        self.data_dir.mkdir()
        self.home.mkdir()
        self.driver_port = free_port()
        self.metrics_port = free_port()
        self.ring_port = free_ring_port() if cluster else free_port()
        self.internode_port: int | None = None
        environment: dict[str, str] = {
            "HOME": str(self.home),
            "HOARDDB_DATA_DIR": str(self.data_dir),
            "HOARDDB_TCP_LISTEN": f"127.0.0.1:{self.driver_port}",
            "HOARDDB_LISTEN": f"127.0.0.1:{self.ring_port}",
            "HOARDDB_METRICS_LISTEN": f"127.0.0.1:{self.metrics_port}",
            "HOARDDB_ROOT_USER": ROOT_USER,
            "HOARDDB_ROOT_PASSWORD": ROOT_PASSWORD,
        }
        if not cluster:
            self.internode_port = free_port()
            environment["HOARDDB_TCP_INTERNODE_LISTEN"] = f"127.0.0.1:{self.internode_port}"
        environment.update(extra_env or {})
        self.env = environment
        self.process: subprocess.Popen[bytes] | None = None
        self.fingerprint = ""
        self.startup_seconds = 0.0
        _live.append(self)

    # -- addresses -----------------------------------------------------------------------------

    @property
    def address(self) -> str:
        return f"127.0.0.1:{self.driver_port}"

    def uri(self, user: str = ROOT_USER, password: str | None = ROOT_PASSWORD) -> str:
        login = user if password is None else f"{user}:{password}"
        return f"hoarddb://{login}@{self.address}"

    # -- life cycle ----------------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> None:
        """Start the process and wait (bounded) for its ready line; record the time it took.

        A start that failed because another process took one of the ports this node chose is made
        again on fresh ports, up to ``START_ATTEMPTS`` times. A cluster member's ring port is in
        every member's seed list, so losing that one is reported as ``PortStolenError`` for
        ``start_cluster`` to deal with."""
        if self.running:
            raise ServerStartError(f"{self.name} is already running; stop it before starting it")
        for attempt in range(1, START_ATTEMPTS + 1):
            try:
                self._start_once()
                return
            except ServerStartError as failure:
                conflict = self.bind_conflict()
                if conflict is None:
                    raise
                if self._cluster and str(self.ring_port) in conflict:
                    raise self._stolen(conflict) from failure
                if attempt == START_ATTEMPTS:
                    raise self._stolen(conflict, attempts=attempt) from failure
                self._choose_other_ports()
        raise AssertionError("unreachable: the last attempt returns or raises")  # pragma: no cover

    def _start_once(self) -> None:
        self._offset = self.log_path.stat().st_size if self.log_path.exists() else 0
        began = time.monotonic()
        with self.log_path.open("ab") as log:
            self.process = subprocess.Popen(
                [str(self.binary)],
                env=self.env,
                cwd=self.root,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        self.fingerprint = self._wait_ready(self._offset, began)
        self.startup_seconds = time.monotonic() - began

    def bind_conflict(self) -> str | None:
        """The log line of the current attempt that says a port was already in use, if any."""
        try:
            with self.log_path.open("rb") as log:
                log.seek(self._offset)
                text = log.read().decode("utf-8", "replace")
        except OSError:
            return None
        return next((line for line in text.splitlines() if BIND_FAILURE in line), None)

    def _stolen(self, conflict: str, attempts: int | None = None) -> PortStolenError:
        tried = f"after {attempts} attempts " if attempts is not None else ""
        return PortStolenError(
            f"{self.name}: a port chosen a moment before was taken by another process {tried}"
            f"({conflict.strip()}). The fixtures start again on fresh ports ({START_ATTEMPTS} "
            "attempts for a server, "
            f"{CLUSTER_ATTEMPTS} for a cluster); this did not get through. Find the process that "
            "holds the port with `ss -ltnp`, stop it, and run the tests again."
        )

    def _choose_other_ports(self) -> None:
        """Fresh ports for everything only this node uses; the environment follows, unless a test
        set that variable itself."""

        def move(variable: str, new: int, old: int) -> None:
            if self.env.get(variable) == f"127.0.0.1:{old}":
                self.env[variable] = f"127.0.0.1:{new}"

        old_driver, old_metrics = self.driver_port, self.metrics_port
        self.driver_port, self.metrics_port = free_port(), free_port()
        move("HOARDDB_TCP_LISTEN", self.driver_port, old_driver)
        move("HOARDDB_METRICS_LISTEN", self.metrics_port, old_metrics)
        if not self._cluster:
            old_ring, old_internode = self.ring_port, self.internode_port
            self.ring_port, self.internode_port = free_port(), free_port()
            move("HOARDDB_LISTEN", self.ring_port, old_ring)
            if old_internode is not None:
                move("HOARDDB_TCP_INTERNODE_LISTEN", self.internode_port, old_internode)

    def _wait_ready(self, offset: int, began: float) -> str:
        assert self.process is not None
        deadline = began + READY_TIMEOUT
        while True:
            with self.log_path.open("rb") as log:
                log.seek(offset)
                text = log.read().decode("utf-8", "replace")
            for line in text.splitlines():
                entry = _parse(line)
                if entry is not None and entry.get("msg") == READY_MESSAGE:
                    fingerprint = str(entry.get("fingerprint", ""))
                    if fingerprint.startswith("SHA256:"):
                        return fingerprint
            if self.process.poll() is not None:
                raise ServerStartError(
                    f"{self.name} exited with status {self.process.returncode} before it was "
                    f"ready; the end of its log:\n{self.log_tail()}"
                )
            if time.monotonic() > deadline:
                self.kill()
                raise ServerStartError(
                    f"{self.name} was not ready after {READY_TIMEOUT:.0f} s; the end of its "
                    f"log:\n{self.log_tail()}"
                )
            time.sleep(0.02)  # polling a log file; bounded by the deadline above

    def stop(self) -> None:
        """SIGTERM, then SIGKILL after the grace period; always reaps the process."""
        process = self.process
        if process is None or process.poll() is not None:
            return
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=STOP_GRACE)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def kill(self) -> None:
        """SIGKILL now (a crash), and reap it."""
        process = self.process
        if process is None or process.poll() is not None:
            return
        process.kill()
        process.wait()

    def restart(self) -> None:
        """Stop it and start it again with the same data directory, ports and variables."""
        self.stop()
        self.start()

    def cleanup(self) -> None:
        """Stop the process and remove its directory. Safe to call twice."""
        try:
            self.stop()
        finally:
            shutil.rmtree(self.root, ignore_errors=True)
            with contextlib.suppress(ValueError):
                _live.remove(self)

    def reset_password(self, user: str, new_password: str, *, timeout: float = 30.0) -> None:
        """``-reset-password`` while the server is stopped; the flag prompts twice on a terminal,
        so the answers are typed into a pseudo-terminal."""
        if self.running:
            raise ServerStartError(f"{self.name} must be stopped before its password is reset")
        master, slave = pty.openpty()
        process = subprocess.Popen(
            [str(self.binary), "-reset-password", user],
            env=self.env,
            cwd=self.root,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
        )
        os.close(slave)
        seen = b""
        answers = 0
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.2)
                if ready:
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:
                        break  # the child closed the terminal: it is done
                    seen += chunk
                    if answers < 2 and b":" in chunk:
                        os.write(master, new_password.encode() + b"\n")
                        answers += 1
                elif process.poll() is not None:
                    break
            process.wait(timeout=max(1.0, deadline - time.monotonic()))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)
        if process.returncode != 0 or b"PASSWORD RESET WRITTEN" not in seen:
            tail = seen.decode("utf-8", "replace")[-600:]
            raise ServerStartError(f"-reset-password {user} failed ({process.returncode}):\n{tail}")

    def log_tail(self, lines: int = 20) -> str:
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "(no log)"
        return "\n".join(text.splitlines()[-lines:])

    def log_lines(self) -> list[dict[str, object]]:
        """Every JSON line the server has logged so far."""
        lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return [entry for entry in map(_parse, lines) if entry is not None]


class Cluster:
    """Three members and, optionally, a fourth node that no member has added (handbook
    ``testing.md`` section 3.2). ``cleanup`` kills every process and removes every directory."""

    def __init__(self, members: list[Node], newcomer: Node | None, fingerprints: dict[str, str]):
        self.members = members
        self.newcomer = newcomer
        self.fingerprints = fingerprints  # node id -> the fingerprint of the key it was given

    @property
    def nodes(self) -> list[Node]:
        return [*self.members, *([self.newcomer] if self.newcomer is not None else [])]

    def start(self) -> None:
        """Start every process at once (a member waits for the others to confirm it)."""
        failures: list[BaseException] = []

        def go(node: Node) -> None:
            try:
                node.start()
            except BaseException as problem:
                failures.append(problem)

        threads = [threading.Thread(target=go, args=(n,)) for n in self.nodes]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if failures:
            raise next((f for f in failures if isinstance(f, PortStolenError)), failures[0])

    def check_alive(self) -> None:
        """Raise at once if a node has died: the others would wait for a membership that cannot
        come. A node that could not bind its port is named as such (``PortStolenError``)."""
        for node in self.nodes:
            if node.process is None or node.process.poll() is None:
                continue
            conflict = node.bind_conflict()
            if conflict is not None:
                raise node._stolen(conflict)  # pyright: ignore[reportPrivateUsage]
            raise ServerStartError(
                f"{node.name} exited with status {node.process.returncode} while the cluster was "
                f"coming up; the end of its log:\n{node.log_tail()}"
            )

    def cleanup(self) -> None:
        for node in self.nodes:
            node.cleanup()


def _fast_disk_parent() -> Path | None:
    """Where a cluster's data lives. The vectors were captured on a healthy host, so ``health``
    must say ``healthy: true``; the server calls a filesystem more than 85 % used unhealthy, and a
    sandbox's disk allowance can look full. A tmpfs is never full, so it is preferred when there is
    one with room; ``HOARDDB_TEST_TMPDIR`` overrides."""
    chosen = os.environ.get("HOARDDB_TEST_TMPDIR")
    if chosen:
        return Path(chosen)
    shm = Path("/dev/shm")
    if shm.is_dir() and os.access(shm, os.W_OK) and shutil.disk_usage(shm).free > 2 * 2**30:
        return shm
    return None


def make_cluster(
    binary: Path, extra_env: Mapping[str, str] | None = None, *, newcomer: bool = True
) -> Cluster:
    """Prepare (but do not start) ``node1`` to ``node3`` and a ``newcomer`` with their own keys.

    A member's seeds are the three members; the newcomer's are the three members and itself, and
    nobody has ``join``ed it, so it must refuse requests that name a database (``UNAVAILABLE``).
    """
    import faketls

    parent = _fast_disk_parent()
    names = ["node1", "node2", "node3", *(["newcomer"] if newcomer else [])]
    nodes = [Node(binary, name=n, cluster=True, parent=parent) for n in names]
    try:
        fingerprints: dict[str, str] = {}
        for node in nodes:
            made = faketls.make_identity(node.root, "identity")
            keys = node.data_dir / "keys"
            keys.mkdir(mode=0o700)
            shutil.copyfile(made.key_path, keys / "server.key")
            (keys / "server.key").chmod(0o600)
            shutil.copyfile(made.cert_path, keys / "server.crt")
            fingerprints[node.name] = made.fingerprint
        token = secrets.token_hex(32)
        members = nodes[:3]

        def seeds(group: list[Node]) -> tuple[str, str]:
            addresses = ",".join(f"{n.name}@127.0.0.1:{n.ring_port}" for n in group)
            pins = ",".join(f"127.0.0.1:{n.ring_port}={fingerprints[n.name]}" for n in group)
            return addresses, pins

        for node in nodes:
            addresses, pins = seeds(members if node in members else [*members, node])
            node.env.update(
                {
                    "HOARDDB_NODE_ID": node.name,
                    "HOARDDB_SEED_NODES": addresses,
                    "HOARDDB_SEED_FINGERPRINTS": pins,
                    "HOARDDB_CLUSTER_TOKEN": token,
                    "HOARDDB_REPLICATION_FACTOR": "3",
                    **(extra_env or {}),
                }
            )
    except BaseException:
        for node in nodes:
            node.cleanup()
        raise
    return Cluster(members, nodes[3] if newcomer else None, fingerprints)


def start_cluster(
    binary: Path,
    confirm: Callable[[Cluster], float],
    extra_env: Mapping[str, str] | None = None,
    *,
    newcomer: bool = True,
) -> tuple[Cluster, float]:
    """Make, start and confirm a cluster; if a ring port was taken by another process before a
    node bound it, throw that cluster away and make another on fresh ports, up to
    ``CLUSTER_ATTEMPTS`` times. ``confirm`` waits for the membership and returns the seconds it
    took; it should call ``Cluster.check_alive`` so that a dead node is reported at once."""
    for attempt in range(1, CLUSTER_ATTEMPTS + 1):
        group = make_cluster(binary, extra_env, newcomer=newcomer)
        try:
            group.start()
            return group, confirm(group)
        except PortStolenError:
            group.cleanup()
            if attempt == CLUSTER_ATTEMPTS:
                raise
        except BaseException:
            group.cleanup()
            raise
    raise AssertionError("unreachable: the last attempt returns or raises")  # pragma: no cover


def _parse(line: str) -> dict[str, object] | None:
    """One JSON log line, or ``None`` for anything else (the banner, a plain-text line)."""
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    if not isinstance(entry, dict):
        return None
    return {str(key): value for key, value in cast("dict[object, object]", entry).items()}


@contextlib.contextmanager
def running(
    binary: Path, extra_env: Mapping[str, str] | None = None, *, name: str = "node"
) -> Generator[Node]:
    """A started single-node server, killed and removed on every exit path."""
    node = Node(binary, extra_env, name=name)
    try:
        node.start()
        yield node
    finally:
        node.cleanup()


def _reap_at_exit() -> None:
    """The last line of defence: no server outlives the test process."""
    for node in list(_live):
        if node.process is not None and node.process.poll() is None:
            node.process.kill()
            node.process.wait()
        shutil.rmtree(node.root, ignore_errors=True)


atexit.register(_reap_at_exit)
