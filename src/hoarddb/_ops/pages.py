# SPDX-License-Identifier: Apache-2.0
"""One cursor page as an operation (spec 4.8): shared by the blocking and the asyncio cursor.

The pager machines of :mod:`hoarddb._core.paging` decide what to ask for next; this wraps them in
the generator protocol. ``RESULT_TOO_LARGE`` halves the page and asks again, as often as needed
(at most ``log2(page_size) + 1`` attempts per page); a single document that is still too large
raises ``ResultTooLargeError`` with both numbers. Any other error ends the walk: in particular the
refusal of a dropped and re-created bucket is raised, never retried as a fresh scan.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from hoarddb import errors
from hoarddb._core.call import Call, Op, Reply
from hoarddb._core.messages import Request


class Pager(Protocol):
    page_size: int

    def next_request(self) -> Any: ...

    def accept(self, response: Any) -> list[Any]: ...

    def too_large(self, error: errors.ResultTooLargeError) -> None: ...

    def abandon(self) -> None: ...


Check = Callable[[Call, Reply], None]


def next_page(
    pager: Pager, build: Callable[[Request], Call], check: Check | None = None
) -> Op[list[Any] | None]:
    """The next page's rows, or None once the walk is over. ``check`` may raise for an in-band
    refusal (a scan of a bucket that does not exist answers ``found: false``)."""
    while True:
        request = pager.next_request()
        if request is None:
            return None
        sent = build(request)
        try:
            reply: Reply = yield sent
            if check is not None:
                check(sent, reply)
        except errors.ResultTooLargeError as too_large:
            pager.too_large(too_large)  # halves the page, or raises when it cannot
            continue
        except errors.HoardDBError:
            pager.abandon()
            raise
        return pager.accept(reply.response)
