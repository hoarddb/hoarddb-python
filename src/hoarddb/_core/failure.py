# SPDX-License-Identifier: Apache-2.0
"""Turning the server's refusals into errors (errors.md sections 1 to 4, spec 8.5).

Three channels, one error type: an ``ERROR`` frame, an ``AUTH_FAIL`` frame, and an in-band
``success: false`` / ``found: false`` on an otherwise successful frame. The server's sentence is
passed through byte for byte as ``err.message``; the fix the driver adds goes in ``err.hint``.
These functions take payloads that are already decoded to mappings and never touch a socket.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from hoarddb import errors
from hoarddb._core.kinds import Kind

ZERO_ID: Final = bytes(16)

_OUTCOMES: Final = ("applied", "not_applied", "unknown")


@dataclass(frozen=True, slots=True)
class Context:
    """What the caller was doing: it names the error and decides read/write handling."""

    operation: str = ""
    target: str | None = None
    kind: Kind | None = None


NO_CONTEXT: Final = Context()


def is_connection_level(request_id: bytes) -> bool:
    """A refusal with an all-zero request ID is connection-level and is followed by a close."""
    return request_id == ZERO_ID


def _int(value: Any, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _opt_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _outcome(payload: Mapping[str, Any], kind: Kind | None) -> errors.WriteOutcome | None:
    raw = payload.get("write_outcome")
    if raw in _OUTCOMES:
        return raw  # type: ignore[no-any-return]
    # A refused write with no outcome comes from an older server or a store failure: unknown.
    return "unknown" if kind == "WRITE" else None


# --- hints (the fix, in one sentence) --------------------------------------------------------

HINT_NOT_FOUND: Final = (
    "List what exists with `db.list_buckets()` or `client.list_databases()`; "
    "create it with `db.create_bucket(name, 'hash')`."
)
HINT_ALREADY_EXISTS: Final = (
    "Make creation repeatable with `db.create_bucket(name, 'hash', if_not_exists=True)`."
)
HINT_PERMISSION: Final = (
    "This is a role problem: an administrator grants the role named in the message."
)
HINT_UNSUPPORTED_WIRE: Final = "This driver speaks `hoarddb/1`: use a driver and server that agree."
HINT_UNSUPPORTED_DECLARATION: Final = (
    "The server cannot enforce this bucket's declaration; reads still work. "
    "An operator must act (upgrade the server or re-declare the bucket)."
)
HINT_OVERLOADED: Final = (
    "The server's connection cap is full: lower the pool with pool=`hoarddb.PoolOptions(size=2)`, "
    "or raise HOARDDB_TCP_MAX_CONNS_PER_IP on the server."
)
HINT_TIMEOUT: Final = "The write is stored; do not resend a push or an increment."
HINT_BATCH_TOO_LARGE: Final = (
    "insert_many() splits such a batch for you; one document that is refused alone is too "
    "large to store."
)
# The server's sentence quotes shell syntax; this is the same advice as calls of this driver.
HINT_DOCUMENT_EXISTS: Final = (
    "Nothing was stored: replace the document with `bucket.upsert(key, doc)`, or change some "
    'of its fields with `bucket.update(key, {"$set": {...}})`.'
)
HINT_UNKNOWN_CODE: Final = "A newer server than this driver? Upgrade the driver."


def _internal_hint(request_id: bytes) -> str:
    return f"Report it with the request_id ({request_id.hex()}) if it repeats."


def error_from_payload(
    payload: Mapping[str, Any], request_id: bytes, ctx: Context = NO_CONTEXT
) -> errors.HoardDBError:
    """Decode an ``ERROR`` frame's payload. Code 0, or none, is ``INTERNAL``: never success."""
    code = _int(payload.get("code"))
    raw_message = payload.get("message")
    message = raw_message if isinstance(raw_message, str) else ""
    zero = is_connection_level(request_id)
    outcome = _outcome(payload, ctx.kind)
    common: dict[str, Any] = {
        "wire_code": code,
        "operation": ctx.operation,
        "target": ctx.target,
        "request_id": request_id.hex(),
        "write_outcome": outcome,
        "copies_confirmed": _opt_int(payload.get("copies_confirmed")),
        "replication_factor": _opt_int(payload.get("replication_factor")),
    }
    read = ctx.kind == "READ"
    safe_unavailable = read or (ctx.kind in ("WRITE", "DDL") and outcome == "not_applied")

    def build(
        cls: type[errors.HoardDBError],
        *,
        retryable: bool = False,
        hint: str | None = None,
        **more: Any,
    ) -> errors.HoardDBError:
        return cls(message, retryable=retryable, hint=hint, **common, **more)

    match code:
        case 1:
            return build(errors.NotFoundError, hint=HINT_NOT_FOUND)
        case 2:
            return build(errors.AlreadyExistsError, hint=HINT_ALREADY_EXISTS)
        case 3:
            return build(errors.PermissionDeniedError, hint=HINT_PERMISSION)
        case 4:
            return build(
                errors.InvalidArgumentError,
                constraint_refusal=payload.get("constraint_refusal") is True,
            )
        case 6:
            return build(errors.UnavailableError, retryable=safe_unavailable)
        case 7:
            return build(errors.WriteConcernTimeoutError, hint=HINT_TIMEOUT)
        case 8:
            return build(errors.ConflictError)
        case 9:
            return build(errors.QuotaExceededError)
        case 10:
            hint = HINT_UNSUPPORTED_WIRE if zero else HINT_UNSUPPORTED_DECLARATION
            return build(errors.UnsupportedVersionError, hint=hint)
        case 11:
            return build(errors.HoardDBError, code="EARLY_DATA_REJECTED")
        case 12:
            return build(errors.NotAuthenticatedError, retryable=read)
        case 13:
            return build(errors.OverloadedError, retryable=zero or read, hint=HINT_OVERLOADED)
        case 14:
            return build(
                errors.ResultTooLargeError,
                max_result_bytes=_int(payload.get("max_result_bytes")),
                retained_bytes=_int(payload.get("retained_bytes")),
            )
        case 15:
            return build(errors.DuplicateKeyError)
        case 16:
            return build(errors.FieldTypeError)
        case 17:
            return build(errors.BatchTooLargeError, hint=HINT_BATCH_TOO_LARGE)
        case 18:
            return build(errors.DocumentExistsError, hint=HINT_DOCUMENT_EXISTS)
        case 0 | 5:
            return build(errors.InternalError, hint=_internal_hint(request_id))
        case _:
            return build(errors.InternalError, code=f"UNKNOWN({code})", hint=HINT_UNKNOWN_CODE)


