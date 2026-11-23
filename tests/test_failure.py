# SPDX-License-Identifier: Apache-2.0
"""P2: ERROR / AUTH_FAIL decoding, in-band refusals and the local errors (errors.md, spec 8.5)."""

from __future__ import annotations

import pickle
from typing import Any

import pytest

import minibson
import vectors
from hoarddb import errors
from hoarddb._core import diagnostics, frame, messages
from hoarddb._core.failure import (
    ZERO_ID,
    Context,
    auth_failure_from_payload,
    check_deleted,
    check_found,
    check_success,
    check_updated,
    error_from_payload,
    is_connection_level,
)

ERROR_OPCODE = 0xA2
AUTH_FAIL_OPCODE = 0x03

CLASS_BY_NAME: dict[str, type[errors.HoardDBError]] = {
    "NOT_FOUND": errors.NotFoundError,
    "ALREADY_EXISTS": errors.AlreadyExistsError,
    "PERMISSION_DENIED": errors.PermissionDeniedError,
    "INVALID_ARGUMENT": errors.InvalidArgumentError,
    "INTERNAL": errors.InternalError,
    "UNAVAILABLE": errors.UnavailableError,
    "TIMEOUT": errors.WriteConcernTimeoutError,
    "CONFLICT": errors.ConflictError,
    "UNSUPPORTED_VERSION": errors.UnsupportedVersionError,
    "NOT_AUTHENTICATED": errors.NotAuthenticatedError,
    "OVERLOADED": errors.OverloadedError,
    "RESULT_TOO_LARGE": errors.ResultTooLargeError,
    "DUPLICATE_KEY": errors.DuplicateKeyError,
    "FIELD_TYPE": errors.FieldTypeError,
    "BATCH_TOO_LARGE": errors.BatchTooLargeError,
    "DOCUMENT_EXISTS": errors.DocumentExistsError,
}

LAST_CODE_WITH_A_CLASS = 18  # DOCUMENT_EXISTS; the ring's own codes start at 19


def _decode(frame_hex: str) -> tuple[frame.Frame, dict[str, Any]]:
    f = frame.decode_frame(bytes.fromhex(frame_hex))
    return f, minibson.loads(f.payload) if f.payload else {}


# --- the decode vectors -----------------------------------------------------------------------

DECODE = vectors.driver_vectors(kind="decode")


@pytest.mark.parametrize("vec", DECODE, ids=lambda v: v["name"])
def test_decode_vectors(vec: vectors.Vector) -> None:
    expect = vec["expect"]
    f, payload = _decode(vec["frame_hex"])
    if expect["result"] == "answer":
        assert f.opcode not in (ERROR_OPCODE, AUTH_FAIL_OPCODE)
        return
    if expect["result"] == "error":
        assert f.opcode == ERROR_OPCODE
        err = error_from_payload(payload, f.request_id, Context("probe"))
        assert err.wire_code == expect["code"]
        assert err.code == expect["name"]
        assert CLASS_BY_NAME.get(expect["name"].split("(")[0], errors.InternalError) is type(err)
    else:
        assert f.opcode == AUTH_FAIL_OPCODE
        err = auth_failure_from_payload(payload, f.request_id, username="admin", host_port="h:1")
        assert isinstance(err, errors.AuthenticationError)
        assert err.reason == expect["name"]
        assert err.reason_code == expect["auth_fail_reason"]
    assert is_connection_level(f.request_id) is expect["request_id_zero"]
    if expect["request_id_zero"]:
        assert expect["connection_fatal"] is True  # the server closes after a zero-ID refusal
    if "message" in payload:
        assert err.message == payload["message"]  # verbatim
    for needle in expect.get("message_contains", []):
        assert needle in err.message


def test_a_code_zero_or_missing_error_is_internal_and_never_success() -> None:
    for payload in ({"code": 0, "message": "m"}, {"message": "m"}, {}):
        err = error_from_payload(payload, b"\x01" * 16, Context("get"))
        assert type(err) is errors.InternalError
        assert (err.code, err.wire_code) == ("INTERNAL", 0)


