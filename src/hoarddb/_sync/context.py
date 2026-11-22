# SPDX-License-Identifier: Apache-2.0
"""What a client's handles share: the runner, the pool and the guards. One per client.

Handles (``Database``, the bucket facets, ``FieldStore``) keep a reference to this and two names;
nothing else. Every public call goes through :meth:`run`, whose first statement is the process
check, taken before any lock.
"""

from __future__ import annotations

import os
from typing import TypeVar

from hoarddb._core import diagnostics
from hoarddb._core.call import Op
from hoarddb._core.config import ResolvedOptions
from hoarddb._poolstate import ServerFacts
from hoarddb._sync.pool import Pool
from hoarddb._sync.run import Runner

T = TypeVar("T")


class Context:
    __slots__ = ("closed", "forked", "options", "pid", "pool", "runner")

    def __init__(self, options: ResolvedOptions, pool: Pool, runner: Runner) -> None:
        self.options = options
        self.pool = pool
        self.runner = runner
        self.pid = os.getpid()
        self.forked = False
        self.closed = False

    def guard(self) -> None:
        """The first statement of every public call (spec 3.5)."""
        if self.forked or os.getpid() != self.pid:
            raise diagnostics.forked_client(self.pid, os.getpid())
        if self.closed:
            raise diagnostics.client_closed()

    def run(self, op: Op[T]) -> T:
        self.guard()
        return self.runner.run(op)

    def wc(self, given: str | None) -> str | None:
        """The call's write concern, else the client's default (None: the server's)."""
        return given if given is not None else self.options.write_concern

    @property
    def facts(self) -> ServerFacts | None:
        return self.pool.server
