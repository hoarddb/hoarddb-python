# SPDX-License-Identifier: Apache-2.0
"""The AUTH exchange as pure functions (auth.md sections 1 and 2; spec 5.2, 8.3).

Building the request and reading the answer are shared by the blocking and the asyncio connection;
neither function touches a socket.
"""

from __future__ import annotations

from typing import Any

from hoarddb import errors
from hoarddb._bson import deserialize
from hoarddb._core import failure, frame, messages, opcodes
from hoarddb._core.config import ResolvedOptions
from hoarddb._core.messages import AuthOkResponse, AuthRequest
from hoarddb._core.token import Credential
from hoarddb._secret import Secret


def auth_request(
    options: ResolvedOptions, credential: Credential, token: Secret | None
) -> AuthRequest:
    """The AUTH frame's request: the token when that is the credential, else the password.

    ``credential`` is what the token manager says a *new* connection presents; ``token`` is the
    token to present for it. The password is sent only for ``"password"``.
    """
    if credential == "token" and token is not None:
        return AuthRequest(username=options.username, token=token, client=options.client_string)
    return AuthRequest(
        username=options.username, password=options.password, client=options.client_string
    )


def _payload(reply: frame.Frame) -> dict[str, Any]:
    return deserialize(reply.payload) if reply.payload else {}


def read_answer(
    reply: frame.Frame, sent_id: bytes, *, username: str, host_port: str
) -> AuthOkResponse:
    """The answer to the AUTH that was in flight, or the error it stands for.

    A zero request ID is accepted here and only here: in the dial phase the server writes its
    connection-level refusals at accept time, before it reads a request, so the only request that
    can be in flight is this AUTH (spec 5.3).
    """
    zero = failure.is_connection_level(reply.request_id)
    if not zero and reply.request_id != sent_id:
        raise errors.ProtocolError(
            f"The server at {host_port} answered an AUTH with a request ID that is not the one "
            "sent. The connection was discarded.",
            operation="connect",
            target=host_port,
        )
    if reply.opcode == opcodes.AUTH_OK:
        if zero:
            raise errors.ProtocolError(
                f"The server at {host_port} answered AUTH_OK with a zero request ID. "
                "The connection was discarded.",
                operation="connect",
                target=host_port,
            )
        parsed: AuthOkResponse = messages.parse_response(AuthRequest, reply)
        return parsed
    if reply.opcode == opcodes.AUTH_FAIL:
        raise failure.auth_failure_from_payload(
            _payload(reply), reply.request_id, username=username, host_port=host_port
        )
    if reply.opcode == opcodes.ERROR:
        raise failure.error_from_payload(
            _payload(reply),
            reply.request_id,
            failure.Context(operation="connect", target=host_port, kind="READ"),
        )
    name = opcodes.NAMES.get(reply.opcode, f"{reply.opcode:#04x}")
    raise errors.ProtocolError(
        f"The server at {host_port} answered AUTH with {name}, which is not an answer to it. "
        "The connection was discarded.",
        operation="connect",
        target=host_port,
    )
