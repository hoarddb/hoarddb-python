# SPDX-License-Identifier: Apache-2.0
"""P7: the blocking connection against a fake hoardDB server (TLS, frames, AUTH, deadlines)."""

from __future__ import annotations

import os
import resource
import select
import socket
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

import fakehoard
import faketls
import localswalk
from hoarddb import errors
from hoarddb._bson import deserialize
from hoarddb._core import config, frame, handshake, messages, opcodes
from hoarddb._core.failure import Context
from hoarddb._core.interrupt import Interrupted
from hoarddb._core.uri import HostPort
from hoarddb._deadline import Deadline
from hoarddb._netutil import readable, tune_socket
from hoarddb._secret import Secret
from hoarddb._sync import connection as connection_module
from hoarddb._sync.connection import Connection
from hoarddb.options import TlsOptions

READ = Context(operation="ping", target="t", kind="READ")
# Module level on purpose: a local would show up in the test's own captured frame.
SECRET = "correct-horse-battery-staple-7731"
WRITE = Context(operation="put", target="db/b", kind="WRITE")


@pytest.fixture(scope="module")
def identities(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[faketls.Identity, faketls.Identity]:
    d = tmp_path_factory.mktemp("ids")
    return faketls.make_identity(d, "one"), faketls.make_identity(d, "two")


@pytest.fixture
def server(identities: Any) -> Iterator[fakehoard.FakeHoard]:
    with fakehoard.FakeHoard(identities[0]) as s:
        yield s


def options(
    server: fakehoard.FakeHoard, identity: faketls.Identity, **extra: Any
) -> config.ResolvedOptions:
    tls = extra.pop("tls", TlsOptions(fingerprints=[identity.fingerprint]))
    return config.resolve_options(
        f"hoarddb://admin:pw@127.0.0.1:{server.port}", {"tls": tls, **extra}, {}
    )


def open_connection(
    server: fakehoard.FakeHoard,
    identity: faketls.Identity,
    seconds: float | None = 5.0,
    **extra: Any,
) -> tuple[Connection, Any]:
    opts = options(server, identity, **extra)
    auth = handshake.auth_request(opts, "password", None)
    return Connection.open(opts.hosts[0], opts.pin_for(0), opts, auth, Deadline(seconds))


def ping(conn: Connection, seconds: float | None = 5.0) -> Any:
    return conn.exchange(messages.PingRequest(), READ, Deadline(seconds))


def count(conn: Connection, seconds: float | None = 5.0) -> Any:
    """A request the fake server hands to the test's handler (it answers PING itself)."""
    return conn.exchange(messages.CountRequest(database="d", bucket="b"), READ, Deadline(seconds))


# --- dialling and AUTH ---------------------------------------------------------------------------


def test_the_first_frame_is_a_password_auth_and_nothing_precedes_it(
    server: fakehoard.FakeHoard, identities: Any
) -> None:
    conn, ok = open_connection(server, identities[0])
    try:
        assert ok.success and ok.token is not None and ok.token.reveal() == "tok-1"
        assert conn.authenticated
        assert [f.opcode for f in server.connections[0].frames] == [opcodes.AUTH]
        doc = server.connections[0].auth_requests[0]
        assert doc["username"] == "admin" and doc["password"] == "pw"
        assert doc["protocol_version"] == 1 and doc["capabilities"] == []
        assert doc["client"].startswith("hoarddb-python/")
        assert "token" not in doc
    finally:
        conn.close()


def test_a_token_auth_sends_the_token_and_not_the_password(
    server: fakehoard.FakeHoard, identities: Any
) -> None:
    opts = options(server, identities[0])
    auth = handshake.auth_request(opts, "token", Secret("tok-9"))
    conn, _ = Connection.open(opts.hosts[0], opts.pin_for(0), opts, auth, Deadline(5.0))
    conn.close()
    doc = server.connections[0].auth_requests[0]
    assert doc["token"] == "tok-9" and "password" not in doc


def test_client_name_is_appended_to_the_client_string(
    server: fakehoard.FakeHoard, identities: Any
) -> None:
    conn, _ = open_connection(server, identities[0], client_name="etl")
    conn.close()
    assert server.connections[0].auth_requests[0]["client"].endswith(" (etl)")


@pytest.mark.parametrize("case", ["wrong pin", "no alpn", "unknown key", "insecure not asked"])
def test_no_auth_byte_follows_a_refusal(tmp_path: Path, identities: Any, case: str) -> None:
    """The other half of P6's test: through the whole connection, a refused key means the server
    reads zero application bytes, so the password never left the process."""
    alpn = () if case == "no alpn" else ("hoarddb/1",)
    with fakehoard.FakeHoard(identities[0], alpn=alpn or None) as server:
        tls = TlsOptions(
            fingerprints=[identities[1].fingerprint] if case == "wrong pin" else [],
            known_servers_path=tmp_path / "ks",
        )
        opts = options(server, identities[0], tls=tls)
        auth = handshake.auth_request(opts, "password", None)
        with pytest.raises(errors.HoardDBError):
            Connection.open(opts.hosts[0], opts.pin_for(0), opts, auth, Deadline(5.0))
        server.wait_for(lambda: all(c.closed for c in server.connections))
        assert server.frames == []
        assert all(not c.auth_requests for c in server.connections)


@pytest.mark.parametrize(
    ("reason", "name"),
    [(1, "CREDENTIALS"), (2, "TOKEN_EXPIRED"), (4, "TOKEN_UNSUPPORTED"), (5, "PROTOCOL_MISMATCH"),
     (6, "RATE_LIMITED"), (7, "ALREADY_AUTHED"), (8, "INTERNAL")],
)  # fmt: skip
def test_auth_fail_is_an_authentication_error_and_the_connection_is_closed(
    identities: Any, reason: int, name: str
) -> None:
    with fakehoard.FakeHoard(
        identities[0], on_auth=lambda c, d: [fakehoard.auth_fail(reason)]
    ) as server:
        with pytest.raises(errors.AuthenticationError) as caught:
            open_connection(server, identities[0])
        assert caught.value.reason == name
        assert caught.value.retryable is (name in ("RATE_LIMITED", "INTERNAL"))
        assert server.wait_for(lambda: server.connections[0].closed)
        assert "pw" not in str(caught.value)


class _FailingWrites:
    """Stands in for the TLS object of a socket whose write fails, as a reset or a stall would."""

    def __init__(self, real: Any, failure: Exception) -> None:
        self._real = real
        self._failure = failure

    def write(self, data: Any) -> int:
        raise self._failure

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


@pytest.mark.parametrize("answer", ["auth_fail", "close", "send_reset", "send_timeout"])
def test_no_frame_of_a_failed_auth_traceback_holds_the_password(
    identities: Any, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    actions = [fakehoard.auth_fail(1)] if answer == "auth_fail" else [fakehoard.Close()]
    if answer.startswith("send_"):
        failure: Exception = (
            ConnectionResetError(104, "Connection reset by peer")
            if answer == "send_reset"
            else TimeoutError("timed out")
        )
        real_tune = tune_socket

        def tune_then_break_writes(sock: Any) -> None:
            real_tune(sock)
            sock._sslobj = _FailingWrites(sock._sslobj, failure)

        monkeypatch.setattr(connection_module, "tune_socket", tune_then_break_writes, raising=True)
    with fakehoard.FakeHoard(identities[0], on_auth=lambda c, d: actions) as server:
        opts = config.resolve_options(
            f"hoarddb://127.0.0.1:{server.port}",
            {"tls": TlsOptions(fingerprints=[identities[0].fingerprint])},
            {"HOARDDB_ROOT_PASSWORD": SECRET},
        )
        auth = handshake.auth_request(opts, "password", None)
        with pytest.raises(errors.HoardDBError) as caught:
            Connection.open(opts.hosts[0], opts.pin_for(0), opts, auth, Deadline(5.0))
        assert localswalk.frames_showing(caught.value, SECRET) == []


def test_an_empty_auth_fail_payload_is_credentials(identities: Any) -> None:
    with fakehoard.FakeHoard(
        identities[0], on_auth=lambda c, d: [fakehoard.Reply(opcodes.AUTH_FAIL, b"")]
    ) as server:
        with pytest.raises(errors.AuthenticationError) as caught:
            open_connection(server, identities[0])
        assert caught.value.reason == "CREDENTIALS"


def test_a_zero_id_overloaded_refusal_at_accept_time_is_the_answer_to_auth(identities: Any) -> None:
    refusal = fakehoard.error(13, "too many connections from this address")
    with fakehoard.FakeHoard(
        identities[0], greeting=[fakehoard.Reply(opcodes.ERROR, refusal.payload, bytes(16))]
    ) as server:
        with pytest.raises(errors.OverloadedError) as caught:
            open_connection(server, identities[0])
        assert caught.value.retryable is True
        assert caught.value.request_id == "00" * 16
        assert caught.value.write_outcome is None  # nothing the call carried was processed
        assert "pool=" in str(caught.value)  # the hint says how to lower the pool


def test_a_reset_instead_of_the_refusal_names_the_connection_cap(identities: Any) -> None:
    with fakehoard.FakeHoard(identities[0], greeting=[fakehoard.Reset()]) as server:
        with pytest.raises(errors.ConnectionFailedError) as caught:
            open_connection(server, identities[0])
        assert "HOARDDB_TCP_MAX_CONNS_PER_IP" in str(caught.value)
        assert caught.value.retryable is True


def test_a_refused_port_is_a_connection_failed_error_naming_the_host(identities: Any) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()  # nothing listens here now
    opts = config.resolve_options(
        f"hoarddb://admin:pw@127.0.0.1:{port}",
        {"tls": TlsOptions(fingerprints=[identities[0].fingerprint])},
        {},
    )
    auth = handshake.auth_request(opts, "password", None)
    with pytest.raises(errors.ConnectionFailedError) as caught:
        Connection.open(opts.hosts[0], None, opts, auth, Deadline(5.0))
    assert f"127.0.0.1:{port}" in str(caught.value)
    assert "refused" in str(caught.value)


def test_a_silent_server_fails_the_dial_at_the_dial_timeout(identities: Any) -> None:
    with fakehoard.FakeHoard(identities[0], on_auth=lambda c, d: [fakehoard.Hang()]) as server:
        started = time.monotonic()
        with pytest.raises(errors.ConnectionFailedError):
            open_connection(server, identities[0], seconds=30.0, dial_timeout=0.3)
        assert time.monotonic() - started < 5


def test_auth_answered_with_an_unrelated_opcode_is_a_protocol_error(identities: Any) -> None:
    with (
        fakehoard.FakeHoard(
            identities[0], on_auth=lambda c, d: [fakehoard.Reply(opcodes.PONG)]
        ) as server,
        pytest.raises(errors.ProtocolError),
    ):
        open_connection(server, identities[0])


def test_auth_answered_with_another_request_id_is_a_protocol_error(identities: Any) -> None:
    with (
        fakehoard.FakeHoard(
            identities[0],
            on_auth=lambda c, d: [fakehoard.Reply(opcodes.AUTH_OK, {"success": True}, b"x" * 16)],
        ) as server,
        pytest.raises(errors.ProtocolError),
    ):
        open_connection(server, identities[0])


# --- the exchange --------------------------------------------------------------------------------


def test_a_ping_is_answered_and_the_connection_is_reusable(
    server: fakehoard.FakeHoard, identities: Any
) -> None:
    conn, _ = open_connection(server, identities[0])
    try:
        for _ in range(3):
            reply = ping(conn)
            assert isinstance(reply.response, messages.PongResponse)
            assert len(reply.request_id) == 16
        ids = [f.request_id for f in server.frames_with(opcodes.PING)]
        assert len(set(ids)) == 3
    finally:
        conn.close()


def test_a_typed_response_is_decoded(identities: Any) -> None:
    def handler(conn: fakehoard.Conn, request: frame.Frame) -> list[fakehoard.Action]:
        assert request.opcode == opcodes.COUNT
        assert deserialize(request.payload) == {"database": "d", "bucket": "b"}
        return [fakehoard.Reply(opcodes.COUNT, {"count": 7})]

    with fakehoard.FakeHoard(identities[0], handler=handler) as server:
        conn, _ = open_connection(server, identities[0])
        try:
            reply = conn.exchange(
                messages.CountRequest(database="d", bucket="b"), READ, Deadline(5.0)
            )
            assert reply.response.count == 7
        finally:
            conn.close()


def test_an_error_frame_on_the_survivor_list_keeps_the_connection(identities: Any) -> None:
    answers: Iterator[Sequence[fakehoard.Action]] = iter(
        [[fakehoard.error(1, "bucket not found")], [fakehoard.Reply(opcodes.COUNT, {})]]
    )
    with fakehoard.FakeHoard(identities[0], handler=lambda c, f: next(answers)) as server:
        conn, _ = open_connection(server, identities[0])
        try:
            with pytest.raises(errors.NotFoundError):
                conn.exchange(messages.CountRequest(database="d", bucket="b"), READ, Deadline(5))
            assert conn.healthy
            conn.exchange(messages.CountRequest(database="d", bucket="b"), READ, Deadline(5))
        finally:
            conn.close()


@pytest.mark.parametrize("code", [3, 4, 6, 8, 13, 15, 5])
def test_an_error_frame_off_the_survivor_list_discards_the_connection(
    identities: Any, code: int
) -> None:
    with fakehoard.FakeHoard(
        identities[0], handler=lambda c, f: [fakehoard.error(code, "refused")]
    ) as server:
        conn, _ = open_connection(server, identities[0])
        with pytest.raises(errors.HoardDBError):
            conn.exchange(messages.CountRequest(database="d", bucket="b"), READ, Deadline(5.0))
        assert not conn.healthy
        assert server.wait_for(lambda: server.connections[0].closed)


def test_a_node_to_node_opcode_refusal_does_not_leave_a_reusable_connection(
    identities: Any,
) -> None:
    def handler(conn: fakehoard.Conn, request: frame.Frame) -> list[fakehoard.Action]:
        return [
            fakehoard.error(4, "opcode GOSSIP is not served on the driver port"),
            fakehoard.Close(),
        ]

    with fakehoard.FakeHoard(identities[0], handler=handler) as server:
        conn, _ = open_connection(server, identities[0])
        with pytest.raises(errors.InvalidArgumentError):
            conn.exchange(messages.CountRequest(database="d", bucket="b"), READ, Deadline(5.0))
        assert not conn.healthy
        assert not conn.probe()


def test_a_write_error_carries_the_servers_write_outcome(identities: Any) -> None:
    with fakehoard.FakeHoard(
        identities[0],
        handler=lambda c, f: [fakehoard.error(6, "no holder", write_outcome="not_applied")],
    ) as server:
        conn, _ = open_connection(server, identities[0])
        with pytest.raises(errors.UnavailableError) as caught:
            conn.exchange(
                messages.PutRequest(database="d", bucket="b", key="k", data={"a": 1}),
                WRITE,
                Deadline(5.0),
            )
        assert caught.value.write_outcome == "not_applied" and caught.value.retryable


def test_a_silent_server_is_a_timeout_and_the_connection_is_discarded(identities: Any) -> None:
    with fakehoard.FakeHoard(identities[0], handler=lambda c, f: [fakehoard.Hang()]) as server:
        conn, _ = open_connection(server, identities[0])
        started = time.monotonic()
        with pytest.raises(Interrupted) as caught:
            count(conn, seconds=0.3)
        assert caught.value.cause == "timeout" and caught.value.sent is True
        assert 0.25 < time.monotonic() - started < 3
        assert not conn.healthy


def test_a_deadline_already_past_sends_nothing(identities: Any) -> None:
    with fakehoard.FakeHoard(identities[0]) as server:
        conn, _ = open_connection(server, identities[0])
        with pytest.raises(Interrupted) as caught:
            ping(conn, seconds=0.0)
        assert caught.value.cause == "timeout" and caught.value.sent is False
        server.wait_for(lambda: server.connections[0].closed)
        assert [f.opcode for f in server.frames] == [opcodes.AUTH]


@pytest.mark.parametrize("how", ["close before reply", "close mid frame"])
def test_a_connection_lost_after_the_write_is_reported_as_lost_and_sent(
    identities: Any, how: str
) -> None:
    half = frame.encode_frame(opcodes.PONG, bytes(16), b"")[:10]
    actions: list[fakehoard.Action] = (
        [fakehoard.Close()]
        if how == "close before reply"
        else [fakehoard.Raw(half), fakehoard.Close()]
    )
    with fakehoard.FakeHoard(identities[0], handler=lambda c, f: actions) as server:
        conn, _ = open_connection(server, identities[0])
        with pytest.raises(Interrupted) as caught:
            count(conn)
        assert caught.value.cause == "lost" and caught.value.sent is True
        assert not conn.healthy


def test_a_frame_with_another_request_id_is_a_protocol_error_and_not_resent(
    identities: Any,
) -> None:
    with fakehoard.FakeHoard(
        identities[0], handler=lambda c, f: [fakehoard.Reply(opcodes.PONG, b"", b"z" * 16)]
    ) as server:
        conn, _ = open_connection(server, identities[0])
        with pytest.raises(errors.ProtocolError) as caught:
            conn.exchange(
                messages.PutRequest(database="d", bucket="b", key="k", data={}), WRITE, Deadline(5)
            )
        assert caught.value.write_outcome == "unknown"
        assert not conn.healthy


def test_a_zero_id_frame_on_an_authenticated_connection_is_a_protocol_error(
    identities: Any,
) -> None:
    refusal = fakehoard.Reply(opcodes.ERROR, {"code": 13, "message": "full"}, bytes(16))
    with fakehoard.FakeHoard(identities[0], handler=lambda c, f: [refusal]) as server:
        conn, _ = open_connection(server, identities[0])
        with pytest.raises(errors.ProtocolError) as caught:
            count(conn)
        assert "zero request ID" in str(caught.value)
        assert caught.value.write_outcome is None  # a read
        assert not conn.healthy


def test_an_answer_with_the_wrong_opcode_is_a_protocol_error(identities: Any) -> None:
    with fakehoard.FakeHoard(
        identities[0], handler=lambda c, f: [fakehoard.Reply(opcodes.EXISTS_RESPONSE, {})]
    ) as server:
        conn, _ = open_connection(server, identities[0])
        with pytest.raises(errors.ProtocolError):
            conn.exchange(messages.CountRequest(database="d", bucket="b"), READ, Deadline(5.0))
        assert not conn.healthy


@pytest.mark.parametrize(
    "prefix", [b"\x01\xff\xff\xff\xff", b"\x01\x10\x00\x00\x00", b"\x09\x11\x00\x00\x00"]
)
def test_a_hostile_frame_prefix_is_a_protocol_error_before_any_body_is_allocated(
    identities: Any, prefix: bytes
) -> None:
    with fakehoard.FakeHoard(identities[0], handler=lambda c, f: [fakehoard.Raw(prefix)]) as server:
        conn, _ = open_connection(server, identities[0])
        with pytest.raises(errors.ProtocolError):
            count(conn)
        assert not conn.healthy


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
    with fakehoard.FakeHoard(identities[0], handler=lambda c, f: [answer()]) as server:
        conn, _ = open_connection(server, identities[0])
        with pytest.raises(errors.ProtocolError) as caught:
            conn.exchange(request_, ctx, Deadline(5.0))
        err = caught.value
        assert err.operation == ctx.operation
        assert not conn.healthy
        if unknown is None:
            assert err.write_outcome is None and err.hint is None
        else:
            assert err.write_outcome == "unknown"
            assert err.hint is not None and unknown in err.hint
            assert "The outcome is unknown" in str(err)


def test_an_oversize_request_is_refused_before_a_byte_is_written(identities: Any) -> None:
    with fakehoard.FakeHoard(identities[0]) as server:
        conn, _ = open_connection(server, identities[0])
        try:
            big = {"blob": b"x" * (frame.MAX_LENGTH + 1)}
            with pytest.raises(errors.FrameTooLargeError):
                conn.exchange(
                    messages.PutRequest(database="d", bucket="b", key="k", data=big),
                    WRITE,
                    Deadline(5.0),
                )
            assert conn.healthy  # nothing was written, so the connection is fine
            ping(conn)
            assert [f.opcode for f in server.frames] == [opcodes.AUTH, opcodes.PING]
        finally:
            conn.close()


def test_an_unencodable_document_is_refused_before_a_byte_is_written(identities: Any) -> None:
    with fakehoard.FakeHoard(identities[0]) as server:
        conn, _ = open_connection(server, identities[0])
        try:
            with pytest.raises(errors.InvalidArgumentError):
                conn.exchange(
                    messages.PutRequest(database="d", bucket="b", key="k", data={"x": object()}),
                    WRITE,
                    Deadline(5.0),
                )
            assert conn.healthy
        finally:
            conn.close()


@pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit, GeneratorExit, RuntimeError])
def test_any_exit_that_did_not_complete_the_exchange_closes_the_connection(
    identities: Any, exc: type[BaseException], monkeypatch: pytest.MonkeyPatch
) -> None:
    with fakehoard.FakeHoard(identities[0]) as server:
        conn, _ = open_connection(server, identities[0])

        def boom(self: Connection, deadline: Deadline) -> frame.Frame:
            raise exc

        monkeypatch.setattr(Connection, "_read_frame", boom)
        with pytest.raises(exc):
            ping(conn)
        assert not conn.healthy
        assert server.wait_for(lambda: server.connections[0].closed)


