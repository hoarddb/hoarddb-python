# SPDX-License-Identifier: Apache-2.0
"""The public asyncio cursor (spec 4.8): the blocking cursor's twin over an awaitable fetch."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Generic, Self, TypeVar

D = TypeVar("D")


class AsyncCursor(Generic[D]):
    """Iterate a result page by page: ``async for doc in cursor``, ``await cursor.to_list()`` or
    ``async for page in cursor.batches()``. Pins nothing; one consumer per cursor; not a
    snapshot."""

    __slots__ = ("_buffer", "_closed", "_done", "_fetch", "_pos", "_total")

    def __init__(
        self,
        fetch: Callable[[], Awaitable[list[D] | None]],
        total: Callable[[], int | None],
    ) -> None:
        self._fetch = fetch
        self._total = total
        self._buffer: list[D] = []
        self._pos = 0
        self._closed = False
        self._done = False

    async def _refill(self) -> bool:
        """Make the next non-empty page current; False when the walk is over."""
        while self._pos >= len(self._buffer):
            if self._closed or self._done:
                return False
            try:
                page = await self._fetch()
            except BaseException:
                self._done = True  # an error (or a cancellation) ends the walk
                raise
            if page is None:
                self._done = True
                return False
            self._buffer, self._pos = page, 0
        return True

    def __aiter__(self) -> AsyncIterator[D]:
        return self

    async def __anext__(self) -> D:
        if not await self._refill():
            raise StopAsyncIteration
        item = self._buffer[self._pos]
        self._pos += 1
        return item

    async def batches(self) -> AsyncIterator[list[D]]:
        """One page at a time, for bulk processing."""
        while await self._refill():
            page = self._buffer[self._pos :]
            self._pos = len(self._buffer)
            yield page

    async def to_list(self) -> list[D]:
        """Every remaining document. Reads the whole result into memory."""
        out: list[D] = []
        async for page in self.batches():
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

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.close()