# --- AUTH_FAIL -------------------------------------------------------------------------------

_AUTH_REASONS: Final = {
    1: "CREDENTIALS",
    2: "TOKEN_EXPIRED",
    3: "TOKEN_INVALID",
    4: "TOKEN_UNSUPPORTED",
    5: "PROTOCOL_MISMATCH",
    6: "RATE_LIMITED",
    7: "ALREADY_AUTHED",
    8: "INTERNAL",
}


def _auth_hint(reason: str, username: str, host_port: str) -> str | None:
    match reason:
        case "CREDENTIALS":
            return (
                f'Check the user name and password for user "{username}" on {host_port} '
                "(the root password is HOARDDB_ROOT_PASSWORD, or <data dir>/root.password on the "
                "server). The driver does not retry: repeated failures lock this address out."
            )
        case "TOKEN_EXPIRED" | "TOKEN_INVALID":
            return (
                "The token was refused and the password fallback did not succeed: "
                "check the password for this user."
            )
        case "TOKEN_UNSUPPORTED":
            return "This server does not issue tokens; the driver uses the password instead."
        case "PROTOCOL_MISMATCH":
            return "Upgrade the driver or the server so both speak protocol 1 (`hoarddb/1`)."
        case "RATE_LIMITED":
            return (
                "No credential was checked: this address is being rate limited after failed "
                "logins. Wait (up to a minute), then try again. The driver does not retry on its "
                "own; a retry loop is what the limit exists to stop."
            )
        case "ALREADY_AUTHED":
            return (
                "This is a bug in the driver (a second password AUTH on one connection). "
                "Report it with the request_id; the driver will not resend."
            )
        case "INTERNAL":
            return (
                "This is not a credential failure: the node was not ready to check logins. "
                "Try again in a moment, or list another host."
            )
        case _:
            return None