def test_an_unknown_code_keeps_its_number() -> None:
    err = error_from_payload({"code": 99, "message": "m"}, b"\x01" * 16, Context("get"))
    assert type(err) is errors.InternalError
    assert (err.code, err.wire_code) == ("UNKNOWN(99)", 99)
    assert err.hint is not None and "Upgrade the driver" in err.hint
    assert err.retryable is False


def test_document_exists_is_a_typed_refusal() -> None:
    """Code 18: an insert of a key that already holds a document. The server stored nothing and
    its sentence quotes shell syntax, so the hint names the calls this driver has."""
    sentence = 'Document "ada" already exists in bucket "users" (database "app").'
    payload = {"code": 18, "message": sentence, "write_outcome": "not_applied"}
    err = error_from_payload(payload, b"\x01" * 16, Context("put", "app/users/ada", "WRITE"))
    assert type(err) is errors.DocumentExistsError
    assert (err.code, err.wire_code, err.message) == ("DOCUMENT_EXISTS", 18, sentence)
    assert err.write_outcome == "not_applied" and err.retryable is False
    assert err.hint is not None and "upsert(key, doc)" in err.hint and "update(key" in err.hint
    assert "Upgrade the driver" not in str(err)


def test_batch_too_large_is_a_typed_refusal() -> None:
    payload = {"code": 17, "message": "none of it was written", "write_outcome": "not_applied"}
    err = error_from_payload(payload, b"\x01" * 16, Context("insert_many", "app/users", "WRITE"))
    assert type(err) is errors.BatchTooLargeError
    assert (err.code, err.wire_code, err.write_outcome) == ("BATCH_TOO_LARGE", 17, "not_applied")
    assert err.retryable is False


def test_an_empty_auth_fail_payload_is_credentials() -> None:
    err = auth_failure_from_payload({}, b"\x01" * 16, username="admin", host_port="h:7433")
    assert err.reason == "CREDENTIALS"
    assert err.reason_code == 1


# --- every ERROR / AUTH_FAIL answer of the exchange vectors ----------------------------------


def _error_exchanges() -> list[vectors.Vector]:
    return [v for v in vectors.driver_vectors(kind="exchange") if "error" in v or "auth_fail" in v]


@pytest.mark.parametrize("vec", _error_exchanges(), ids=lambda v: v["name"])
def test_exchange_vector_errors(vec: vectors.Vector) -> None:
    resp = vec["response"]
    payload = minibson.loads(bytes.fromhex(resp["payload_hex"]))
    rid = bytes.fromhex(resp["request_id_hex"])
    if "auth_fail" in vec:
        want = vec["auth_fail"]
        aerr = auth_failure_from_payload(payload, rid, username="admin", host_port="h:1")
        assert (aerr.reason, aerr.reason_code) == (want["name"], want["reason"])
        assert aerr.message == payload["message"]
        assert all(s in aerr.message for s in want["message_contains"])
        return
    want = vec["error"]
    err = error_from_payload(payload, rid, Context(vec["request"]["opcode_name"], None, "WRITE"))
    if want["code"] > LAST_CODE_WITH_A_CLASS:
        # A ring code. This driver sends no ring opcode and has no class for one: it keeps the
        # number and the server's sentence, and never reads the code as one it knows.
        assert type(err) is errors.InternalError
        assert err.code == f"UNKNOWN({want['code']})"
    else:
        assert type(err) is CLASS_BY_NAME[want["name"]]
        assert err.code == want["name"]
    assert err.wire_code == want["code"]
    assert err.message == payload["message"]
    assert all(s in err.message for s in want["message_contains"])
    if "write_outcome" in want:
        assert err.write_outcome == want["write_outcome"]
    assert err.request_id == rid.hex()
    assert pickle.loads(pickle.dumps(err)).__dict__ == err.__dict__


# --- the table of section 8.5 and the retry table --------------------------------------------

RID = b"\x07" * 16


def _err(code: int, kind: Any = "READ", **extra: Any) -> errors.HoardDBError:
    payload = {"code": code, "message": "server sentence", **extra}
    return error_from_payload(payload, extra.pop("rid", RID), Context("op", "app/b", kind))


