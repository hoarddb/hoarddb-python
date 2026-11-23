# SPDX-License-Identifier: Apache-2.0
"""P2: the error classes (spec 8.5): fields, rendering, hierarchy, pickling."""

from __future__ import annotations

import builtins
import pickle
from typing import Any

import pytest

from hoarddb import errors

SENTINEL = "s3cret-sentinel-9f2"


def _samples() -> list[errors.HoardDBError]:
    return [
        errors.HoardDBError("base"),
        errors.NotFoundError("nf", wire_code=1, operation="get", target="app/users"),
        errors.AlreadyExistsError("ae", wire_code=2),
        errors.PermissionDeniedError("pd", wire_code=3),
        errors.InvalidArgumentError("ia", wire_code=4, constraint_refusal=True),
        errors.InternalError("ie", wire_code=5),
        errors.InternalError("odd", code="UNKNOWN(99)", wire_code=99),
        errors.UnavailableError("un", wire_code=6, write_outcome="not_applied", retryable=True),
        errors.WriteConcernTimeoutError(
            "wc", wire_code=7, write_outcome="applied", copies_confirmed=1, replication_factor=3
        ),
        errors.ConflictError("co", wire_code=8),
        errors.QuotaExceededError("qe", wire_code=9),
        errors.UnsupportedVersionError("uv", wire_code=10),
        errors.NotAuthenticatedError("na", wire_code=12),
        errors.OverloadedError("ov", wire_code=13, retryable=True),
        errors.ResultTooLargeError("rt", wire_code=14, max_result_bytes=1 << 20, retained_bytes=7),
        errors.DuplicateKeyError("dk", wire_code=15, write_outcome="not_applied"),
        errors.FieldTypeError("ft", wire_code=16, write_outcome="not_applied"),
        errors.BatchTooLargeError("btl", wire_code=17, write_outcome="not_applied"),
        errors.DocumentExistsError("de", wire_code=18, write_outcome="not_applied"),
        errors.ConnectionFailedError("cf", write_outcome="not_applied", retryable=True),
        errors.AuthenticationError("af", reason="CREDENTIALS", reason_code=1),
        errors.ProtocolError("pe"),
        errors.RequestTimeoutError("rto", write_outcome="unknown"),
        errors.ClientClosedError("cc"),
        errors.PoolTimeoutError("pt"),
        errors.FrameTooLargeError("ftl"),
        errors.ConfigError("cfg", code="INVALID_URI"),
        errors.UnsupportedFeatureError("uf"),
        errors.ForkedClientError("fk"),
        errors.TlsTrustError("tls", code="TLS_KEY_CHANGED"),
        errors.InvalidArgumentError("many", written_count=2, written_ids=["a", "b"]),
    ]


def test_every_error_is_a_hoarddb_error_with_a_stable_code() -> None:
    expected = {
        errors.NotFoundError: "NOT_FOUND",
        errors.AlreadyExistsError: "ALREADY_EXISTS",
        errors.PermissionDeniedError: "PERMISSION_DENIED",
        errors.InvalidArgumentError: "INVALID_ARGUMENT",
        errors.InternalError: "INTERNAL",
        errors.UnavailableError: "UNAVAILABLE",
        errors.WriteConcernTimeoutError: "TIMEOUT",
        errors.ConflictError: "CONFLICT",
        errors.QuotaExceededError: "QUOTA_EXCEEDED",
        errors.UnsupportedVersionError: "UNSUPPORTED_VERSION",
        errors.NotAuthenticatedError: "NOT_AUTHENTICATED",
        errors.OverloadedError: "OVERLOADED",
        errors.ResultTooLargeError: "RESULT_TOO_LARGE",
        errors.DuplicateKeyError: "DUPLICATE_KEY",
        errors.FieldTypeError: "FIELD_TYPE",
        errors.BatchTooLargeError: "BATCH_TOO_LARGE",
        errors.DocumentExistsError: "DOCUMENT_EXISTS",
        errors.ConnectionFailedError: "CONNECTION_FAILED",
        errors.AuthenticationError: "AUTH_FAILED",
        errors.ProtocolError: "PROTOCOL_ERROR",
        errors.RequestTimeoutError: "REQUEST_TIMEOUT",
        errors.ClientClosedError: "CLIENT_CLOSED",
        errors.PoolTimeoutError: "POOL_TIMEOUT",
        errors.FrameTooLargeError: "FRAME_TOO_LARGE",
        errors.UnsupportedFeatureError: "UNSUPPORTED_FEATURE",
        errors.ForkedClientError: "FORKED_CLIENT",
    }
    for cls, code in expected.items():
        assert issubclass(cls, errors.HoardDBError)
        extra: dict[str, Any] = {}
        if cls is errors.ResultTooLargeError:
            extra = {"max_result_bytes": 0, "retained_bytes": 0}
        if cls is errors.AuthenticationError:
            extra = {"reason": "CREDENTIALS"}
        assert cls("m", **extra).code == code


