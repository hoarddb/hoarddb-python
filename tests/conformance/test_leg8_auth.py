# SPDX-License-Identifier: Apache-2.0
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false, reportUnknownLambdaType=false
"""Leg 8: authentication, on servers of their own (``auth.md``): failed logins count against a
per-address limiter, so no test here shares a server with another test.

A user cannot be created or dropped from here: the handbook does not document the ``USER_*``
payloads and they need a client-side Argon2id credential (Q16). So "a dropped user's pooled
connections close" is covered by its closest neighbour: the credential behind a live pool changes
(``-reset-password``, offline), the server restarts, and the next call reports the refusal.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

import faketls
import hoarddb
from clientkit import PC
from hoarddb import errors
from hoarddb._bson import serialize
from hoarddb._core import frame, opcodes
from hoarddb._deadline import Deadline
from realkit import RESULT_LINES, Rig, authenticate, decode
from serverkit import ROOT_PASSWORD, ROOT_USER, Node
from tapkit import RawConn, Tap, password_auth_payload

pytestmark = pytest.mark.server

SHORT_TOKENS = {"HOARDDB_TOKEN_TTL": "1", "HOARDDB_AUTH_SKEW": "0"}


def log_in(
    mode: str, node: Node, tap: Tap, identity: faketls.Identity, password: str, **options: Any
) -> PC:
    uri = f"hoarddb://{ROOT_USER}:{password}@127.0.0.1:{tap.port}"
    return PC(mode, tap, identity, uri=uri, request_timeout=10.0, **options)


def auth_transcript(tap: Tap) -> list[tuple[str, str]]:
    """Every ``AUTH`` the driver sent, oldest first, as (credential, how it ended): the
    credential is ``password`` or ``token`` (a token ``AUTH`` carries the token), the ending
    ``ok`` or the refusal's reason name (``AUTH_FAIL`` reason codes of auth.md)."""
    names = {1: "CREDENTIALS", 2: "TOKEN_EXPIRED", 3: "TOKEN_INVALID", 4: "TOKEN_UNSUPPORTED"}
    names |= {5: "PROTOCOL_MISMATCH", 6: "RATE_LIMITED", 7: "ALREADY_AUTHED", 8: "INTERNAL"}
    answers = {f.request_id: f for f in tap.frames("s2c")}
    out: list[tuple[str, str]] = []
    for sent in tap.frames("c2s"):
        if sent.opcode != opcodes.AUTH:
            continue
        credential = "token" if decode(sent.payload).get("token") else "password"
        answer = answers.get(sent.request_id)
        if answer is None:
            outcome = "no answer"
        elif answer.opcode == opcodes.AUTH_OK:
            outcome = "ok"
        else:
            outcome = names.get(int(decode(answer.payload).get("reason", 1)), "?")
        out.append((credential, outcome))
    return out


# -- a wrong password -----------------------------------------------------------------------------


def test_a_wrong_password_is_credentials_and_the_next_attempt_dials_a_new_connection(
    mode: str, own: Any, tap_identity: faketls.Identity
) -> None:
    node = own()
    with Tap(tap_identity, node.driver_port) as tap:
        for attempt in (1, 2):
            with pytest.raises(errors.AuthenticationError) as refused:
                log_in(mode, node, tap, tap_identity, "not-the-password")
            assert refused.value.reason == "CREDENTIALS" and refused.value.reason_code == 1
            assert "invalid username or password" in refused.value.message
            assert not refused.value.retryable
            assert len(auth_transcript(tap)) == attempt, "one AUTH per attempt: no retry loop"
            assert tap.connections == attempt, "each attempt dialled a new connection"
        assert auth_transcript(tap) == [("password", "CREDENTIALS")] * 2
        pc = log_in(mode, node, tap, tap_identity, ROOT_PASSWORD)
        try:
            assert pc.call(lambda c: c.ping()) is None
        finally:
            pc.close()
        assert auth_transcript(tap)[-1] == ("password", "ok")
        assert tap.connections == 3


# -- tokens ---------------------------------------------------------------------------------------