@pytest.mark.parametrize(
    ("code", "hint_has"),
    [
        (1, "db.list_buckets()"),
        (2, "if_not_exists=True"),
        (3, "role problem"),
        (5, "request_id"),
        (7, "do not resend a push"),
        (13, "pool=`hoarddb.PoolOptions(size=2)`"),
    ],
)
def test_hints_name_the_fix(code: int, hint_has: str) -> None:
    err = _err(code, "WRITE", write_outcome="applied" if code == 7 else "not_applied")
    assert err.hint is not None
    assert hint_has in err.hint


@pytest.mark.parametrize("code", [4, 8, 14, 15, 16])
def test_codes_whose_server_message_is_the_diagnosis_add_no_hint(code: int) -> None:
    extra = {"max_result_bytes": 5, "retained_bytes": 6} if code == 14 else {}
    assert _err(code, **extra).hint is None


def test_unsupported_version_has_two_hints_for_two_causes() -> None:
    wire = _err(10, rid=ZERO_ID)
    declared = _err(10, "WRITE", write_outcome="not_applied")
    assert wire.hint is not None and "hoarddb/1" in wire.hint
    assert declared.hint is not None
    assert "hoarddb/1" not in declared.hint
    assert "operator" in declared.hint


@pytest.mark.parametrize(
    ("code", "kind", "outcome", "retryable"),
    [
        (6, "READ", None, True),
        (6, "WRITE", "not_applied", True),
        (6, "WRITE", "unknown", False),
        (6, "WRITE", None, False),
        (6, "DDL", "not_applied", True),
        (6, "DDL", None, False),
        (7, "WRITE", "applied", False),
        (12, "READ", None, True),
        (12, "WRITE", None, False),
        (13, "READ", None, True),
        (13, "WRITE", None, False),
        (1, "READ", None, False),
        (4, "WRITE", "not_applied", False),
        (5, "READ", None, False),
        (14, "READ", None, False),
    ],
)
def test_retryable_follows_the_table(
    code: int, kind: str, outcome: str | None, retryable: bool
) -> None:
    extra: dict[str, Any] = {"write_outcome": outcome} if outcome else {}
    if code == 14:
        extra |= {"max_result_bytes": 1, "retained_bytes": 1}
    assert _err(code, kind, **extra).retryable is retryable


def test_a_zero_id_overloaded_is_retryable_for_every_kind() -> None:
    for kind in ("READ", "WRITE", "DDL"):
        assert _err(13, kind, rid=ZERO_ID).retryable is True


def test_a_write_refusal_without_an_outcome_is_unknown_but_a_read_has_none() -> None:
    assert _err(5, "WRITE").write_outcome == "unknown"
    assert _err(5, "READ").write_outcome is None
    assert _err(5, "DDL").write_outcome is None
    assert _err(5, "WRITE", write_outcome="not_applied").write_outcome == "not_applied"


def test_copies_and_constraint_refusal_and_result_too_large_numbers_are_carried() -> None:
    t = _err(7, "WRITE", write_outcome="applied", copies_confirmed=1, replication_factor=3)
    assert (t.copies_confirmed, t.replication_factor) == (1, 3)
    ia = _err(4, "WRITE", constraint_refusal=True)
    assert isinstance(ia, errors.InvalidArgumentError) and ia.constraint_refusal is True
    big = _err(14)
    assert isinstance(big, errors.ResultTooLargeError)
    assert (big.max_result_bytes, big.retained_bytes) == (0, 0)  # absent: zero
    big = _err(14, max_result_bytes=1 << 20, retained_bytes=3)
    assert isinstance(big, errors.ResultTooLargeError)
    assert (big.max_result_bytes, big.retained_bytes) == (1 << 20, 3)


def test_the_operation_and_target_are_recorded() -> None:
    err = _err(1)
    assert (err.operation, err.target) == ("op", "app/b")