def test_a_discarded_connection_refuses_further_use(identities: Any) -> None:
    with fakehoard.FakeHoard(identities[0]) as server:
        conn, _ = open_connection(server, identities[0])
        conn.close()
        with pytest.raises(errors.InternalError):
            ping(conn)


# --- liveness ------------------------------------------------------------------------------------


def test_a_freshly_authenticated_connection_is_not_discarded_by_the_probe(identities: Any) -> None:
    """Right after the handshake the unread session tickets make a socket readable; after AUTH
    they are consumed (spec 6.2 item 1)."""
    with fakehoard.FakeHoard(identities[0]) as server:
        for _ in range(10):
            conn, _ = open_connection(server, identities[0])
            assert conn.probe()
            conn.close()


def test_the_probe_sees_a_close_and_unsolicited_bytes(identities: Any) -> None:
    with fakehoard.FakeHoard(identities[0]) as server:
        gone, _ = open_connection(server, identities[0])
        server.connections[0].sock.shutdown(socket.SHUT_RDWR)
        assert server.wait_for(lambda: not gone.probe())
        gone.close()

        chatty, _ = open_connection(server, identities[0])
        server.connections[1].sock.sendall(frame.encode_frame(opcodes.PONG, bytes(16), b""))
        assert server.wait_for(lambda: not chatty.probe())
        chatty.close()


