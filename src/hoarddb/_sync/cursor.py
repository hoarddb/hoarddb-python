# SPDX-License-Identifier: Apache-2.0
"""The public blocking cursor (spec 4.8): a thin shell over a page-fetching function.

The server has no cursor. Each page is an independent request on the bulk lane, so a cursor pins
nothing (no socket, no server state, no timer, no thread) and abandoning one is free. One consumer
per cursor: a ``Cursor`` is not thread-safe. It is not a snapshot either: between pages a
concurrent insert ahead of the boundary can show a document twice and a delete can hide one.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Generic, Self, TypeVar

D = TypeVar("D")


class Cursor(Generic[D]):
    """Iterate a result page by page. ``for doc in cursor``, ``cursor.to_list()`` or
    ``for page in cursor.batches()``. Closing it (or leaving a ``with`` block) stops further
    fetches and releases nothing, because nothing is held."""

    __slots__ = ("_buffer", "_closed", "_done", "_fetch", "_pos", "_total")

    def __init__(
        self, fetch: Callable[[], list[D] | None], total: Callable[[], int | None]
    ) -> None:
        self._fetch = fetch
        self._total = total
        self._buffer: list[D] = []
        self._pos = 0
        self._closed = False
        self._done = False

    def _refill(self) -> bool:
        """Make the next non-empty page current; False when the walk is over."""
        while self._pos >= len(self._buffer):
            if self._closed or self._done:
                return False
            try:
                page = self._fetch()
            except BaseException:
                self._done = True  # an error ends the walk; starting again is the caller's choice
                raise
            if page is None:
                self._done = True
                return False
            self._buffer, self._pos = page, 0
        return True

    def __iter__(self) -> Iterator[D]:
        return self

    def __next__(self) -> D:
        if not self._refill():
            raise StopIteration
        item = self._buffer[self._pos]
        self._pos += 1
        return item

    def batches(self) -> Iterator[list[D]]:
        """One page at a time, for bulk processing."""
        while self._refill():
            page = self._buffer[self._pos :]
            self._pos = len(self._buffer)
            yield page

    def to_list(self) -> list[D]:
        """Every remaining document. Reads the whole result into memory."""
        out: list[D] = []
        for page in self.batches():
            out.extend(page)
        return out

    @property
    def total(self) -> int | None:
        """From the first page: for ``find`` the exact match count, for ``search`` the number
        returned. None until the first page has been read."""
        return self._total()

    def close(self) -> None:
        """Stop further fetches. Idempotent; releases nothing on the server."""
        self._closed = True
        self._buffer, self._pos = [], 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
