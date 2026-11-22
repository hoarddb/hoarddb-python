# SPDX-License-Identifier: Apache-2.0
"""``os.fork()`` and clients (spec 3.5).

Neither client survives a fork: the connections belong to the parent, and a socket shared by two
processes would interleave frames. A hook registered once, at import, marks every live client as
forked in the child (one weak entry per client, never per bucket). The first public call on a
forked client then raises ``ForkedClientError``, and closing it drops the inherited descriptors
without a ``shutdown`` (which would reset the parent's connection).
"""

from __future__ import annotations

import os
import weakref
from typing import Protocol


class ForkAware(Protocol):
    def mark_forked(self) -> None: ...


_LIVE: weakref.WeakSet[ForkAware] = weakref.WeakSet()


def register(target: ForkAware) -> None:
    _LIVE.add(target)


def _after_fork_in_child() -> None:
    for target in list(_LIVE):
        target.mark_forked()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)
