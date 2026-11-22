# SPDX-License-Identifier: Apache-2.0
"""``FieldStore``: one document's store field (spec 4.9).

A store field belongs to each document: ``users.field("ada", "Purchases").push(order)`` pushes onto
Ada's own purchase queue, exactly as ``queue.push(order)`` pushes onto a fifo bucket. A handle is
a few strings and a reference to the client; it does no I/O and caches nothing.
"""

from __future__ import annotations

import builtins
from collections.abc import Mapping
from typing import Any, Generic, TypeVar, cast, overload

from hoarddb._aio.context import Context
from hoarddb._aio.cursor import AsyncCursor
from hoarddb._ops import field as fieldops
from hoarddb._ops import pages
from hoarddb._ops.docs import writes
from hoarddb._ops.refs import BucketRef
from hoarddb.types import FieldBlob, FieldEntry, FieldWriteResult, WriteConcern

E = TypeVar("E")
F = TypeVar("F")


class FieldStore(Generic[E]):
    """The store field ``name`` of the document ``key``; ``field()`` reaches a store nested
    inside one of its elements. The verbs a field answers depend on its declared type (a fifo,
    lifo or heap takes push, pop and peek; a hash, btree or blob takes put, get and delete): a
    verb its type does not answer is InvalidArgumentError, an undeclared field or a parent
    document that does not exist is NotFoundError."""

    __slots__ = ("_bucket", "_ctx", "_path", "key", "name")

    def __init__(
        self,
        ctx: Context,
        bucket: BucketRef,
        key: str,
        name: str,
        path: tuple[str, ...] = (),
    ) -> None:
        self._ctx = ctx
        self._bucket = bucket
        self.key = key
        self.name = name
        self._path = path

    @property
    def _ref(self) -> fieldops.FieldRef:
        return fieldops.FieldRef(self._bucket, self.key, self.name, self._path)

    def __repr__(self) -> str:
        return f"<FieldStore {self._ref.target}>"

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, FieldStore) and self._ctx is other._ctx and self._ref == other._ref  # pyright: ignore[reportUnknownMemberType]
        )

    def __hash__(self) -> int:
        return hash((FieldStore, id(self._ctx), self._ref))

    @overload
    def field(self, element: str, name: str) -> FieldStore[dict[str, Any]]: ...

    @overload
    def field(self, element: str, name: str, *, doc_type: type[F]) -> FieldStore[F]: ...

    def field(
        self, element: str, name: str, *, doc_type: type[Any] | None = None
    ) -> FieldStore[Any]:
        """A store nested inside the element ``element`` of this one. No I/O."""
        self._ctx.guard()
        fieldops.check_nested(element, name, operation="field")
        return FieldStore(
            self._ctx, self._bucket, self.key, name, (*self._path, self.name, element)
        )

    @writes
    async def push(
        self,
        doc: E,
        *,
        priority: int | None = None,
        write_concern: WriteConcern | None = None,
        timeout: float | None = None,
    ) -> FieldWriteResult:
        """Push onto a fifo, lifo or heap field. On a heap, ``priority`` orders the element
        (an int64, 0 when none is given)."""
        wc = self._ctx.wc(write_concern)
        op = fieldops.push(self._ref, doc, priority=priority, wc=wc, timeout=timeout)
        return await self._ctx.run(op)

    @writes
    async def pop(
        self, *, write_concern: WriteConcern | None = None, timeout: float | None = None
    ) -> FieldEntry[E] | None:
        """Take the next element of a fifo, lifo or heap field, or None when it is empty. A pop
        consumes, so it is a write and is never retried."""
        wc = self._ctx.wc(write_concern)
        op = fieldops.pop(self._ref, wc=wc, timeout=timeout)
        return cast("FieldEntry[E] | None", await self._ctx.run(op))

    async def peek(
        self, *, limit: int = 1, timeout: float | None = None
    ) -> builtins.list[FieldEntry[E]]:
        """The next ``limit`` elements (1 to 1000) in pop order, removing none; [] when empty."""
        op = fieldops.peek(self._ref, limit=limit, timeout=timeout)
        return cast("builtins.list[FieldEntry[E]]", await self._ctx.run(op))

    @writes
    async def put(
        self,
        element: str,
        doc: E,
        *,
        write_concern: WriteConcern | None = None,
        timeout: float | None = None,
    ) -> FieldWriteResult:
        """Store ``doc`` under ``element`` in a hash or btree field, replacing what was there."""
        wc = self._ctx.wc(write_concern)
        return await self._ctx.run(fieldops.put(self._ref, element, doc, wc=wc, timeout=timeout))

    async def get(self, element: str, *, timeout: float | None = None) -> FieldEntry[E] | None:
        """The element ``element`` of a hash, btree or blob field, or None when there is none."""
        op = fieldops.get(self._ref, element, timeout=timeout)
        return cast("FieldEntry[E] | None", await self._ctx.run(op))

    @writes
    async def delete(
        self,
        element: str,
        *,
        write_concern: WriteConcern | None = None,
        timeout: float | None = None,
    ) -> bool:
        """Delete the element ``element`` of a hash, btree or blob field. False means nothing
        was there."""
        wc = self._ctx.wc(write_concern)
        return await self._ctx.run(fieldops.delete(self._ref, element, wc=wc, timeout=timeout))

    @writes
    async def put_blob(
        self,
        element: str,
        data: bytes,
        *,
        metadata: Mapping[str, Any] | None = None,
        write_concern: WriteConcern | None = None,
        timeout: float | None = None,
    ) -> FieldWriteResult:
        """Store binary ``data`` under ``element`` of a blob field, with an optional metadata
        document, on the bulk lane."""
        wc = self._ctx.wc(write_concern)
        op = fieldops.put_blob(self._ref, element, data, metadata=metadata, wc=wc, timeout=timeout)
        return await self._ctx.run(op)

    async def get_blob(self, element: str, *, timeout: float | None = None) -> FieldBlob | None:
        """The blob ``element`` of a blob field with its metadata, or None when there is none."""
        return await self._ctx.run(fieldops.get_blob(self._ref, element, timeout=timeout))

    async def len(self, *, timeout: float | None = None) -> int:
        """How many elements the field holds: one round trip. A method named ``len``, not
        ``__len__``: ``len(store)`` that silently did a network round trip is the kind of surprise
        this driver does not offer."""
        return await self._ctx.run(fieldops.length(self._ref, timeout=timeout))

    def list(
        self, *, page_size: int = 100, limit: int | None = None, timeout: float | None = None
    ) -> AsyncCursor[FieldEntry[E]]:
        """A cursor over the field's elements (FIELD_LIST pages of up to 1000, bulk lane). Each
        page continues after the opaque ``next`` of the last one, passed back unchanged. Like
        find(), it is not a snapshot, and it pins nothing. ``len`` is deliberately not
        ``__len__``, and this is the way to walk a field."""
        self._ctx.guard()
        pager = fieldops.list_pager(self._ref, page_size=page_size, limit=limit)
        build = fieldops.list_builder(self._ref, timeout)

        async def fetch() -> builtins.list[FieldEntry[E]] | None:
            rows = await self._ctx.run(pages.next_page(pager, build, fieldops.check_list))
            return None if rows is None else [fieldops.entry(r) for r in rows]

        return AsyncCursor(fetch, lambda: None)
