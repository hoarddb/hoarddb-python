# SPDX-License-Identifier: Apache-2.0
"""Queue and heap verbs: ``push``, ``pop``, ``peek`` (spec 4.6).

The ``FIFO_*`` opcode family is sent for fifo, lifo and heap alike: the server aliases the
families (``wire.md`` section 4; there is no ``HEAP_PEEK``), and the vectors pin it. ``priority``
is sent on every push, as int64, ``0`` when the caller gives none, so the wire shape of a push does
not depend on the store type. A ``pop`` consumes, so it is a write: it carries a write concern and
is never retried.
"""

from __future__ import annotations

from typing import Any, Final

from hoarddb import errors
from hoarddb._core import failure
from hoarddb._core.call import Op
from hoarddb._core.messages import FifoPeekRequest, FifoPopRequest, FifoPushRequest, PeekResponse
from hoarddb._ops.keyed import write_result
from hoarddb._ops.refs import BucketRef
from hoarddb._ops.support import call, context, payload, require_mapping, write_concern
from hoarddb.types import WriteResult

MAX_PEEK: Final = 1000
_INT64: Final = range(-(2**63), 2**63)


def check_priority(priority: object, *, operation: str) -> int:
    if priority is None:
        return 0
    if isinstance(priority, bool) or not isinstance(priority, int) or priority not in _INT64:
        raise errors.InvalidArgumentError(
            f"priority must be a whole number within 64 bits, not {priority!r}. Nothing was sent.",
            operation=operation,
        )
    return priority


def push(
    ref: BucketRef,
    doc: Any,
    *,
    priority: int | None,
    wc: str | None,
    timeout: float | None,
) -> Op[WriteResult]:
    op = "push"
    data = require_mapping(doc, "A document", operation=op)
    request = FifoPushRequest(
        database=ref.database,
        bucket=ref.name,
        data=data,
        priority=check_priority(priority, operation=op),
        write_concern=write_concern(wc, None, operation=op),
    )
    c = call(op, ref.target, request, "WRITE", timeout)
    reply = yield c
    failure.check_success(payload(reply), reply.request_id, context(c))
    return write_result(reply.response)


def pop(ref: BucketRef, *, wc: str | None, timeout: float | None) -> Op[dict[str, Any] | None]:
    op = "pop"
    request = FifoPopRequest(
        database=ref.database,
        bucket=ref.name,
        write_concern=write_concern(wc, None, operation=op),
    )
    c = call(op, ref.target, request, "WRITE", timeout)
    reply = yield c
    if not failure.check_found(payload(reply), reply.request_id, context(c)):
        return None
    data: dict[str, Any] | None = reply.response.data
    return data


def peek(ref: BucketRef, *, limit: int, timeout: float | None) -> Op[list[dict[str, Any]]]:
    op = "peek"
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PEEK:  # pyright: ignore[reportUnnecessaryIsInstance]
        raise errors.InvalidArgumentError(
            f"peek limit must be a whole number from 1 to {MAX_PEEK}, not {limit!r}. "
            "Nothing was sent.",
            operation=op,
        )
    request = FifoPeekRequest(database=ref.database, bucket=ref.name, limit=limit)
    c = call(op, ref.target, request, "READ", timeout)
    reply = yield c
    r: PeekResponse = reply.response
    if not failure.check_found(payload(reply), reply.request_id, context(c)):
        return []
    return list(r.items)