# --- auth failures ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "name", "retryable", "hint_has"),
    [
        (1, "CREDENTIALS", False, "HOARDDB_ROOT_PASSWORD"),
        (2, "TOKEN_EXPIRED", False, "password"),
        (3, "TOKEN_INVALID", False, "password"),
        (4, "TOKEN_UNSUPPORTED", False, "tokens"),
        (5, "PROTOCOL_MISMATCH", False, "Upgrade"),
        (6, "RATE_LIMITED", True, "no credential was checked"),
        (7, "ALREADY_AUTHED", False, "bug in the driver"),
        (8, "INTERNAL", True, "another host"),
    ],
)
def test_auth_failure_reasons(reason: int, name: str, retryable: bool, hint_has: str) -> None:
    err = auth_failure_from_payload(
        {"reason": reason, "message": "from the server"}, RID, username="admin", host_port="db:7433"
    )
    assert (err.reason, err.reason_code, err.retryable) == (name, reason, retryable)
    assert err.message == "from the server"
    assert err.hint is not None and hint_has.lower() in err.hint.lower()
    assert err.code == "AUTH_FAILED"


def test_rate_limited_says_to_wait_and_the_driver_does_not_retry() -> None:
    err = auth_failure_from_payload({"reason": 6}, RID, username="admin", host_port="db:7433")
    assert err.hint is not None
    assert "wait" in err.hint.lower()
    assert "does not retry" in err.hint


def test_credentials_hint_names_user_host_and_the_lockout() -> None:
    err = auth_failure_from_payload({"reason": 1}, RID, username="ada", host_port="db:7433")
    assert err.hint is not None
    assert 'user "ada"' in err.hint
    assert "db:7433" in err.hint
    assert "lock this address out" in err.hint
    assert "admin" not in err.message


def test_an_unknown_auth_reason_is_kept_and_not_retryable() -> None:
    err = auth_failure_from_payload({"reason": 42}, RID, username="a", host_port="h:1")
    assert (err.reason, err.reason_code, err.retryable) == ("UNKNOWN(42)", 42, False)


# --- in-band refusals (errors.md section 3, operations.md) -----------------------------------


def _ctx(kind: Any = "READ") -> Context:
    return Context("op", "app/b/k", kind)


@pytest.mark.parametrize("message", ["document not found", "queue is empty", "heap is empty"])
def test_the_listed_read_absences_are_misses(message: str) -> None:
    assert check_found({"found": False, "message": message}, RID, _ctx()) is False


def test_a_blob_absence_is_a_miss_and_a_found_read_is_found() -> None:
    blob = 'blob "nothing.txt" not found in v096.files — keys are exact'
    assert check_found({"found": False, "message": blob}, RID, _ctx()) is False
    assert check_found({"found": True, "data": {}}, RID, _ctx()) is True


def test_a_field_miss_is_silent_success_true_found_false() -> None:
    assert check_found({"success": True, "found": False}, RID, _ctx()) is False


@pytest.mark.parametrize(
    ("message", "cls"),
    [
        ("find not supported for bucket type fifo", errors.InvalidArgumentError),
        ("pop not supported for bucket type hash", errors.InvalidArgumentError),
        ("db.files.find() does not scan blob buckets: ...", errors.InvalidArgumentError),
        ('bucket not found: bucket "p" not found in database "d". Run ...', errors.NotFoundError),
        ('database "d" not found on this node. Run ...', errors.NotFoundError),
        ("", errors.InvalidArgumentError),
    ],
)
def test_every_other_found_false_is_an_error_with_the_message_verbatim(
    message: str, cls: type[errors.HoardDBError]
) -> None:
    with pytest.raises(cls) as info:
        check_found({"found": False, "message": message}, RID, _ctx())
    assert info.value.message == message
    assert type(info.value) is cls