def test_frame_too_large_is_an_invalid_argument_and_two_errors_are_oserrors() -> None:
    assert issubclass(errors.FrameTooLargeError, errors.InvalidArgumentError)
    assert issubclass(errors.ConnectionFailedError, builtins.ConnectionError)
    assert issubclass(errors.RequestTimeoutError, builtins.TimeoutError)
    assert issubclass(errors.ConnectionFailedError, OSError)  # the hazard of spec 8.5, kept


def test_str_is_the_message_then_the_hint_and_message_stays_the_servers() -> None:
    plain = errors.NotFoundError("bucket not found: x")
    hinted = errors.NotFoundError("bucket not found: x", hint="Create it.")
    assert str(plain) == "bucket not found: x"
    assert str(hinted) == "bucket not found: x. Create it."  # a sentence break is inserted
    assert hinted.message == "bucket not found: x"
    assert hinted.hint == "Create it."


@pytest.mark.parametrize("stop", [".", "!", "?"])
def test_str_keeps_a_message_that_already_ends_a_sentence_and_joins_with_one_space(
    stop: str,
) -> None:
    err = errors.NotFoundError(f"bucket not found{stop}", hint="Create it.")
    assert str(err) == f"bucket not found{stop} Create it."


def test_str_of_an_empty_message_with_a_hint_is_the_hint() -> None:
    assert str(errors.InternalError("", hint="Report it.")) == "Report it."


def test_defaults_are_the_documented_ones() -> None:
    err = errors.HoardDBError("m")
    assert (err.wire_code, err.operation, err.target, err.request_id) == (None, "", None, None)
    assert err.retryable is False
    assert err.write_outcome is None
    assert (err.copies_confirmed, err.replication_factor) == (None, None)
    assert (err.written_count, err.written_ids, err.hint) == (None, None, None)


def test_the_cause_chain_is_pythons_own() -> None:
    inner = OSError("econnrefused")
    with pytest.raises(errors.ConnectionFailedError) as info:
        raise errors.ConnectionFailedError("could not connect") from inner
    assert info.value.__cause__ is inner


@pytest.mark.parametrize("err", _samples(), ids=lambda e: f"{type(e).__name__}:{e.code}")
def test_every_error_survives_pickle_field_for_field(err: errors.HoardDBError) -> None:
    clone = pickle.loads(pickle.dumps(err))
    assert type(clone) is type(err)
    assert vars(clone) == vars(err)
    assert clone.args == err.args
    assert str(clone) == str(err)


def test_a_required_keyword_only_field_survives_pickle() -> None:
    too_big = errors.ResultTooLargeError("m", max_result_bytes=5, retained_bytes=9)
    auth = errors.AuthenticationError("m", reason="RATE_LIMITED", reason_code=6)
    for err in (too_big, auth):
        clone = pickle.loads(pickle.dumps(err))
        assert vars(clone) == vars(err)
    assert pickle.loads(pickle.dumps(too_big)).max_result_bytes == 5
    assert pickle.loads(pickle.dumps(auth)).reason == "RATE_LIMITED"


def test_required_fields_are_required() -> None:
    with pytest.raises(TypeError):
        errors.ResultTooLargeError("m")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        errors.AuthenticationError("m")  # type: ignore[call-arg]


def test_an_error_never_prints_what_it_was_not_given() -> None:
    for err in _samples():
        rendered = " ".join([repr(err), str(err), repr(vars(err))])
        assert SENTINEL not in rendered


def test_the_insecure_tls_warning_is_a_warning() -> None:
    assert issubclass(errors.InsecureTlsWarning, Warning)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("see the log:", "see the log: Create it."),
        ("bucket not found. ", "bucket not found. Create it."),
        ("bucket not found: x \n", "bucket not found: x. Create it."),
        ("   ", "Create it."),
    ],
    ids=["colon", "trailing-space", "trailing-whitespace", "blank"],
)
def test_str_joins_edge_case_messages_cleanly(message: str, expected: str) -> None:
    err = errors.NotFoundError(message, hint="Create it.")
    assert str(err) == expected
    assert err.message == message
