# SPDX-License-Identifier: Apache-2.0
"""P18: ``python -m hoarddb trust host:port`` shows the key a server presents, asks once on a
terminal, and records it in ``known_servers`` (spec 5.1 item 10). It writes nothing, to the file or
to the server, on a refusal, an end of input, or a changed key."""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

import faketls
from hoarddb import __main__ as cli
from hoarddb._trust import fingerprint

pytestmark = pytest.mark.filterwarnings("ignore")


class Terminal(io.StringIO):
    """A stdin that is a terminal."""

    def isatty(self) -> bool:
        return True


@pytest.fixture(scope="module")
def identity(tmp_path_factory: pytest.TempPathFactory) -> faketls.Identity:
    return faketls.make_identity(tmp_path_factory.mktemp("cli"), "cli")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path / ".hoarddb" / "known_servers"


def run(argv: list[str], answer: str | None, *, tty: bool = True) -> tuple[int, str, str]:
    stdin = Terminal(answer or "") if tty else io.StringIO(answer or "")
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, stdin=stdin, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def test_a_refusal_writes_nothing_to_the_file_or_the_server(
    identity: faketls.Identity, home: Path
) -> None:
    with faketls.FakeTlsServer(identity) as server:
        code, out, err = run(["trust", f"127.0.0.1:{server.port}"], "no\n")
    server.settle()
    assert code == 1
    assert identity.fingerprint in out + err, "the key is shown before it is asked about"
    assert "Nothing was written" in out + err
    assert not home.exists()
    assert server.bytes_received == 0, "not one application byte may reach the server"


def test_end_of_input_is_a_refusal_not_a_hang(identity: faketls.Identity, home: Path) -> None:
    with faketls.FakeTlsServer(identity) as server:
        code, out, err = run(["trust", f"127.0.0.1:{server.port}"], "")
    assert code == 1 and not home.exists()
    assert "Nothing was written" in out + err


def test_a_stdin_that_is_not_a_terminal_is_refused_without_reading_it(
    identity: faketls.Identity, home: Path
) -> None:
    with faketls.FakeTlsServer(identity) as server:
        code, out, err = run(["trust", f"127.0.0.1:{server.port}"], "yes\n", tty=False)
    assert code == 1 and not home.exists()
    text = out + err
    assert "terminal" in text and "fingerprint=" in text, "it names the pin as the way forward"


def test_yes_records_the_key_in_the_shared_file_with_mode_0600(
    identity: faketls.Identity, home: Path
) -> None:
    with faketls.FakeTlsServer(identity) as server:
        code, out, _ = run(["trust", f"127.0.0.1:{server.port}"], "yes\n")
    server.settle()
    assert code == 0, out
    assert home.read_text() == f"127.0.0.1:{server.port} {identity.public_key.hex()}\n"
    assert (home.stat().st_mode & 0o777) == 0o600
    assert (home.parent.stat().st_mode & 0o777) == 0o700
    assert str(home) in out
    assert server.bytes_received == 0, "trusting a key never sends a password"


def test_a_key_that_is_already_trusted_is_not_asked_about_again(
    identity: faketls.Identity, home: Path
) -> None:
    with faketls.FakeTlsServer(identity) as server:
        assert run(["trust", f"127.0.0.1:{server.port}"], "yes\n")[0] == 0
        code, out, _ = run(["trust", f"127.0.0.1:{server.port}"], "no\n")
    assert code == 0 and "already trusted" in out
    assert home.read_text().count("\n") == 1


def test_a_changed_key_is_refused_naming_both_and_the_file_is_left_alone(
    identity: faketls.Identity, home: Path
) -> None:
    with faketls.FakeTlsServer(identity) as server:
        home.parent.mkdir(mode=0o700)
        old = bytes(range(32))
        home.write_text(f"127.0.0.1:{server.port} {old.hex()}\n")
        home.chmod(0o600)
        before = home.read_text()
        code, out, err = run(["trust", f"127.0.0.1:{server.port}"], "yes\n")
    server.settle()
    text = out + err
    assert code == 1
    assert identity.fingerprint in text and fingerprint.fingerprint_of_key(old) in text
    assert home.read_text() == before
    assert server.bytes_received == 0


def test_a_host_nobody_answers_is_reported_with_the_fix(home: Path) -> None:
    code, out, err = run(["trust", "127.0.0.1:1"], "yes\n")
    assert code == 2 and not home.exists()
    assert "Is hoardDB-server running" in out + err


def test_help_has_a_real_example_and_names_the_file(home: Path) -> None:
    for argv in (["--help"], ["trust", "--help"], ["-h"]):
        code, out, _ = run(argv, None)
        assert code == 0
        assert "python -m hoarddb trust localhost:7433" in out
        assert "known_servers" in out and "fingerprint=" in out
        assert "trust on first use" in out.lower() or "first use" in out.lower()


def test_no_command_and_an_unknown_command_print_the_usage(home: Path) -> None:
    for argv in ([], ["frobnicate"], ["trust"]):
        code, out, err = run(argv, None)
        assert code == 2
        assert "python -m hoarddb trust" in out + err


def test_a_bad_host_is_a_usage_error_not_a_traceback(home: Path) -> None:
    code, out, err = run(["trust", "a:b:c"], "yes\n")
    assert code == 2 and "Traceback" not in out + err and "[::1]" in out + err


def test_the_real_command_does_not_hang_when_stdin_is_not_a_terminal(
    identity: faketls.Identity, tmp_path: Path
) -> None:
    env = {**os.environ, "HOME": str(tmp_path)}
    with faketls.FakeTlsServer(identity) as server:
        done = subprocess.run(
            [sys.executable, "-m", "hoarddb", "trust", f"127.0.0.1:{server.port}"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            env={**env, "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src")},
        )
    assert done.returncode == 1 and identity.fingerprint in done.stdout + done.stderr
    assert not (tmp_path / ".hoarddb").exists()
