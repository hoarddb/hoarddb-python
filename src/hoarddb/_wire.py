# SPDX-License-Identifier: Apache-2.0
"""A private raw sender, for the conformance vectors and for operator opcodes.

No stability promise, not exported, not documented for programs. It sends any opcode with a
document you built, through the client's own pool, deadline and error decoding, and returns the
frame that answered, undecoded. An ``ERROR`` frame is still raised as the driver's error.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hoarddb._core.call import Call, Lane, Op
from hoarddb._core.kinds import Kind
from hoarddb._core.messages import RawRequest, RawResponse


def raw_op(
    opcode: int,
    document: Mapping[str, Any] | None = None,
    *,
    payload: bytes | None = None,
    kind: Kind = "READ",
    lane: Lane = "point",
    timeout: float | None = None,
) -> Op[RawResponse]:
    """The operation: run it with the client's runner (``client._ctx.run``)."""
    reply = yield Call(
        op=f"raw {opcode:#04x}",
        target=None,
        request=RawRequest(opcode, document, payload),
        lane=lane,
        kind=kind,
        timeout=timeout,
    )
    response: RawResponse = reply.response
    return response


def send(
    client: Any,
    opcode: int,
    document: Mapping[str, Any] | None = None,
    *,
    payload: bytes | None = None,
    kind: Kind = "READ",
    lane: Lane = "point",
    timeout: float | None = None,
) -> RawResponse:
    """Send one raw request through a blocking ``hoarddb.Client``."""
    op = raw_op(opcode, document, payload=payload, kind=kind, lane=lane, timeout=timeout)
    response: RawResponse = client._ctx.run(op)  # pyright: ignore[reportPrivateUsage]
    return response


async def asend(
    client: Any,
    opcode: int,
    document: Mapping[str, Any] | None = None,
    *,
    payload: bytes | None = None,
    kind: Kind = "READ",
    lane: Lane = "point",
    timeout: float | None = None,
) -> RawResponse:
    """Send one raw request through an asyncio ``hoarddb.aio.Client``."""
    op = raw_op(opcode, document, payload=payload, kind=kind, lane=lane, timeout=timeout)
    response: RawResponse = await client._ctx.run(op)  # pyright: ignore[reportPrivateUsage]
    return response
