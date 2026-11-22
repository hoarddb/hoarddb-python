# SPDX-License-Identifier: Apache-2.0
"""P8: the asyncio connection, the same behaviour as P7's against the same fake server."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections.abc import Awaitable, Callable, Iterator, Sequence
from pathlib import Path
from typing import Any, TypeVar

import pytest

import fakehoard
import faketls
import localswalk
from hoarddb import errors
from hoarddb._aio.connection import Connection
from hoarddb._core import config, frame, handshake, messages, opcodes
from hoarddb._core.failure import Context
from hoarddb._core.interrupt import Interrupted
from hoarddb._deadline import Deadline
from hoarddb._secret import Secret
from hoarddb.options import ServerKey, TlsOptions

T = TypeVar("T")
READ = Context(operation="count", target="d/b", kind="READ")
# Module level on purpose: a local would show up in the test's own captured frame.
SECRET = "correct-horse-battery-staple-7731"
WRITE = Context(operation="put", target="d/b", kind="WRITE")


def run(coro: Callable[[], Awaitable[T]]) -> T:
    async def main() -> T:
        before = {t for t in asyncio.all_tasks() if not t.done()}
        result = await coro()
        await asyncio.sleep(0)
        leaked = (
            {t for t in asyncio.all_tasks() if not t.done()} - before - {asyncio.current_task()}
        )
        assert not leaked, f"tasks outlived the connection: {leaked}"
        return result

    return asyncio.run(main())


@pytest.fixture(scope="module")
def identities(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[faketls.Identity, faketls.Identity]:
    d = tmp_path_factory.mktemp("ids")
    return faketls.make_identity(d, "one"), faketls.make_identity(d, "two")


def options(
    server: fakehoard.FakeHoard, identity: faketls.Identity, **extra: Any
) -> config.ResolvedOptions:
    tls = extra.pop("tls", TlsOptions(fingerprints=[identity.fingerprint]))
    return config.resolve_options(
        f"hoarddb://admin:pw@127.0.0.1:{server.port}", {"tls": tls, **extra}, {}
    )


async def open_connection(
    server: fakehoard.FakeHoard,
    identity: faketls.Identity,
    seconds: float | None = 5.0,
    **extra: Any,
) -> tuple[Connection, Any]:
    opts = options(server, identity, **extra)
    auth = handshake.auth_request(opts, "password", None)
    return await Connection.open(opts.hosts[0], opts.pin_for(0), opts, auth, Deadline(seconds))


async def count(conn: Connection, seconds: float | None = 5.0, ctx: Context = READ) -> Any:
    return await conn.exchange(
        messages.CountRequest(database="d", bucket="b"), ctx, Deadline(seconds)
    )


async def settled(server: fakehoard.FakeHoard, predicate: Callable[[], bool]) -> bool:
    for _ in range(500):
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


# --- dialling and AUTH ---------------------------------------------------------------------------


def test_the_first_frame_is_a_password_auth(identities: Any) -> None:
    async def main() -> None:
        with fakehoard.FakeHoard(identities[0]) as server:
            conn, ok = await open_connection(server, identities[0], client_name="etl")
            try:
                assert ok.token is not None and ok.token.reveal() == "tok-1"
                assert conn.authenticated
                assert [f.opcode for f in server.frames] == [opcodes.AUTH]
                doc = server.connections[0].auth_requests[0]
                assert doc["username"] == "admin" and doc["password"] == "pw"
                assert doc["capabilities"] == [] and doc["client"].endswith(" (etl)")
            finally:
                conn.close()

    run(main)


def test_a_token_auth_sends_the_token_and_not_the_password(identities: Any) -> None:
    async def main() -> None:
        with fakehoard.FakeHoard(identities[0]) as server:
            opts = options(server, identities[0])
            auth = handshake.auth_request(opts, "token", Secret("tok-9"))
            conn, _ = await Connection.open(
                opts.hosts[0], opts.pin_for(0), opts, auth, Deadline(5.0)
            )
            conn.close()
            doc = server.connections[0].auth_requests[0]
            assert doc["token"] == "tok-9" and "password" not in doc

    run(main)


@pytest.mark.parametrize("case", ["wrong pin", "no alpn", "unknown key"])
def test_no_auth_byte_follows_a_refusal(tmp_path: Path, identities: Any, case: str) -> None:
    async def main() -> None:
        alpn = None if case == "no alpn" else ("hoarddb/1",)
        with fakehoard.FakeHoard(identities[0], alpn=alpn) as server:
            tls = TlsOptions(
                fingerprints=[identities[1].fingerprint] if case == "wrong pin" else [],
                known_servers_path=tmp_path / "ks",
            )
            opts = options(server, identities[0], tls=tls)
            auth = handshake.auth_request(opts, "password", None)
            with pytest.raises(errors.HoardDBError):
                await Connection.open(opts.hosts[0], opts.pin_for(0), opts, auth, Deadline(5.0))
            await settled(server, lambda: all(c.closed for c in server.connections))
            assert server.frames == []

    run(main)


def test_the_unknown_server_callback_may_be_a_coroutine_function(
    tmp_path: Path, identities: Any
) -> None:
    seen: list[ServerKey] = []

    async def approve(key: ServerKey) -> bool:
        await asyncio.sleep(0)
        seen.append(key)
        return True

    async def main() -> None:
        with fakehoard.FakeHoard(identities[0]) as server:
            tls = TlsOptions(known_servers_path=tmp_path / "ks", on_unknown_server=approve)  # type: ignore[arg-type]
            conn, _ = await open_connection(server, identities[0], tls=tls)
            conn.close()
            assert seen[0].fingerprint == identities[0].fingerprint

    run(main)


def test_an_awaitable_answer_that_is_not_true_does_not_trust_the_key(
    tmp_path: Path, identities: Any
) -> None:
    async def maybe(key: ServerKey) -> str:
        return "yes"

    async def main() -> None:
        with fakehoard.FakeHoard(identities[0]) as server:
            tls = TlsOptions(known_servers_path=tmp_path / "ks", on_unknown_server=maybe)  # type: ignore[arg-type]
            with pytest.raises(errors.TlsTrustError):
                await open_connection(server, identities[0], tls=tls)
            assert server.frames == []

    run(main)


@pytest.mark.parametrize(
    ("reason", "name"),
    [(1, "CREDENTIALS"), (2, "TOKEN_EXPIRED"), (4, "TOKEN_UNSUPPORTED"), (5, "PROTOCOL_MISMATCH"),
     (6, "RATE_LIMITED"), (7, "ALREADY_AUTHED"), (8, "INTERNAL")],
)  # fmt: skip
def test_auth_fail_is_an_authentication_error_and_the_connection_is_closed(
    identities: Any, reason: int, name: str
) -> None:
    async def main() -> None:
        with fakehoard.FakeHoard(
            identities[0], on_auth=lambda c, d: [fakehoard.auth_fail(reason)]
        ) as server:
            with pytest.raises(errors.AuthenticationError) as caught:
                await open_connection(server, identities[0])
            assert caught.value.reason == name
            assert await settled(server, lambda: server.connections[0].closed)

    run(main)


@pytest.mark.parametrize("answer", ["auth_fail", "close"])
def test_no_frame_of_a_failed_auth_traceback_holds_the_password(
    identities: Any, answer: str
) -> None:
    actions = [fakehoard.auth_fail(1)] if answer == "auth_fail" else [fakehoard.Close()]

    async def main() -> None:
        with fakehoard.FakeHoard(identities[0], on_auth=lambda c, d: actions) as server:
            opts = config.resolve_options(
                f"hoarddb://127.0.0.1:{server.port}",
                {"tls": TlsOptions(fingerprints=[identities[0].fingerprint])},
                {"HOARDDB_ROOT_PASSWORD": SECRET},
            )
            auth = handshake.auth_request(opts, "password", None)
            with pytest.raises(errors.HoardDBError) as caught:
                await Connection.open(opts.hosts[0], opts.pin_for(0), opts, auth, Deadline(5.0))
            assert localswalk.frames_showing(caught.value, SECRET) == []

    run(main)


def test_a_zero_id_overloaded_refusal_is_the_answer_to_auth(identities: Any) -> None:
    refusal = fakehoard.Reply(opcodes.ERROR, {"code": 13, "message": "full"}, bytes(16))

    async def main() -> None:
        with fakehoard.FakeHoard(identities[0], greeting=[refusal]) as server:
            with pytest.raises(errors.OverloadedError) as caught:
                await open_connection(server, identities[0])
            assert caught.value.retryable and caught.value.request_id == "00" * 16

    run(main)


def test_a_reset_instead_of_the_refusal_names_the_connection_cap(identities: Any) -> None:
    async def main() -> None:
        with fakehoard.FakeHoard(identities[0], greeting=[fakehoard.Reset()]) as server:
            with pytest.raises(errors.ConnectionFailedError) as caught:
                await open_connection(server, identities[0])
            assert "HOARDDB_TCP_MAX_CONNS_PER_IP" in str(caught.value)

    run(main)


def test_a_refused_port_is_a_connection_failed_error(identities: Any) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    opts = config.resolve_options(
        f"hoarddb://admin:pw@127.0.0.1:{port}",
        {"tls": TlsOptions(fingerprints=[identities[0].fingerprint])},
        {},
    )
    auth = handshake.auth_request(opts, "password", None)

    async def main() -> None:
        with pytest.raises(errors.ConnectionFailedError) as caught:
            await Connection.open(opts.hosts[0], None, opts, auth, Deadline(5.0))
        assert f"127.0.0.1:{port}" in str(caught.value) and "refused" in str(caught.value)

    run(main)


def test_a_silent_server_fails_the_dial_at_the_dial_timeout(identities: Any) -> None:
    async def main() -> None:
        with fakehoard.FakeHoard(identities[0], on_auth=lambda c, d: [fakehoard.Hang()]) as server:
            loop = asyncio.get_running_loop()
            started = loop.time()
            with pytest.raises(errors.ConnectionFailedError):
                await open_connection(server, identities[0], seconds=30.0, dial_timeout=0.3)
            assert loop.time() - started < 5

    run(main)


# --- the exchange --------------------------------------------------------------------------------


def test_a_ping_and_a_typed_response_round_trip(identities: Any) -> None:
    def handler(conn: fakehoard.Conn, request: frame.Frame) -> Sequence[fakehoard.Action]:
        return [fakehoard.Reply(opcodes.COUNT, {"count": 7})]

    async def main() -> None:
        with fakehoard.FakeHoard(identities[0], handler=handler) as server:
            conn, _ = await open_connection(server, identities[0])
            try:
                pong = await conn.exchange(messages.PingRequest(), READ, Deadline(5.0))
                assert isinstance(pong.response, messages.PongResponse)
                assert (await count(conn)).response.count == 7
                assert conn.healthy and conn.probe()
            finally:
                conn.close()

    run(main)


def test_an_error_frame_on_the_survivor_list_keeps_the_connection(identities: Any) -> None:
    answers: Iterator[Sequence[fakehoard.Action]] = iter(
        [[fakehoard.error(1, "bucket not found")], [fakehoard.Reply(opcodes.COUNT, {})]]
    )

    async def main() -> None:
        with fakehoard.FakeHoard(identities[0], handler=lambda c, f: next(answers)) as server:
            conn, _ = await open_connection(server, identities[0])
            try:
                with pytest.raises(errors.NotFoundError):
                    await count(conn)
                assert conn.healthy
                await count(conn)
            finally:
                conn.close()

    run(main)


@pytest.mark.parametrize("code", [3, 4, 6, 8, 13, 15, 5])
def test_an_error_frame_off_the_survivor_list_discards_the_connection(
    identities: Any, code: int
) -> None:
    async def main() -> None:
        with fakehoard.FakeHoard(
            identities[0], handler=lambda c, f: [fakehoard.error(code, "refused")]
        ) as server:
            conn, _ = await open_connection(server, identities[0])
            with pytest.raises(errors.HoardDBError):
                await count(conn)
            assert not conn.healthy
            assert await settled(server, lambda: server.connections[0].closed)

    run(main)


def test_a_silent_server_is_a_timeout_and_the_connection_is_discarded(identities: Any) -> None:
    async def main() -> None:
        with fakehoard.FakeHoard(identities[0], handler=lambda c, f: [fakehoard.Hang()]) as server:
            conn, _ = await open_connection(server, identities[0])
            loop = asyncio.get_running_loop()
            started = loop.time()
            with pytest.raises(Interrupted) as caught:
                await count(conn, seconds=0.3)
            assert caught.value.cause == "timeout" and caught.value.sent is True
            assert 0.25 < loop.time() - started < 3
            assert not conn.healthy

    run(main)


def test_a_deadline_already_past_sends_nothing(identities: Any) -> None:
    async def main() -> None:
        with fakehoard.FakeHoard(identities[0]) as server:
            conn, _ = await open_connection(server, identities[0])
            with pytest.raises(Interrupted) as caught:
                await count(conn, seconds=0.0)
            assert caught.value.cause == "timeout" and caught.value.sent is False
            conn.close()
            await settled(server, lambda: server.connections[0].closed)
            assert [f.opcode for f in server.frames] == [opcodes.AUTH]

    run(main)


@pytest.mark.parametrize("how", ["close before reply", "close mid frame"])
def test_a_connection_lost_after_the_write_is_lost_and_sent(identities: Any, how: str) -> None:
    half = frame.encode_frame(opcodes.PONG, bytes(16), b"")[:10]
    actions: list[fakehoard.Action] = (
        [fakehoard.Close()]
        if how == "close before reply"
        else [fakehoard.Raw(half), fakehoard.Close()]
    )

    async def main() -> None:
        with fakehoard.FakeHoard(identities[0], handler=lambda c, f: actions) as server:
            conn, _ = await open_connection(server, identities[0])
            with pytest.raises(Interrupted) as caught:
                await count(conn)
            assert caught.value.cause == "lost" and caught.value.sent is True
            assert not conn.healthy

    run(main)


def test_another_request_id_and_a_zero_id_frame_are_protocol_errors(identities: Any) -> None:
    async def main() -> None:
        for rid, needle in ((b"z" * 16, "matches nothing"), (bytes(16), "zero request ID")):
            answer: list[fakehoard.Action] = [fakehoard.Reply(opcodes.PONG, b"", rid)]

            def handler(
                c: fakehoard.Conn, f: frame.Frame, a: list[fakehoard.Action] = answer
            ) -> Sequence[fakehoard.Action]:
                return a

            with fakehoard.FakeHoard(identities[0], handler=handler) as server:
                conn, _ = await open_connection(server, identities[0])
                with pytest.raises(errors.ProtocolError) as caught:
                    await count(conn, ctx=WRITE)
                assert needle in str(caught.value) and caught.value.write_outcome == "unknown"
                assert not conn.healthy

    run(main)


@pytest.mark.parametrize(
    "prefix", [b"\x01\xff\xff\xff\xff", b"\x01\x10\x00\x00\x00", b"\x09\x11\x00\x00\x00"]
)
def test_a_hostile_frame_prefix_is_a_protocol_error(identities: Any, prefix: bytes) -> None:
    async def main() -> None:
        with fakehoard.FakeHoard(
            identities[0], handler=lambda c, f: [fakehoard.Raw(prefix)]
        ) as server:
            conn, _ = await open_connection(server, identities[0])
            with pytest.raises(errors.ProtocolError):
                await count(conn)
            assert not conn.healthy

    run(main)


BAD_ANSWERS: list[tuple[str, Any]] = [
    ("bad-version", lambda: fakehoard.Raw(b"\x09\x11\x00\x00\x00")),
    ("length-out-of-range", lambda: fakehoard.Raw(b"\x01\xff\xff\xff\xff")),
    ("malformed-error-payload", lambda: fakehoard.Reply(opcodes.ERROR, b"\x05\x00\x00\x00\x01")),
    ("opcode-mismatch", lambda: fakehoard.Reply(opcodes.EXISTS_RESPONSE, {})),
]
DDL = Context(operation="create_database", target="app", kind="DDL")


@pytest.mark.parametrize(("name", "answer"), BAD_ANSWERS, ids=[n for n, _ in BAD_ANSWERS])
@pytest.mark.parametrize(
    ("ctx", "request_", "unknown"),
    [
        (WRITE, messages.PutRequest(database="d", bucket="b", key="k", data={}), "read it back"),
        (DDL, messages.CreateDbRequest(name="app"), "list_buckets"),
        (READ, messages.CountRequest(database="d", bucket="b"), None),
    ],
    ids=["write", "ddl", "read"],
)
def test_a_protocol_error_after_a_write_was_sent_says_the_outcome_is_unknown(
    identities: Any, name: str, answer: Any, ctx: Context, request_: Any, unknown: str | None
) -> None:
    async def main() -> None:
        with fakehoard.FakeHoard(identities[0], handler=lambda c, f: [answer()]) as server:
            conn, _ = await open_connection(server, identities[0])
            with pytest.raises(errors.ProtocolError) as caught:
                await conn.exchange(request_, ctx, Deadline(5.0))
            err = caught.value
            assert err.operation == ctx.operation
            assert not conn.healthy
            if unknown is None:
                assert err.write_outcome is None and err.hint is None
            else:
                assert err.write_outcome == "unknown"
                assert err.hint is not None and unknown in err.hint
                assert "The outcome is unknown" in str(err)

    run(main)


def test_an_oversize_or_unencodable_request_is_refused_before_a_byte_is_written(
    identities: Any,
) -> None:
    async def main() -> None:
        with fakehoard.FakeHoard(identities[0]) as server:
            conn, _ = await open_connection(server, identities[0])
            try:
                big = {"blob": b"x" * (frame.MAX_LENGTH + 1)}
                with pytest.raises(errors.FrameTooLargeError):
                    await conn.exchange(
                        messages.PutRequest(database="d", bucket="b", key="k", data=big),
                        WRITE,
                        Deadline(5.0),
                    )
                with pytest.raises(errors.InvalidArgumentError):
                    await conn.exchange(
                        messages.PutRequest(
                            database="d", bucket="b", key="k", data={"x": object()}
                        ),
                        WRITE,
                        Deadline(5.0),
                    )
                assert conn.healthy
                assert [f.opcode for f in server.frames] == [opcodes.AUTH]
            finally:
                conn.close()

    run(main)


# --- cancellation, the loop, liveness ------------------------------------------------------------


@pytest.mark.parametrize(("ctx", "warns"), [(READ, False), (WRITE, True)])
def test_cancellation_aborts_the_connection_is_never_swallowed_and_a_write_warns(
    identities: Any, ctx: Context, warns: bool, caplog: pytest.LogCaptureFixture
) -> None:
    async def main() -> None:
        with fakehoard.FakeHoard(identities[0], handler=lambda c, f: [fakehoard.Hang()]) as server:
            conn, _ = await open_connection(server, identities[0])
            task = asyncio.ensure_future(count(conn, seconds=30.0, ctx=ctx))
            await settled(server, lambda: len(server.frames) == 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not conn.healthy

    with caplog.at_level(logging.WARNING, logger="hoarddb"):
        run(main)
    cancelled = [r for r in caplog.records if "cancelled" in r.getMessage()]
    assert bool(cancelled) is warns
    if warns:
        assert "unknown" in cancelled[0].getMessage() and "shield" in cancelled[0].getMessage()


def test_a_connection_used_from_a_second_loop_is_a_config_error_naming_its_loop(
    identities: Any,
) -> None:
    async def second(conn: Connection) -> None:
        with pytest.raises(errors.ConfigError, match="event loop"):
            await count(conn)

    async def first() -> None:
        with fakehoard.FakeHoard(identities[0]) as server:
            conn, _ = await open_connection(server, identities[0])
            try:
                await asyncio.to_thread(lambda: asyncio.run(second(conn)))
                assert conn.healthy  # refusing the wrong loop does not damage the connection
            finally:
                conn.close()

    run(first)


def test_a_freshly_authenticated_connection_passes_the_probe_and_a_close_fails_it(
    identities: Any,
) -> None:
    async def main() -> None:
        with fakehoard.FakeHoard(identities[0]) as server:
            for _ in range(5):
                conn, _ = await open_connection(server, identities[0])
                assert conn.probe()
                conn.close()
            gone, _ = await open_connection(server, identities[0])
            server.connections[-1].sock.shutdown(socket.SHUT_RDWR)
            assert await settled(server, lambda: not gone.probe())
            gone.close()

    run(main)


def test_unsolicited_bytes_on_an_idle_connection_are_a_violation(identities: Any) -> None:
    async def main() -> None:
        with fakehoard.FakeHoard(identities[0]) as server:
            conn, _ = await open_connection(server, identities[0])
            server.connections[0].sock.sendall(frame.encode_frame(opcodes.PONG, bytes(16), b""))
            assert await settled(server, lambda: not conn.probe())
            conn.close()

    run(main)


def test_a_forked_close_leaves_the_parents_connection_working(identities: Any) -> None:
    async def main() -> None:
        with fakehoard.FakeHoard(
            identities[0], handler=lambda c, f: [fakehoard.Reply(opcodes.COUNT, {"count": 1})]
        ) as server:
            conn, _ = await open_connection(server, identities[0])
            try:
                pid = os.fork()
                if pid == 0:
                    try:
                        conn.close(forked=True)
                    finally:
                        os._exit(0)
                _, status = os.waitpid(pid, 0)
                assert os.WEXITSTATUS(status) == 0
                assert (await count(conn)).response.count == 1
            finally:
                conn.close()

    run(main)