def second_connection_while_the_first_is_busy(rig: Rig) -> None:
    """Make the pool open a second connection: the first is checked out while a call is made."""
    if rig.mode == "sync":

        def blocking(c: Any) -> None:
            lease = c._ctx.pool.checkout("point", Deadline(5.0), op="hold", kind="READ")
            try:
                c.ping()
            finally:
                c._ctx.pool.release(lease, reusable=True)

        rig.call(blocking)
    else:

        async def asynchronous(c: Any) -> None:
            lease = await c._ctx.pool.checkout("point", Deadline(5.0), op="hold", kind="READ")
            try:
                await c.ping()
            finally:
                c._ctx.pool.release(lease, reusable=True)

        rig.call(asynchronous)


def test_a_token_minted_on_one_connection_authenticates_another(
    mode: str, own: Any, tap_identity: faketls.Identity
) -> None:
    node = own()
    rig = Rig(mode, node, tap_identity, pool=hoarddb.PoolOptions(size=2))
    try:
        second_connection_while_the_first_is_busy(rig)
        assert rig.call(lambda c: c.pool_stats()).dials == 2
        assert auth_transcript(rig.tap) == [("password", "ok"), ("token", "ok")]
        presented = [decode(f.payload) for f in rig.tap.frames("c2s") if f.opcode == opcodes.AUTH]
        assert presented[0].get("password") and not presented[0].get("token")
        assert presented[1].get("token"), "the second connection presents the token"
        methods = [e.get("auth_method") for e in node.log_lines() if e.get("auth_method")]
        assert methods == ["password", "token"], "and the server authenticated it by the token"
    finally:
        rig.close()


def mint_token(node: Node) -> str:
    """A resumption token from a bare password ``AUTH``."""
    with RawConn(node.driver_port) as conn:
        granted = authenticate(conn)
    assert granted.opcode == opcodes.AUTH_OK
    token = str(decode(granted.payload)["token"])
    assert token
    return token


@pytest.mark.parametrize("kind", ["expired", "garbage"])
def test_a_token_the_server_refuses_falls_back_to_the_password_exactly_once(
    kind: str, mode: str, own: Any, tap_identity: faketls.Identity
) -> None:
    node = own(SHORT_TOKENS)  # a token lives one second, with no clock skew allowed
    if kind == "expired":
        token = mint_token(node)
        time.sleep(1.3)  # the 1 s token must run out on the server's clock: a wait is the point
        want = "TOKEN_EXPIRED"
    else:
        token, want = "not.a.token", "TOKEN_INVALID"
    with Tap(tap_identity, node.driver_port) as tap:
        pc = log_in(mode, node, tap, tap_identity, ROOT_PASSWORD, auth_token=token)
        try:
            assert pc.call(lambda c: c.ping()) is None, "the caller never sees the refused token"
            transcript = auth_transcript(tap)
            assert transcript == [("token", want), ("password", "ok")], transcript
            assert tap.connections == 2, "the fallback runs on a new connection"
        finally:
            pc.close()
        # the password AUTH minted a fresh token, which the next connection can use: no second
        # fallback, and no token AUTH on a connection that used its password
        assert [t[1] for t in auth_transcript(tap)].count(want) == 1


# -- RATE_LIMITED ---------------------------------------------------------------------------------


def test_rate_limited_is_surfaced_at_once_and_never_retried(
    mode: str, own: Any, tap_identity: faketls.Identity
) -> None:
    node = own({"HOARDDB_TCP_AUTH_MAX_FAILS": "1"})  # one failure and the address is refused
    with Tap(tap_identity, node.driver_port) as tap:
        with pytest.raises(errors.AuthenticationError) as wrong:
            log_in(mode, node, tap, tap_identity, "not-the-password")
        assert wrong.value.reason == "CREDENTIALS"
        began = time.monotonic()
        with pytest.raises(errors.AuthenticationError) as limited:
            log_in(mode, node, tap, tap_identity, ROOT_PASSWORD)
        elapsed = time.monotonic() - began
        assert limited.value.reason == "RATE_LIMITED" and limited.value.reason_code == 6
        assert limited.value.retryable
        assert "no credential was checked" in limited.value.message.lower() or (
            "wait" in limited.value.message.lower()
        )
        assert elapsed < 3.0, f"surfaced after {elapsed:.1f} s: it must not wait or loop"
        assert auth_transcript(tap) == [("password", "CREDENTIALS"), ("password", "RATE_LIMITED")]


