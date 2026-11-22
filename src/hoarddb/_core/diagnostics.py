# SPDX-License-Identifier: Apache-2.0
"""The driver's own errors (spec 8.5): each says what happened, what it means, what to do."""

from __future__ import annotations

from hoarddb import errors
from hoarddb._core.failure import Context
from hoarddb._core.frame import MAX_LENGTH, MIN_LENGTH, FrameRefusal, FrameTooLarge
from hoarddb._core.kinds import Kind

UNKNOWN_WRITE_OUTCOME = (
    "The outcome is unknown: read it back before resending, and do not resend a push, "
    "a pop or an increment."
)
UNKNOWN_DDL_OUTCOME = (
    "The outcome is unknown: list what exists with `db.list_buckets()` or "
    "`client.list_databases()` before repeating the create or drop."
)


def _unknown_sentence(kind: Kind) -> str:
    return UNKNOWN_DDL_OUTCOME if kind == "DDL" else UNKNOWN_WRITE_OUTCOME


def client_closed() -> errors.ClientClosedError:
    return errors.ClientClosedError(
        "This client was closed. Create a new one with hoarddb.connect().", operation="call"
    )


def frame_too_large(operation: str, too_large: FrameTooLarge) -> errors.FrameTooLargeError:
    payload = too_large.size - MIN_LENGTH
    return errors.FrameTooLargeError(
        f"This {operation} encodes to {payload} bytes; a frame holds at most {MAX_LENGTH} "
        "(1 + 16 + payload <= 32 MiB). Nothing was sent. Split it into smaller batches, "
        "or store a large value as a blob.",
        operation=operation,
    )


def protocol_error(host_port: str, refusal: FrameRefusal) -> errors.ProtocolError:
    return errors.ProtocolError(
        f"The server at {host_port} sent {refusal.detail}. The connection was discarded.",
        operation="receive",
        target=host_port,
    )


def stamp_protocol_error(err: errors.ProtocolError, ctx: Context) -> None:
    """Give a ``ProtocolError`` raised inside a call the call's operation, and for a write or DDL
    request that was already sent the unknown outcome, with the sentence that says what to do.

    The error is raised where the bytes are read, which knows the host but not the call; the
    connection's ``exchange`` knows the call and stamps it here. A read keeps ``write_outcome``
    ``None``.
    """
    err.operation = ctx.operation
    if ctx.kind is None or ctx.kind == "READ":
        return
    err.write_outcome = "unknown"
    if err.hint is None:
        err.hint = _unknown_sentence(ctx.kind)


def request_timeout(
    operation: str, target: str | None, seconds: float, kind: Kind, *, sent: bool
) -> errors.RequestTimeoutError:
    where = f"{operation} on {target}" if target else operation
    message = (
        f"{where} timed out after {seconds} s. For a slow call pass timeout=<seconds>; "
        "the connection was discarded."
    )
    if kind == "READ":
        return errors.RequestTimeoutError(
            message, operation=operation, target=target, retryable=True
        )
    if sent:
        return errors.RequestTimeoutError(
            message,
            operation=operation,
            target=target,
            write_outcome="unknown",
            hint=_unknown_sentence(kind),
        )
    return errors.RequestTimeoutError(
        message,
        operation=operation,
        target=target,
        retryable=True,
        write_outcome="not_applied",
        hint="Nothing was sent, so it is safe to repeat.",
    )


class DialFailed(errors.ConnectionFailedError):
    """A dial that failed, with the short ``reason`` kept apart from the full message so a pass
    over several hosts can name each host's reason."""

    reason: str = ""


def connection_failed(
    hosts: list[str], reason: str, operation: str, kind: Kind, *, target: str | None = None
) -> DialFailed:
    """A call's one pass over the host list ended with every dial refused or unreachable."""
    first = hosts[0]
    message = (
        f"Could not connect to {first} ({reason}). Is hoardDB-server running there? "
        "The server listens on 127.0.0.1:7433 by default; the container image listens on every "
        f"interface. Tried {len(hosts)} host(s): {', '.join(hosts)}."
    )
    failed = DialFailed(
        message,
        operation=operation,
        target=target if target is not None else first,
        retryable=True,
        write_outcome=None if kind == "READ" else "not_applied",
    )
    failed.reason = reason
    return failed


def connection_lost(
    host_port: str, operation: str, reason: str, kind: Kind, *, sent: bool
) -> errors.ConnectionFailedError:
    message = f"The connection to {host_port} failed during {operation} ({reason})."
    if kind == "READ":
        return errors.ConnectionFailedError(
            message, operation=operation, target=host_port, retryable=True
        )
    if sent:
        return errors.ConnectionFailedError(
            message,
            operation=operation,
            target=host_port,
            write_outcome="unknown",
            hint=_unknown_sentence(kind),
        )
    return errors.ConnectionFailedError(
        message,
        operation=operation,
        target=host_port,
        retryable=True,
        write_outcome="not_applied",
        hint="Nothing was sent, so it is safe to repeat.",
    )


def unsupported_feature(
    server_version: str, capability: str, option: str
) -> errors.UnsupportedFeatureError:
    return errors.UnsupportedFeatureError(
        f'This server ({server_version}) does not advertise "{capability}", so {option} '
        "cannot be honoured. Nothing was sent. Upgrade the server, or leave the option out.",
        retryable=False,
    )


def forked_client(created_pid: int, current_pid: int) -> errors.ForkedClientError:
    return errors.ForkedClientError(
        f"This client was created in process {created_pid} and is being used in process "
        f"{current_pid} (after a fork). Create the client after the fork, in a gunicorn "
        "post_fork hook, or inside the worker."
    )


def pool_timeout(seconds: float, in_use: int, waiting: int, lane: str) -> errors.PoolTimeoutError:
    option = "size=8" if lane == "point" else "bulk_size=4"
    return errors.PoolTimeoutError(
        f"No connection became free within {seconds} s ({in_use} in use, {waiting} waiting on "
        f"the {lane} lane). Raise pool=`hoarddb.PoolOptions({option})`, or find the call that "
        "holds a connection; blob and bulk work uses the separate bulk lane."
    )


def pool_queue_full(max_waiters: int, in_use: int, lane: str) -> errors.PoolTimeoutError:
    return errors.PoolTimeoutError(
        f"The {lane} lane has all {in_use} connections in use and {max_waiters} calls already "
        "waiting for one, so this call was refused at once instead of joining a queue that only "
        "grows. Raise pool=`hoarddb.PoolOptions(max_waiters=...)`, or find the call that holds "
        "a connection."
    )
