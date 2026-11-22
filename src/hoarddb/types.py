# SPDX-License-Identifier: Apache-2.0
"""Public result and declaration types (spec 4.4, 4.5, 4.10).

Every type here is a frozen ``dataclass`` with ``slots``; none holds a connection, and none has a
hidden cost. ``from __future__ import annotations`` is needed by ``StoreSpec`` and ``FieldSpec``,
which refer to themselves.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Generic, Literal, TypedDict, TypeVar

T = TypeVar("T")

WriteConcern = Literal["one", "majority", "all"]
Document = dict[str, Any]


class StoreType(StrEnum):
    """The six store types. The literal strings are accepted everywhere a ``StoreType`` is:
    ``create_bucket("users", "hash")``. The wire words carry no ``Store`` suffix."""

    HASH = "hash"
    BTREE = "btree"
    FIFO = "fifo"
    LIFO = "lifo"
    HEAP = "heap"
    BLOB = "blob"


@dataclass(frozen=True, slots=True)
class StoreSpec:
    """A store field every document of a bucket carries (``users.field("ada", "Purchases")``)."""

    name: str
    type: StoreType | str
    stores: Sequence[StoreSpec] = ()


@dataclass(frozen=True, slots=True)
class IndexSpec:
    """A secondary index. Indexes (and so ``unique``) exist on hash buckets only."""

    fields: Sequence[str]
    unique: bool = False
    name: str | None = None
    expire_after_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One typed field of a bucket's declaration. ``members`` declares an object's fields and
    ``elem`` an array's element type."""

    name: str
    type: str
    required: bool = False
    members: Sequence[FieldSpec] = ()
    elem: FieldSpec | None = None


@dataclass(frozen=True, slots=True)
class FieldTypes:
    """Typed fields and how they are enforced (``strict`` when ``mode`` is left out)."""

    fields: Sequence[FieldSpec]
    mode: Literal["strict", "warn", "off"] | None = None


@dataclass(frozen=True, slots=True)
class DatabaseInfo:
    name: str
    bucket_count: int


@dataclass(frozen=True, slots=True)
class BucketInfo:
    """One bucket's row of ``Database.list_buckets()``."""

    name: str
    type: str
    entries: int
    stores: tuple[str, ...]
    replication_factor: int | None
    inherited: bool


@dataclass(frozen=True, slots=True)
class StoreFieldInfo:
    name: str
    engine: str  # "hash" | "btree" | "fifo" | "lifo" | "heap" | "blob", or a newer word, unchanged


@dataclass(frozen=True, slots=True)
class BucketDeclaration:
    """What ``info()`` returns: the bucket's declaration, without walking any store."""

    name: str
    type: str  # a StoreType value for the six; a word this driver does not know passes through
    entries: int  # the bucket's own document count, not its stores' elements
    stores: tuple[str, ...]  # declared top-level store field names, in declaration order
    store_fields: tuple[StoreFieldInfo, ...]  # the same fields with their engines, same order
    replication_factor: int | None  # None: the server sent none
    inherited: bool  # the replication factor comes from the database; False when absent


@dataclass(frozen=True, slots=True)
class StoreStat:
    name: str
    engine: str
    parent: str
    elements: int


@dataclass(frozen=True, slots=True)
class DeclaredField:
    name: str
    type: str
    required: bool
    store: str


@dataclass(frozen=True, slots=True)
class BucketStats:
    """What ``stats()`` returns. It counts every store element, so it is not cheap on a bucket
    with large store fields; use ``info()`` for the declaration alone."""

    name: str
    type: str
    entries: int
    logical_bytes: int
    indexes: tuple[str, ...]
    stores: tuple[StoreStat, ...]
    encrypted: str
    fields: tuple[DeclaredField, ...]
    field_types: str
    writes_refused: bool


@dataclass(frozen=True, slots=True)
class Health:
    """Display facts about the node that answered (a driver never decides from them)."""

    node_id: str
    uptime: str
    version: str
    bucket_count: int
    healthy: bool
    membership: str
    store_refusal: str
    capacity: Mapping[str, Any] | None = None
    memtable: Mapping[str, Any] | None = None
    encryption: Mapping[str, Any] | None = None
    host: Mapping[str, Any] | None = None
    reasons: tuple[Any, ...] = ()


@dataclass(frozen=True, slots=True)
class NodeInfo:
    node_id: str
    addr: str  # the node's cluster address: never a dial target
    fingerprint: str
    joined_at: Any
    status: str


@dataclass(frozen=True, slots=True)
class ServerInfo:
    """What the server said at ``AUTH``; no round trip."""

    version: str
    protocol_version: int
    capabilities: tuple[str, ...]
    password_min_length: int


@dataclass(frozen=True, slots=True)
class PoolStats:
    """Aggregate pool counters. Never per bucket or per database: the pool is per client."""

    point_size: int
    bulk_size: int
    point_in_use: int
    bulk_in_use: int
    point_idle: int
    bulk_idle: int
    waiting: int
    dials: int
    dial_failures: int
    discarded: int
    checkouts: int
    closed: bool


@dataclass(frozen=True, slots=True)
class WriteResult:
    id: str
    seq: int | None = None
    copies_confirmed: int | None = None  # None: a single node, or an older server
    replication_factor: int | None = None


@dataclass(frozen=True, slots=True)
class BatchResult:
    ids: list[str] = field(default_factory=list[str])
    count: int = 0
    copies_confirmed: int | None = None
    replication_factor: int | None = None


@dataclass(frozen=True, slots=True)
class Entry(Generic[T]):
    """One key and its document: ``range``, ``first``, ``last`` of a btree bucket."""

    key: str
    value: T


@dataclass(frozen=True, slots=True)
class Blob:
    """What ``get_blob`` returns: the bytes and the metadata document stored with them."""

    key: str
    blob_id: str
    size: int
    data: bytes
    metadata: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True, slots=True)
class BlobWriteResult:
    key: str
    blob_id: str
    size: int
    copies_confirmed: int | None = None
    replication_factor: int | None = None


UpdateOps = TypedDict(
    "UpdateOps",
    {
        "$set": Mapping[str, Any],
        "$unset": Sequence[str],
        "$inc": Mapping[str, int],
        "$push": Mapping[str, Any],
        "$addToSet": Mapping[str, Any],
        "$mul": Mapping[str, int | float],
        "$min": Mapping[str, Any],
        "$max": Mapping[str, Any],
        "$rename": Mapping[str, str],
    },
    total=False,
)
"""The nine update operators, spelled as the HQL writes them (the only way to spell keys that
start with ``$`` as a ``TypedDict``). A key may be a path (``"Address.City"``, ``"Lines[0].Qty"``)
and is sent verbatim."""

Filter = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class FieldWriteResult:
    """What a store-field write reports: where the element went, by engine (``element_key`` for a
    hash, btree or blob field; ``entry_id`` and ``priority`` for a queue or heap)."""

    element_key: str = ""
    entry_id: str = ""
    priority: int | None = None
    blob_id: str = ""
    copies_confirmed: int | None = None
    replication_factor: int | None = None


@dataclass(frozen=True, slots=True)
class FieldEntry(Generic[T]):
    """One element of a store field and where it is."""

    element: T | None
    element_key: str = ""
    position: int = 0
    priority: int = 0
    entry_id: str = ""


@dataclass(frozen=True, slots=True)
class FieldBlob:
    """A blob element of a store field: its bytes and its metadata document."""

    element_key: str
    blob_id: str
    content: bytes
    metadata: dict[str, Any] = field(default_factory=dict[str, Any])