def test_the_probe_sees_bytes_already_buffered_in_the_tls_object(identities: Any) -> None:
    """Two frames in one TLS record: the second is decrypted but unread, the socket is not
    readable, and only ``pending()`` shows it (an unsolicited frame is a protocol violation)."""
    twice = frame.encode_frame(opcodes.COUNT, bytes(16), b"")
    with fakehoard.FakeHoard(identities[0]) as server:

        def handler(conn: fakehoard.Conn, request: frame.Frame) -> list[fakehoard.Action]:
            one = frame.encode_frame(opcodes.COUNT, request.request_id, b"")
            return [fakehoard.Raw(one + twice)]

        server.handler = handler
        conn, _ = open_connection(server, identities[0])
        try:
            count(conn)
            assert conn.healthy
            assert conn._sock.pending() > 0  # pyright: ignore[reportPrivateUsage]
            assert not conn.probe()
        finally:
            conn.close()


def test_the_probe_works_with_a_descriptor_above_1024(identities: Any) -> None:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = 1600
    if hard != resource.RLIM_INFINITY and hard < wanted + 10:
        pytest.skip(f"RLIMIT_NOFILE hard limit {hard} is too low for a descriptor above 1024")
    if soft < wanted + 10:
        resource.setrlimit(resource.RLIMIT_NOFILE, (wanted + 10, hard))
    with fakehoard.FakeHoard(identities[0]) as server:
        conn, _ = open_connection(server, identities[0])
        try:
            high = os.dup2(conn._sock.fileno(), wanted)  # pyright: ignore[reportPrivateUsage]
            try:
                assert high >= 1024
                assert readable(high) is False  # select() would raise ValueError here
                with pytest.raises(ValueError, match="out of range"):
                    select.select([high], [], [], 0)
            finally:
                os.close(high)
            assert conn.probe()
        finally:
            conn.close()
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def test_a_forked_close_never_shuts_down_the_inherited_socket(identities: Any) -> None:
    """The child merely drops its descriptor; the parent's connection still answers."""
    with fakehoard.FakeHoard(identities[0]) as server:
        conn, _ = open_connection(server, identities[0])
        try:
            pid = os.fork()
            if pid == 0:  # the child: drop the inherited connection and leave at once
                try:
                    conn.close(forked=True)
                finally:
                    os._exit(0)
            _, status = os.waitpid(pid, 0)
            assert os.WEXITSTATUS(status) == 0
            ping(conn)  # the parent's TLS session is intact
            assert conn.probe()
        finally:
            conn.close()


def test_the_socket_options_of_a_connection(identities: Any) -> None:
    with fakehoard.FakeHoard(identities[0]) as server:
        conn, _ = open_connection(server, identities[0])
        try:
            sock = conn._sock  # pyright: ignore[reportPrivateUsage]
            assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
            assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
            if hasattr(socket, "TCP_KEEPIDLE"):
                assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE) == 60
        finally:
            conn.close()


def test_a_connection_to_a_host_is_remembered_as_that_host(
    server: fakehoard.FakeHoard, identities: Any
) -> None:
    conn, _ = open_connection(server, identities[0])
    try:
        assert conn.host == HostPort("127.0.0.1", server.port)
    finally:
        conn.close()