def auth_failure_from_payload(
    payload: Mapping[str, Any], request_id: bytes, *, username: str, host_port: str
) -> errors.AuthenticationError:
    """Decode an ``AUTH_FAIL``. An empty payload, which older servers send, is ``CREDENTIALS``."""
    number = _int(payload.get("reason"), 1) or 1
    reason = _AUTH_REASONS.get(number, f"UNKNOWN({number})")
    raw = payload.get("message")
    message = (
        raw
        if isinstance(raw, str) and raw
        else f'Authentication failed for user "{username}" on {host_port}.'
    )
    return errors.AuthenticationError(
        message,
        reason=reason,
        reason_code=number,
        operation="connect",
        target=host_port,
        request_id=request_id.hex(),
        retryable=reason in ("RATE_LIMITED", "INTERNAL"),
        hint=_auth_hint(reason, username, host_port),
    )


# --- in-band refusals ------------------------------------------------------------------------

_READ_MISSES: Final = frozenset({"document not found", "queue is empty", "heap is empty"})
_BLOB_MISS: Final = re.compile(r'^blob ".*" not found in ')
_UPDATE_MISS: Final = re.compile(r'^document ".*" not found in ')
_DELETE_MISSES: Final = frozenset({"document not found", "blob not found"})


def _in_band_error(
    payload: Mapping[str, Any], request_id: bytes, ctx: Context
) -> errors.HoardDBError:
    raw = payload.get("message")
    message = raw if isinstance(raw, str) else ""
    fields: dict[str, Any] = {
        "operation": ctx.operation,
        "target": ctx.target,
        "request_id": request_id.hex(),
        "write_outcome": _outcome(payload, ctx.kind),
        "copies_confirmed": _opt_int(payload.get("copies_confirmed")),
        "replication_factor": _opt_int(payload.get("replication_factor")),
    }
    absent_scope = message.startswith("bucket not found") or (
        message.startswith('database "') and " not found on this node" in message
    )
    if absent_scope:
        return errors.NotFoundError(message, hint=HINT_NOT_FOUND, **fields)
    return errors.InvalidArgumentError(message, **fields)


def _message(payload: Mapping[str, Any]) -> str:
    raw = payload.get("message")
    return raw if isinstance(raw, str) else ""


def check_found(payload: Mapping[str, Any], request_id: bytes, ctx: Context = NO_CONTEXT) -> bool:
    """A read: True when found, False for a listed absence, otherwise raise.

    ``found: false`` is a miss only with one of the messages the vectors pin (or, for a store
    field, none at all alongside ``success: true``); any other ``found: false`` is a refusal.
    """
    if payload.get("found") is True:
        return True
    message = _message(payload)
    if payload.get("success") is True and not message:
        return False
    if message in _READ_MISSES or _BLOB_MISS.match(message):
        return False
    raise _in_band_error(payload, request_id, ctx)


def check_deleted(payload: Mapping[str, Any], request_id: bytes, ctx: Context = NO_CONTEXT) -> bool:
    """A delete: True when something was deleted, False for ``document``/``blob not found``."""
    if payload.get("success") is True:
        return True
    if _message(payload) in _DELETE_MISSES:
        return False
    raise _in_band_error(payload, request_id, ctx)


def check_updated(payload: Mapping[str, Any], request_id: bytes, ctx: Context = NO_CONTEXT) -> bool:
    """An update: True when applied, False for ``document "..." not found in ...``."""
    if payload.get("success") is True:
        return True
    if _UPDATE_MISS.match(_message(payload)):
        return False
    raise _in_band_error(payload, request_id, ctx)


def check_success(payload: Mapping[str, Any], request_id: bytes, ctx: Context = NO_CONTEXT) -> None:
    """Writes and DDL: ``success: false`` is always a refusal."""
    if payload.get("success") is False:
        raise _in_band_error(payload, request_id, ctx)