def test_a_delete_miss_is_false_and_anything_else_is_an_error() -> None:
    miss = {"success": False, "message": "document not found", "write_outcome": "not_applied"}
    assert check_deleted(miss, RID, _ctx("WRITE")) is False
    blob = {"success": False, "message": "blob not found", "write_outcome": "not_applied"}
    assert check_deleted(blob, RID, _ctx("WRITE")) is False
    assert check_deleted({"success": True, "message": ""}, RID, _ctx("WRITE")) is True
    refusal = {"success": False, "message": "delete not supported for bucket type fifo"}
    with pytest.raises(errors.InvalidArgumentError) as info:
        check_deleted(refusal, RID, _ctx("WRITE"))
    assert info.value.message == "delete not supported for bucket type fifo"
    assert info.value.write_outcome == "unknown"  # a write refusal with no outcome


def test_an_update_miss_is_false_and_anything_else_is_an_error() -> None:
    miss = {"success": False, "message": 'document "nobody" not found in v059.users — insert it'}
    assert check_updated(miss, RID, _ctx("WRITE")) is False
    assert check_updated({"success": True, "doc": {}}, RID, _ctx("WRITE")) is True
    with pytest.raises(errors.InvalidArgumentError):
        check_updated({"success": False, "message": "something else"}, RID, _ctx("WRITE"))


def test_success_false_on_ddl_is_invalid_argument_with_the_message_verbatim() -> None:
    check_success({"success": True, "message": "bucket exists"}, RID, _ctx("DDL"))
    with pytest.raises(errors.InvalidArgumentError) as info:
        check_success(
            {"success": False, "message": "unsupported bucket type: queue"}, RID, _ctx("DDL")
        )
    assert info.value.message == "unsupported bucket type: queue"
    assert info.value.write_outcome is None  # a refused DDL changed nothing


def test_in_band_errors_carry_the_outcome_fields_of_the_payload() -> None:
    payload = {"success": False, "message": "m", "write_outcome": "applied", "copies_confirmed": 2}
    with pytest.raises(errors.InvalidArgumentError) as info:
        check_success(payload, RID, _ctx("WRITE"))
    assert (info.value.write_outcome, info.value.copies_confirmed) == ("applied", 2)
    assert info.value.wire_code is None


def test_every_in_band_exchange_vector_classifies_as_pinned() -> None:
    checked = 0
    for v in vectors.driver_vectors(kind="exchange"):
        resp = v["response"]
        if resp["opcode_name"] == "ERROR" or v["request"]["opcode"] not in messages.REQUESTS:
            continue  # a refusal frame, or an opcode this driver does not send
        if not resp.get("payload_hex") or resp["opcode_name"] == "GET_FAST_RESPONSE":
            continue  # empty, or GET_FAST's status byte + BSON, which is not a plain document
        payload = minibson.loads(bytes.fromhex(resp["payload_hex"]))
        rid = bytes.fromhex(resp["request_id_hex"])
        op = v["request"]["opcode_name"]
        if payload.get("found") is False and payload.get("message") is not None:
            msg = payload["message"]
            miss = msg in (
                "document not found",
                "queue is empty",
                "heap is empty",
            ) or msg.startswith('blob "')
            if miss:
                assert check_found(payload, rid, Context(op, None, "READ")) is False
            else:
                with pytest.raises(errors.HoardDBError):
                    check_found(payload, rid, Context(op, None, "READ"))
            checked += 1
    assert checked >= 9


# --- local errors (spec 8.5) -----------------------------------------------------------------


def test_client_closed() -> None:
    err = diagnostics.client_closed()
    assert isinstance(err, errors.ClientClosedError)
    assert err.message == "This client was closed. Create a new one with hoarddb.connect()."


def test_frame_too_large_gives_the_arithmetic_and_the_remedy() -> None:
    err = diagnostics.frame_too_large("put", frame.FrameTooLarge(41943040 + 17))
    assert isinstance(err, errors.FrameTooLargeError)
    assert isinstance(err, errors.InvalidArgumentError)
    assert err.message == (
        "This put encodes to 41943040 bytes; a frame holds at most 33554432 "
        "(1 + 16 + payload <= 32 MiB). Nothing was sent. Split it into smaller batches, "
        "or store a large value as a blob."
    )
    assert err.write_outcome is None


