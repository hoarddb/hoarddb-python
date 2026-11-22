# SPDX-License-Identifier: Apache-2.0
"""Public-client test kit: a client of either surface against a scripted fake server.

``PC.call(fn)`` calls ``fn(client)`` and, for the asyncio client, awaits the result on the
harness's loop, so one test body checks both surfaces.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import Callable, Generator, Mapping, Sequence
from typing import Any, Protocol, cast

import fakehoard
import faketls
import hoarddb
import hoarddb.aio
from hoarddb._bson import deserialize
from hoarddb._core import frame, opcodes

Actions = Sequence[fakehoard.Action]
Answer = Actions | Callable[[dict[str, Any]], Actions]


def reply(opcode: int, doc: Mapping[str, Any] | None = None) -> Actions:
    return [fakehoard.Reply(opcode, dict(doc or {}))]


def ok(opcode: int, message: str = "") -> Actions:
    return reply(opcode, {"success": True, "message": message})


class Script:
    """Answers each non-AUTH request by opcode: ``Script({opcodes.PUT: [...]})``. A list of
    answers is consumed one per request (the last repeats); a callable gets the decoded request."""

    def __init__(self, answers: Mapping[int, Answer | Sequence[Answer]] | None = None) -> None:
        self.answers: dict[int, Any] = dict(answers or {})
        self.requests: list[tuple[int, dict[str, Any]]] = []

    def __call__(self, conn: fakehoard.Conn, request: frame.Frame) -> Actions:
        try:
            doc = deserialize(request.payload) if request.payload else {}
        except Exception:
            doc = {}
        self.requests.append((request.opcode, doc))
        wanted: Any = self.answers.get(request.opcode)
        if wanted is None:
            name = opcodes.NAMES.get(request.opcode, hex(request.opcode))
            return [fakehoard.error(5, f"script: no answer for {name}")]
        if callable(wanted):
            return cast(Actions, wanted(doc))
        if wanted and isinstance(wanted[0], list | tuple):
            queue: list[Any] = wanted
            step: Actions = queue.pop(0) if len(queue) > 1 else queue[0]
            return step
        answer: Actions = wanted
        return answer

    def of(self, opcode: int) -> list[dict[str, Any]]:
        return [doc for op, doc in self.requests if op == opcode]


class HasPort(Protocol):
    """Anything a client can dial on loopback: the fake server, or a recording tap."""

    port: int


class PC:
    """A connected public client, blocking or asyncio."""

    def __init__(self, mode: str, server: HasPort, identity: faketls.Identity, **kw: Any):
        self.mode = mode
        self.server = server
        uri = kw.pop("uri", f"hoarddb://admin:pw@127.0.0.1:{server.port}")
        options: dict[str, Any] = {
            "tls": hoarddb.TlsOptions(fingerprints=[identity.fingerprint]),
            **kw,
        }
        self.loop: asyncio.AbstractEventLoop | None = None
        self.client: Any
        if mode == "sync":
            self.client = hoarddb.connect(uri, **options)
        else:
            self.loop = asyncio.new_event_loop()

            async def open_it() -> Any:
                return await hoarddb.aio.connect(uri, **options)

            try:
                self.client = self.loop.run_until_complete(open_it())
            except BaseException:  # a refused connect must not leak its loop
                self.loop.run_until_complete(asyncio.sleep(0))
                self.loop.close()
                raise

    def call(self, fn: Callable[[Any], Any]) -> Any:
        result = fn(self.client)
        if inspect.isawaitable(result):
            assert self.loop is not None
            return self.loop.run_until_complete(_await(result))
        return result

    def close(self) -> None:
        if self.loop is None:
            self.client.close()
        else:
            self.loop.run_until_complete(self.client.close())
            self.loop.run_until_complete(asyncio.sleep(0))
            self.loop.close()


async def _await(awaitable: Any) -> Any:
    return await awaitable


@contextlib.contextmanager
def public(
    mode: str,
    identity: faketls.Identity,
    script: Script | None = None,
    **kw: Any,
) -> Generator[tuple[PC, Script, fakehoard.FakeHoard], None, None]:
    """A fake server running ``script`` and a client of ``mode`` connected to it."""
    script = script or Script()
    server_kw = {k: kw.pop(k) for k in ("on_auth", "greeting_fn") if k in kw}
    with fakehoard.FakeHoard(identity, handler=script, **server_kw) as server:
        pc = PC(mode, server, identity, **kw)
        try:
            yield pc, script, server
        finally:
            pc.close()
