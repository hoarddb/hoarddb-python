# SPDX-License-Identifier: Apache-2.0
"""Small helpers every operation module uses: building a ``Call`` and reading its answer."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hoarddb import errors
from hoarddb._core import failure
from hoarddb._core.call import Call, Lane, Reply
from hoarddb._core.kinds import Kind
from hoarddb._core.messages import Request
from hoarddb._core.payloads import as_payload
from hoarddb._core.validate import check_write_concern


def call(
    op: str,
    target: str | None,
    request: Request,
    kind: Kind,
    timeout: float | None,
    lane: Lane = "point",
) -> Call:
    return Call(op=op, target=target, request=request, lane=lane, kind=kind, timeout=timeout)


def context(c: Call) -> failure.Context:
    return failure.Context(operation=c.op, target=c.target, kind=c.kind)


def payload(reply: Reply) -> dict[str, Any]:
    """The answer's fields as the mapping the in-band checks read."""
    return as_payload(reply.response)


def request_id(reply: Reply) -> bytes:
    return reply.request_id


def write_concern(value: str | None, default: str | None, *, operation: str) -> str:
    """The write concern to send: the call's, else the client's default, else the server's
    (empty). Checked before anything is sent."""
    chosen = value if value is not None else default
    if chosen is None:
        return ""
    check_write_concern(chosen, operation=operation)
    return chosen


def require_mapping(value: object, what: str, *, operation: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise errors.InvalidArgumentError(
            f"{what} must be a mapping with string keys, not a {type(value).__name__}; "
            "convert it first. Nothing was sent.",
            operation=operation,
        )
    return value  # pyright: ignore[reportUnknownVariableType]
