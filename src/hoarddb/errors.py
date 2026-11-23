# SPDX-License-Identifier: Apache-2.0
"""The error classes (spec 8.5).

One base, :class:`HoardDBError`, with stable string codes and a subclass per code. Match with
``except``/``isinstance`` and read ``err.code`` for logs. **Before repeating a call, test
``err.retryable`` and ``err.write_outcome``, never the class**: ``ConnectionFailedError`` and
``RequestTimeoutError`` are also ``OSError`` subclasses, so a generic ``except OSError`` retry
helper would repeat a write whose outcome is unknown.

The server's sentence is kept byte for byte in ``err.message``; what the driver adds (the fix) is
in ``err.hint``, and ``str(err)`` is the two together (a sentence stop is inserted between them
when the server's message does not end in one).
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal, TypedDict, Unpack

__all__ = [
    "AlreadyExistsError",
    "AuthenticationError",
    "BatchTooLargeError",
    "ClientClosedError",
    "ConfigError",
    "ConflictError",
    "ConnectionFailedError",
    "DocumentExistsError",
    "DuplicateKeyError",
    "FieldTypeError",
    "ForkedClientError",
    "FrameTooLargeError",
    "HoardDBError",
    "InsecureTlsWarning",
    "InternalError",
    "InvalidArgumentError",
    "NotAuthenticatedError",
    "NotFoundError",
    "OverloadedError",
    "PermissionDeniedError",
    "PoolTimeoutError",
    "ProtocolError",
    "QuotaExceededError",
    "RequestTimeoutError",
    "ResultTooLargeError",
    "TlsTrustError",
    "UnavailableError",
    "UnsupportedFeatureError",
    "UnsupportedVersionError",
    "WriteConcernTimeoutError",
    "WriteOutcome",
]

WriteOutcome = Literal["applied", "not_applied", "unknown"]


class _Fields(TypedDict, total=False):
    code: str
    wire_code: int | None
    operation: str
    target: str | None
    request_id: str | None
    retryable: bool
    write_outcome: WriteOutcome | None
    copies_confirmed: int | None
    replication_factor: int | None
    written_count: int | None
    written_ids: list[str] | None
    hint: str | None


def _rebuild(cls: type[HoardDBError], args: tuple[Any, ...], state: dict[str, Any]) -> Any:
    # The default exception reduction calls cls(*args) and so drops keyword-only arguments.
    err = cls.__new__(cls)
    BaseException.__init__(err, *args)
    err.__dict__.update(state)
    return err


class HoardDBError(Exception):
    """Base of every error the driver raises. See the module docstring on ``retryable``."""

    default_code: ClassVar[str] = "HOARDDB_ERROR"

    # Declared here, not just assigned in __init__, so that every type checker sees the exact types
    # (an inferred attribute widens ``write_outcome``'s Literal to ``str``).
    code: str
    wire_code: int | None
    message: str
    operation: str
    target: str | None
    request_id: str | None
    retryable: bool
    write_outcome: WriteOutcome | None
    copies_confirmed: int | None
    replication_factor: int | None
    written_count: int | None
    written_ids: list[str] | None
    hint: str | None

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        wire_code: int | None = None,
        operation: str = "",
        target: str | None = None,
        request_id: str | None = None,
        retryable: bool = False,
        write_outcome: WriteOutcome | None = None,
        copies_confirmed: int | None = None,
        replication_factor: int | None = None,
        written_count: int | None = None,
        written_ids: list[str] | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code if code is not None else self.default_code
        self.wire_code = wire_code
        self.message = message
        self.operation = operation
        self.target = target
        self.request_id = request_id
        self.retryable = retryable
        self.write_outcome = write_outcome
        self.copies_confirmed = copies_confirmed
        self.replication_factor = replication_factor
        self.written_count = written_count
        self.written_ids = written_ids
        self.hint = hint

    def __str__(self) -> str:
        if not self.hint:
            return self.message
        text = self.message.rstrip()
        if not text:
            return self.hint
        stop = "" if text.endswith((".", "!", "?", ":")) else "."
        return f"{text}{stop} {self.hint}"

    def __reduce__(self) -> tuple[Any, ...]:
        return (_rebuild, (type(self), self.args, dict(self.__dict__)))


class NotFoundError(HoardDBError):
    default_code = "NOT_FOUND"


class AlreadyExistsError(HoardDBError):
    default_code = "ALREADY_EXISTS"


class PermissionDeniedError(HoardDBError):
    default_code = "PERMISSION_DENIED"


class InvalidArgumentError(HoardDBError):
    default_code = "INVALID_ARGUMENT"

    def __init__(
        self, message: str, *, constraint_refusal: bool = False, **fields: Unpack[_Fields]
    ) -> None:
        super().__init__(message, **fields)
        self.constraint_refusal = constraint_refusal


class InternalError(HoardDBError):
    default_code = "INTERNAL"


class UnavailableError(HoardDBError):
    default_code = "UNAVAILABLE"


class WriteConcernTimeoutError(HoardDBError):
    default_code = "TIMEOUT"


class ConflictError(HoardDBError):
    default_code = "CONFLICT"


class QuotaExceededError(HoardDBError):
    default_code = "QUOTA_EXCEEDED"


class UnsupportedVersionError(HoardDBError):
    default_code = "UNSUPPORTED_VERSION"


class NotAuthenticatedError(HoardDBError):
    default_code = "NOT_AUTHENTICATED"


class OverloadedError(HoardDBError):
    default_code = "OVERLOADED"


class ResultTooLargeError(HoardDBError):
    default_code = "RESULT_TOO_LARGE"

    def __init__(
        self,
        message: str,
        *,
        max_result_bytes: int,
        retained_bytes: int,
        **fields: Unpack[_Fields],
    ) -> None:
        super().__init__(message, **fields)
        self.max_result_bytes = max_result_bytes
        self.retained_bytes = retained_bytes


class DuplicateKeyError(HoardDBError):
    default_code = "DUPLICATE_KEY"


class FieldTypeError(HoardDBError):
    default_code = "FIELD_TYPE"


class BatchTooLargeError(HoardDBError):
    """A batch over the largest one the store commits whole. Nothing of it was stored
    (``write_outcome`` is ``"not_applied"``), so sending it again in smaller parts is safe."""

    default_code = "BATCH_TOO_LARGE"


class DocumentExistsError(HoardDBError):
    """A ``put``, an ``insert`` or an ``insert_many`` named a key that already holds a document.
    Nothing was stored (``write_outcome`` is ``"not_applied"``): on a hash or btree bucket a put
    inserts and never replaces.
    Replace the document with ``upsert(key, doc)``, or change some of its fields with
    ``update(key, ops)``."""

    default_code = "DOCUMENT_EXISTS"


class ConnectionFailedError(HoardDBError, ConnectionError):
    default_code = "CONNECTION_FAILED"


class AuthenticationError(HoardDBError):
    default_code = "AUTH_FAILED"

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        reason_code: int | None = None,
        **fields: Unpack[_Fields],
    ) -> None:
        super().__init__(message, **fields)
        self.reason = reason
        self.reason_code = reason_code


class ProtocolError(HoardDBError):
    default_code = "PROTOCOL_ERROR"


class RequestTimeoutError(HoardDBError, TimeoutError):
    default_code = "REQUEST_TIMEOUT"


class ClientClosedError(HoardDBError):
    default_code = "CLIENT_CLOSED"


class PoolTimeoutError(HoardDBError):
    default_code = "POOL_TIMEOUT"


class FrameTooLargeError(InvalidArgumentError):
    default_code = "FRAME_TOO_LARGE"


class ConfigError(HoardDBError):
    default_code = "INVALID_OPTIONS"


class UnsupportedFeatureError(HoardDBError):
    default_code = "UNSUPPORTED_FEATURE"


class ForkedClientError(HoardDBError):
    default_code = "FORKED_CLIENT"


class TlsTrustError(HoardDBError):
    default_code = "TLS_UNTRUSTED"


class InsecureTlsWarning(UserWarning):
    """Raised once per location by ``warnings.warn`` when verification is switched off."""
