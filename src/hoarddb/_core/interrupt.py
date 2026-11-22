# SPDX-License-Identifier: Apache-2.0
"""What a connection reports when an exchange did not complete (spec 6.2 item 4, 8.1).

The connection layers raise :class:`Interrupted`; the runner turns it into a retry decision
(``retry.Failure``) and, when the call is not retried, into the public error.
"""

from __future__ import annotations

from typing import Literal

from hoarddb._core import diagnostics
from hoarddb._core.call import Call
from hoarddb._core.retry import Failure
from hoarddb.errors import HoardDBError

Cause = Literal["lost", "timeout"]


class Interrupted(Exception):
    """The exchange stopped before an answer arrived.

    ``sent`` is False only when no request byte can have left the process (the deadline had passed,
    or the connection was found dead before the write began); True from the first byte written.
    """

    def __init__(self, cause: Cause, *, sent: bool, reason: str) -> None:
        super().__init__(reason)
        self.cause: Cause = cause
        self.sent = sent
        self.reason = reason


def interrupted(
    call: Call, host: object, lost: Interrupted, seconds: float
) -> tuple[Failure | None, HoardDBError]:
    """What an interrupted exchange means: the retry table's failure (None for a timeout, which
    is never retried because the deadline is spent) and the error the caller finally sees."""
    if lost.cause == "timeout":
        return None, diagnostics.request_timeout(
            call.op, call.target, seconds, call.kind, sent=lost.sent
        )
    error = diagnostics.connection_lost(str(host), call.op, lost.reason, call.kind, sent=lost.sent)
    return Failure("lost_after_write" if lost.sent else "closed_before_write"), error