# -- the limiter's real delays (measured, and recorded in the run summary) ------------------------


def timed_wrong_logins(node: Node, attempts: int) -> list[float]:
    delays: list[float] = []
    for _ in range(attempts):
        with RawConn(node.driver_port, timeout=60.0) as conn:
            began = time.monotonic()
            conn.send(
                opcodes.AUTH,
                frame.new_request_id(),
                serialize(password_auth_payload(ROOT_USER, "wrong")),
            )
            answer = conn.read_frame()
            delays.append(time.monotonic() - began)
            assert answer.opcode == opcodes.AUTH_FAIL
    return delays


def test_the_failed_login_delays_are_what_auth_md_says(own: Any) -> None:
    node = own()
    delays = timed_wrong_logins(node, 8)
    RESULT_LINES.append(
        "leg 8 limiter: seconds to answer failed logins 1..8: "
        + " ".join(f"{d:.2f}" for d in delays)
    )
    assert all(d < 1.0 for d in delays[:5]), f"the first failures are not delayed: {delays}"
    later = delays[5:]
    assert max(later) >= 1.0, f"a delay of 1 s or more must set in by the 8th failure: {delays}"
    assert later == sorted(later) or max(later) - later[-1] < 0.5, f"delays double: {delays}"


def test_the_lockout_lasts_about_a_minute_and_then_a_good_login_works(own: Any) -> None:
    node = own({"HOARDDB_TCP_AUTH_MAX_FAILS": "3"})
    timed_wrong_logins(node, 3)
    locked_at = time.monotonic()
    waited = 0.0
    with RawConn(node.driver_port) as conn:
        refused = authenticate(conn)
    assert refused.opcode == opcodes.AUTH_FAIL and decode(refused.payload).get("reason") == 6
    while True:
        with RawConn(node.driver_port) as conn:
            answer = authenticate(conn)
        waited = time.monotonic() - locked_at
        if answer.opcode == opcodes.AUTH_OK:
            break
        assert decode(answer.payload).get("reason") == 6, "RATE_LIMITED until it lifts"
        assert waited < 90, "the lockout must lift"
        time.sleep(2.0)  # a poll of a 60 s lockout; bounded by the assert above
    RESULT_LINES.append(f"leg 8 limiter: lockout after 3 failures lifted after {waited:.0f} s")
    assert 50 <= waited <= 75


# -- a credential taken away under a live pool ----------------------------------------------------


def test_a_changed_password_under_a_live_pool_is_reported_on_the_next_call(
    mode: str, own: Any, tap_identity: faketls.Identity
) -> None:
    """The nearest thing to "a dropped user" that the documented surface allows (Q16): pooled
    connections die with the server, the token has run out, and the redial's password is refused."""
    node = own(SHORT_TOKENS)
    rig = Rig(mode, node, tap_identity)
    try:
        assert rig.call(lambda c: c.ping()) is None
        node.stop()
        node.reset_password(ROOT_USER, "a-different-password-1")
        node.start()
        time.sleep(1.3)  # the token minted before the restart has run out (1 s life)
        rig.tap.clear()
        with pytest.raises(errors.AuthenticationError) as refused:
            rig.call(lambda c: c.ping())
        assert refused.value.reason == "CREDENTIALS" and not refused.value.retryable
        transcript = auth_transcript(rig.tap)
        assert transcript[-1] == ("password", "CREDENTIALS")
        assert [t for t in transcript if t[0] == "password"] == [("password", "CREDENTIALS")], (
            f"one password attempt, never a loop: {transcript}"
        )
    finally:
        rig.close()
