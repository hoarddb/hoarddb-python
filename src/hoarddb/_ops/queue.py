# SPDX-License-Identifier: Apache-2.0
"""Queue and heap verbs: ``push``, ``pop``, ``peek`` (spec 4.6).

The ``FIFO_*`` opcode family is sent for fifo, lifo and heap alike: the server aliases the
families (``wire.md`` section 4; there is no ``HEAP_PEEK``), and the vectors pin it. ``priority``
is sent on every push, as int64, ``0`` when the caller gives none, so the wire shape of a push does
not depend on the store type. A ``pop`` consumes, so it is a write: it carries a write concern and
is never retried. It answers a list, as a ``peek`` does: the entries it removed, in pop order.
"""

from __future__ import annotations

from typing import Any, Final

from hoarddb import errors
from hoarddb._core import failure
from hoarddb._core.call import Call, Op, Reply
from hoarddb._core.messages import (
    FifoPeekRequest,
    FifoPopRequest,
    FifoPushRequest,
    PeekResponse,
    PopResponse,
)
from hoarddb._ops.keyed import write_result
from hoarddb._ops.refs import BucketRef
from hoarddb._ops.support import call, context, payload, require_mapping, write_concern
from hoarddb.types import WriteResult

MAX_PEEK: Final = 1000
MAX_POP: Final = 100
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


def check_pop_limit(limit: object, *, operation: str) -> int:
    """The ``limit`` member of a pop: left off the wire (0) when the caller gives none."""
    if limit is None:
        return 0
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_POP:
        raise errors.InvalidArgumentError(
            f"pop limit must be a whole number from 1 to {MAX_POP}, not {limit!r}. Nothing was "
            f"sent. A pop removes what it returns, so for more than {MAX_POP} repeat the pop, or "
            f"read them without removing any with peek(limit={MAX_PEEK}).",
            operation=operation,
        )
    return limit


def unreadable_pop(c: Call) -> errors.ProtocolError:
    """``found: true`` with no entry the driver can read, or ``found: false`` beside entries. The
    server may have removed something, so neither is handed back as "nothing to pop"."""
    return errors.ProtocolError(
        f"The server answered this pop of {c.target} with a found flag and entries that "
        "contradict each other, so what it removed is lost to this call. Do not repeat the pop: "
        "read what is left with peek(), and use a driver that matches the server.",
        operation=c.op,
        target=c.target,
        write_outcome="unknown",
    )


def pop_answer(c: Call, limit: int) -> Op[Reply]:
    """Send a pop and return its answer. A pop whose answer never arrives may have removed what
    it asked for, so that error also says how many entries that is and how to see what is left."""
    try:
        reply = yield c
    except errors.HoardDBError as lost:
        if lost.write_outcome == "unknown":
            count = "1 entry" if limit <= 1 else f"up to {limit} entries"
            lost.hint = (lost.hint + " " if lost.hint else "") + (
                f"This pop may have removed {count}, and they cannot be read again: "
                "peek() shows what is left."
            )
        raise
    return reply


def pop(
    ref: BucketRef, *, limit: int | None, wc: str | None, timeout: float | None
) -> Op[list[dict[str, Any]]]:
    op = "pop"
    request = FifoPopRequest(
        database=ref.database,
        bucket=ref.name,
        limit=check_pop_limit(limit, operation=op),
        write_concern=write_concern(wc, None, operation=op),
    )
    c = call(op, ref.target, request, "WRITE", timeout)
    reply = yield from pop_answer(c, request.limit)
    r: PopResponse = reply.response
    found = failure.check_found(payload(reply), reply.request_id, context(c))
    if found != bool(r.items):  # found with nothing to read, or entries beside "not found"
        raise unreadable_pop(c)
    return list(r.items)


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
