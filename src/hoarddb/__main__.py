# SPDX-License-Identifier: Apache-2.0
"""``python -m hoarddb``: the command line of the driver.

One command today, ``trust``: connect, show the key the server presents, ask once on a terminal,
and record the key in the shared ``known_servers`` file so that a bare ``hoarddb://host`` URI works
afterwards. It never sends a password, writes nothing on a refusal, an end of input or a changed
key, and is deliberately ``python -m hoarddb`` and not a console script named ``hoarddb`` (that name
would collide with the ``hoardDB`` command-line tool on a case-insensitive file system).
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import TextIO

from hoarddb import errors
from hoarddb._core.uri import parse_host_port
from hoarddb._trust.connect import connect_pinned
from hoarddb._trust.known_servers import KnownServers
from hoarddb.options import ServerKey, TlsOptions

USAGE = """\
usage: python -m hoarddb trust <host[:port]>

Show the key a hoardDB server presents, ask once, and record it in
~/.hoarddb/known_servers (mode 0600) so that `hoarddb.connect("hoarddb://<host>")`
works with no pin. It sends no password and writes nothing unless you answer yes.

example:
    python -m hoarddb trust localhost:7433

Compare the fingerprint with the one the server logs at every start (the
`TCP driver listener listening` line). Trusting a key here is trust on first use:
it is weaker than a pin, because anyone present at this first connection could
show you a different key. For production, pin the key instead:
    hoarddb://host:7433/?fingerprint=<SHA256:64-hex-digits copied from the log line>

exit status: 0 trusted (now or already), 1 not trusted (refused, end of input, no
terminal, or a changed key), 2 usage error or the server could not be reached.
"""


class _Refused(Exception):
    """The key was shown and not accepted; ``why`` is the sentence to print."""

    def __init__(self, why: str) -> None:
        super().__init__(why)
        self.why = why


def _usage_error(stderr: TextIO, problem: str) -> int:
    print(f"python -m hoarddb: {problem}\n", file=stderr)
    print(USAGE, file=stderr, end="")
    return 2


def _ask(key: ServerKey, stdin: TextIO, stdout: TextIO, where: str) -> bool:
    print(f"{key.host}:{key.port} presents the key", file=stdout)
    print(f"    {key.fingerprint}", file=stdout)
    if not stdin.isatty():
        raise _Refused(
            "stdin is not a terminal, so there is nobody to ask. Nothing was written. "
            "Run this in a terminal, or pin the key in the URI instead "
            "(?fingerprint=<SHA256:64-hex-digits copied from the server's log line>)."
        )
    print(
        "Check it against the fingerprint in the server's log. This is trust on first use.",
        file=stdout,
    )
    print(
        f"Record it in {where}? Type yes to trust it, anything else to refuse: ",
        end="",
        file=stdout,
    )
    stdout.flush()
    answer = stdin.readline()
    if answer.strip().lower() not in ("yes", "y"):
        raise _Refused("Not trusted. Nothing was written.")
    return True


def trust(target: str, stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    try:
        host = parse_host_port(target)
    except errors.ConfigError as bad:
        return _usage_error(stderr, f"{bad.message} (write the host as host:port, or [::1]:7433)")
    store = KnownServers.for_options(None)
    asked: list[ServerKey] = []

    def decide(key: ServerKey) -> bool:
        asked.append(key)
        return _ask(key, stdin, stdout, str(store.path))

    try:
        sock = connect_pinned(host, None, TlsOptions(on_unknown_server=decide), timeout=10.0)
    except _Refused as refused:
        print(refused.why, file=stdout)
        return 1
    except errors.TlsTrustError as refused:
        if asked:  # the callback answered no (or the key could not be vouched for)
            print("Not trusted. Nothing was written.", file=stdout)
        else:
            print(refused.message, file=stderr)
        return 1
    except errors.HoardDBError as problem:
        print(problem.message, file=stderr)
        return 2
    except OSError as problem:
        print(
            f"Could not connect to {host} ({problem.strerror or problem}). "
            "Is hoardDB-server running there? It listens on 127.0.0.1:7433 by default.",
            file=stderr,
        )
        return 2
    sock.close()
    if not asked:
        print(f"{host} is already trusted: its key is recorded in {store.path}.", file=stdout)
        return 0
    key = asked[0]
    if store.lookup(host) != key.key:  # the driver only logs a failed write; here it is an error
        print(
            f"The key was accepted but could not be recorded in {store.path}. "
            "Check that the directory is yours and not writable by others "
            f"(chmod 700 {store.path.parent}), then run this again.",
            file=stderr,
        )
        return 1
    print(f"Trusted {key.fingerprint} for {host}. Recorded in {store.path}.", file=stdout)
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    out, err, inp = stdout or sys.stdout, stderr or sys.stderr, stdin or sys.stdin
    if (args and args[0] in ("-h", "--help")) or (args[:1] == ["trust"] and args[1:] == ["--help"]):
        print(USAGE, file=out, end="")
        return 0
    if not args:
        return _usage_error(err, "a command is needed (the only one is `trust`)")
    if args[0] != "trust":
        return _usage_error(err, f"unknown command {args[0]!r} (the only one is `trust`)")
    if len(args) != 2:
        return _usage_error(err, "`trust` takes exactly one host, for example localhost:7433")
    return trust(args[1], inp, out, err)


if __name__ == "__main__":
    sys.exit(main())
