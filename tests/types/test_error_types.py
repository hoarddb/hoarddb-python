# SPDX-License-Identifier: Apache-2.0
"""Type tests for the error classes: the attributes every error carries, the hierarchy, and the
constructors that must keep their required arguments."""

from __future__ import annotations

from typing import assert_type

import hoarddb
from hoarddb.errors import WriteOutcome


def _attributes(err: hoarddb.HoardDBError) -> None:
    assert_type(err.code, str)
    assert_type(err.message, str)
    assert_type(err.wire_code, int | None)
    assert_type(err.retryable, bool)
    assert_type(err.write_outcome, WriteOutcome | None)
    assert_type(err.hint, str | None)
    assert_type(err.operation, str)
    assert_type(err.target, str | None)
    assert_type(err.request_id, str | None)
    err.nope  # type: ignore[attr-defined]  # noqa: B018  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
    err.retryable = "yes"  # type: ignore[assignment]  # pyright: ignore[reportAttributeAccessIssue]


def _hierarchy(
    base: hoarddb.HoardDBError,
    refusal: hoarddb.InvalidArgumentError,
    missing: hoarddb.NotFoundError,
    blocked: hoarddb.TlsTrustError,
) -> None:
    base = missing  # every error is a HoardDBError
    refusal = hoarddb.FrameTooLargeError("too big")  # a frame refusal is an invalid argument
    not_an_error: BaseException = blocked
    wrong: hoarddb.NotFoundError = hoarddb.InternalError("x")  # type: ignore[assignment]  # pyright: ignore[reportAssignmentType]
    assert base and refusal and not_an_error and wrong


def _specific(auth: hoarddb.AuthenticationError, big: hoarddb.ResultTooLargeError) -> None:
    assert_type(auth.reason, str)
    assert_type(auth.reason_code, int | None)
    assert_type(big.max_result_bytes, int)
    assert_type(big.retained_bytes, int)
    hoarddb.ResultTooLargeError("m")  # type: ignore[call-arg]  # pyright: ignore[reportCallIssue]
    hoarddb.AuthenticationError("m")  # type: ignore[call-arg]  # pyright: ignore[reportCallIssue]
    hoarddb.NotFoundError()  # type: ignore[call-arg]  # pyright: ignore[reportCallIssue]


def _handling() -> None:
    try:
        hoarddb.connect()
    except hoarddb.NotFoundError as caught:
        assert_type(caught, hoarddb.NotFoundError)
    except (hoarddb.TlsTrustError, hoarddb.AuthenticationError) as caught_either:
        assert_type(caught_either, hoarddb.TlsTrustError | hoarddb.AuthenticationError)
    except hoarddb.HoardDBError as caught_any:
        assert_type(caught_any.write_outcome, WriteOutcome | None)


def test_the_assertions_above_are_checked_by_the_type_checkers() -> None:
    assert callable(_attributes) and callable(_hierarchy) and callable(_specific)
    assert callable(_handling)