def test_protocol_error_names_the_host_and_discards_the_connection() -> None:
    with pytest.raises(frame.FrameRefusal) as info:
        frame.parse_prefix(b"\x01\xff\xff\xff\xff")
    err = diagnostics.protocol_error("db:7433", info.value)
    assert isinstance(err, errors.ProtocolError)
    assert err.message == (
        "The server at db:7433 sent a frame length of 4294967295, over the 33554432 limit. "
        "The connection was discarded."
    )


def test_request_timeout_read_write_sent_and_not_sent() -> None:
    read = diagnostics.request_timeout("get", "app/users", 30.0, "READ", sent=True)
    assert read.message == (
        "get on app/users timed out after 30.0 s. For a slow call pass timeout=<seconds>; "
        "the connection was discarded."
    )
    assert (read.retryable, read.write_outcome) == (True, None)
    sent = diagnostics.request_timeout("push", "app/jobs", 5.0, "WRITE", sent=True)
    assert sent.write_outcome == "unknown"
    assert sent.retryable is False
    assert (
        "The outcome is unknown: read it back before resending, and do not resend a push, "
        "a pop or an increment."
    ) in str(sent)
    unsent = diagnostics.request_timeout("push", "app/jobs", 5.0, "WRITE", sent=False)
    assert unsent.write_outcome == "not_applied"
    assert unsent.retryable is True
    assert "Nothing was sent, so it is safe to repeat." in str(unsent)
    assert isinstance(unsent, TimeoutError)


def test_connection_failed_at_dial_follows_one_pass_wording() -> None:
    err = diagnostics.connection_failed(
        ["localhost:7433"], "ECONNREFUSED", "put", "WRITE", target="app/users"
    )
    assert isinstance(err, errors.ConnectionFailedError)
    assert err.message == (
        "Could not connect to localhost:7433 (ECONNREFUSED). Is hoardDB-server running there? "
        "The server listens on 127.0.0.1:7433 by default; the container image listens on every "
        "interface. Tried 1 host(s): localhost:7433."
    )
    assert (err.write_outcome, err.retryable) == ("not_applied", True)
    read = diagnostics.connection_failed(["a:1", "b:2"], "timed out", "get", "READ")
    assert (read.write_outcome, read.retryable) == (None, True)
    assert "Tried 2 host(s): a:1, b:2." in read.message


def test_connection_lost_mid_call_is_unknown_for_a_write_that_was_sent() -> None:
    w = diagnostics.connection_lost("db:1", "pop", "reset by peer", "WRITE", sent=True)
    assert w.message.startswith("The connection to db:1 failed during pop (reset by peer).")
    assert (w.write_outcome, w.retryable) == ("unknown", False)
    assert "do not resend a push, a pop or an increment" in str(w)
    d = diagnostics.connection_lost("db:1", "create_bucket", "EOF", "DDL", sent=True)
    assert d.write_outcome == "unknown"
    assert "list" in str(d).lower()
    r = diagnostics.connection_lost("db:1", "get", "EOF", "READ", sent=True)
    assert (r.write_outcome, r.retryable) == (None, True)


def test_unsupported_feature_forked_client_and_pool_timeout_name_the_fix() -> None:
    uf = diagnostics.unsupported_feature("1.0.2", "write_concern", 'write_concern="majority"')
    assert uf.message == (
        'This server (1.0.2) does not advertise "write_concern", so write_concern="majority" '
        "cannot be honoured. Nothing was sent. Upgrade the server, or leave the option out."
    )
    fk = diagnostics.forked_client(4121, 4188)
    assert fk.message == (
        "This client was created in process 4121 and is being used in process 4188 "
        "(after a fork). Create the client after the fork, in a gunicorn post_fork hook, "
        "or inside the worker."
    )
    assert "in a gunicorn post_fork hook, or inside the worker" in fk.message  # spec 3.5
    pt = diagnostics.pool_timeout(30.0, 4, 37, "point")
    assert pt.message == (
        "No connection became free within 30.0 s (4 in use, 37 waiting on the point lane). "
        "Raise pool=`hoarddb.PoolOptions(size=8)`, or find the call that holds a connection; "
        "blob and bulk work uses the separate bulk lane."
    )
