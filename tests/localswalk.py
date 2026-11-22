# SPDX-License-Identifier: Apache-2.0
"""Find a secret in the frame locals of an exception, the way a crash reporter would.

``traceback.TracebackException.from_exception(err, capture_locals=True)`` is what ``sentry`` style
tools and ``traceback.print_exception(capture_locals=True)`` do: it renders every local of every
frame of the traceback and of the whole cause and context chain.
"""

from __future__ import annotations

import traceback


def frames_showing(err: BaseException, secret: str) -> list[str]:
    """``file:function`` of every captured frame whose rendered locals contain ``secret``."""
    found: list[str] = []
    pending: list[traceback.TracebackException] = [
        traceback.TracebackException.from_exception(err, capture_locals=True)
    ]
    seen: set[int] = set()
    while pending:
        rendered = pending.pop()
        if id(rendered) in seen:
            continue
        seen.add(id(rendered))
        for entry in rendered.stack:
            shown = entry.locals or {}
            if any(secret in text for text in shown.values()):
                found.append(f"{entry.filename.rsplit('/', 1)[-1]}:{entry.name}")
        for linked in (rendered.__cause__, rendered.__context__):
            if linked is not None:
                pending.append(linked)
    return found
