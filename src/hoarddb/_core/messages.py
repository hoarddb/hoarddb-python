# SPDX-License-Identifier: Apache-2.0
"""One request type and one response type per opcode (operations.md, wire.md section 4).

Wire names are written out next to the fields that differ from their Python names (``writeconcern``,
``addtoset``, ``blobid``, ``bucketcount``, ``nodeid``, ``joinedat``, ``schemajson``) and are pinned
by the vectors. A request knows its opcode and which response opcodes may answer it; the success
response is a dataclass built by :func:`parse_response`, and an ``ERROR`` frame is not a response
(``failure.error_from_payload`` handles it).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

from hoarddb import errors
from hoarddb._bson import deserialize, serialize
from hoarddb._core import frame, opcodes, schema
from hoarddb._core.schema import model, wire
from hoarddb._secret import Secret

_DOC = Mapping[str, Any]


# =================================================================================================
# Responses
# =================================================================================================


class Response:
    """Base of the success responses."""

    __slots__ = ()


@model
class _Outcome(Response):
    """What every data mutation's success also carries (errors.md section 4)."""

    copies_confirmed: int | None = None
    replication_factor: int | None = None
    write_outcome: str = ""


@model
class AuthOkResponse(Response):
    success: bool = False
    token: Secret | None = None
    message: str = ""
    token_expires_at: int = 0
    token_deadline: int = 0
    protocol_version: int = 0
    server_version: str = ""
    capabilities: list[str] = field(default_factory=list[str])
    password_min_length: int = 0


@model
class WriteResponse(_Outcome):
    """The answer to a put, an upsert, an insert, a btree insert and every push."""

    id: str = ""
    success: bool = False
    message: str = ""
    seq: int = 0


@model
class BatchPutResponse(_Outcome):
    ids: list[str] = field(default_factory=list[str])
    count: int = 0
    success: bool = False
    message: str = ""
    results: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])


@model
class GetResponse(Response):
    """GET_RESPONSE and BTREE_RESPONSE for a lookup."""

    found: bool = False
    data: dict[str, Any] | None = None
    message: str = ""


@model
class ConsistentGetResponse(Response):
    found: bool = False
    data: dict[str, Any] | None = None
    version: int = 0
    repaired: bool = False
    message: str = ""


@model
class ExistsResponse(Response):
    exists: bool = False


@model
class DeleteResponse(_Outcome):
    success: bool = False
    message: str = ""
    spared: bool = False
    skipped: bool = False


@model
class UpdateResponse(_Outcome):
    success: bool = False
    doc: dict[str, Any] | None = None
    message: str = ""
    doc_omitted: bool = False  # set only inside a BATCH, which this driver does not send


@model
class PopResponse(_Outcome):
    """The entries a pop removed, in pop order. There is no single-entry member: a pop of one
    answers a one-item list."""

    found: bool = False
    items: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    message: str = ""
    pending: int = 0  # entries held under a lease, which a pop does not take
    dead: int = 0


@model
class PeekResponse(Response):
    found: bool = False
    items: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    message: str = ""
    pending: int = 0
    dead: int = 0
    expired: int = 0


@model
class RangeEntry:
    key: str = ""
    value: dict[str, Any] | None = None


@model
class RangeResponse(Response):
    entries: list[RangeEntry] = field(default_factory=list[RangeEntry])
    message: str = ""


@model
class BlobWriteResponse(_Outcome):
    success: bool = False
    key: str = ""
    blob_id: str = wire("blobid", default="")
    size: int = 0
    message: str = ""


@model
class BlobReadResponse(Response):
    found: bool = False
    blob_id: str = wire("blobid", default="")
    data: bytes = b""
    size: int = 0
    metadata: dict[str, Any] | None = None
    message: str = ""


@model
class OkResponse(Response):
    """CREATE_DB, DELETE_DB, CREATE_BUCKET, DELETE_BUCKET and DECLARE_STORE_FIELDS."""

    success: bool = False
    message: str = ""
    placement: str = ""  # CREATE_BUCKET only: how the server placed the bucket


@model
class DatabaseRow:
    name: str = ""
    bucket_count: int = wire("bucketcount", default=0)


@model
class ListDatabasesResponse(Response):
    databases: list[DatabaseRow] = field(default_factory=list[DatabaseRow])
    message: str = ""


@model
class BucketRow:
    name: str = ""
    type: str = ""
    entries: int = 0
    stores: list[str] = field(default_factory=list[str])
    replication_factor: int | None = None
    inherited: bool = False
    outside: Any = None
    # Where the bucket's copies stand, and its queue's held entries: display facts, kept as sent.
    state: str = ""
    in_sync: int = 0
    not_in_sync: list[Any] = field(default_factory=list[Any])
    pending_copies: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    pending: int = 0
    dead: int = 0


@model
class ListBucketsResponse(Response):
    found: bool = False
    database: str = ""
    buckets: list[BucketRow] = field(default_factory=list[BucketRow])
    message: str = ""
    encrypted: str = ""


@model
class StoreFieldRow:
    name: str = ""
    engine: str = ""


@model
class BucketInfoResponse(Response):
    found: bool = False
    database: str = ""
    bucket: BucketRow | None = None
    store_fields: list[StoreFieldRow] = field(default_factory=list[StoreFieldRow])


@model
class StoreStat:
    name: str = ""
    engine: str = ""
    parent: str = ""
    elements: int = 0


@model
class DeclaredField:
    name: str = ""
    type: str = ""
    required: bool = False
    store: str = ""


@model
class BucketStatsResponse(Response):
    found: bool = False
    database: str = ""
    bucket: str = ""
    type: str = ""
    entries: int = 0
    logical_bytes: int = 0
    indexes: list[str] = field(default_factory=list[str])
    message: str = ""
    stores: list[StoreStat] = field(default_factory=list[StoreStat])
    encrypted: str = ""
    fields: list[DeclaredField] = field(default_factory=list[DeclaredField])
    field_types: str = ""
    writes_refused: bool = False
    # Placement and copy state: display facts, kept as sent.
    placement: str = ""
    replication_factor: int | None = None
    inherited: bool = False
    state: str = ""
    in_sync: int = 0
    not_in_sync: list[Any] = field(default_factory=list[Any])
    nodes: list[Any] = field(default_factory=list[Any])
    pending_copies: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    pending_copy: Any = None
    previous_owner: Any = None


@model
class CountResponse(Response):
    count: int = 0
    message: str = ""
    pending: int = 0
    dead: int = 0
    expired: int = 0


@model
class DistinctResponse(Response):
    values: list[Any] = field(default_factory=list[Any])
    message: str = ""


@model
class SearchResponse(Response):
    results: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    total: int = 0
    has_more: bool = False
    message: str = ""
    index_used: str = ""


@model
class ScanResponse(Response):
    found: bool = False
    database: str = ""
    bucket: str = ""
    documents: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    total: int = 0
    has_more: bool = False
    message: str = ""
    bucket_id: int = 0
    index_used: str = ""


@model
class HealthResponse(Response):
    """Display facts about a node; the nested parts are kept as documents (a driver never decides
    anything from them, operations.md section 4)."""

    node_id: str = wire("nodeid", default="")
    uptime: str = ""
    version: str = ""
    bucket_count: int = wire("bucketcount", default=0)
    healthy: bool = False
    capacity: dict[str, Any] | None = None
    memtable: dict[str, Any] | None = None
    encryption: dict[str, Any] | None = None
    membership: str = ""
    membership_detail: Any = None
    store_refusal: str = ""
    host: dict[str, Any] | None = None
    wal: dict[str, Any] | None = None
    duplicates: Any = None
    reasons: list[Any] = field(default_factory=list[Any])


@model
class NodeRow:
    node_id: str = wire("nodeid", default="")
    addr: str = ""  # a node's cluster address: never a dial target
    fingerprint: str = ""
    joined_at: Any = wire("joinedat", default=None)
    status: str = ""
    duplicate: Any = None


@model
class ListNodesResponse(Response):
    nodes: list[NodeRow] = field(default_factory=list[NodeRow])
    history_reported: bool = False
    membership_history: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    schema_changes: dict[str, Any] | None = None
    epoch: int = 0
    member_count: int = 0
    standalone: bool = False


@model
class FieldItem:
    element: dict[str, Any] | None = None
    element_key: str = ""
    position: int = 0
    priority: int = 0
    entry_id: str = ""
    blob_id: str = ""


@model
class FieldResponse(_Outcome):
    success: bool = False
    message: str = ""
    found: bool = False
    element: dict[str, Any] | None = None
    element_key: str = ""
    position: int = 0
    priority: int = 0
    entry_id: str = ""
    len: int = 0
    items: list[FieldItem] = field(default_factory=list[FieldItem])
    blob_id: str = ""
    content: bytes = b""


@model
class FieldListResponse(Response):
    success: bool = False
    message: str = ""
    entries: list[FieldItem] = field(default_factory=list[FieldItem])
    has_more: bool = False
    next: bytes = b""


@model
class PongResponse(Response):
    pass


@dataclass(frozen=True, slots=True)
class GetFastResponse(Response):
    """GET_FAST_RESPONSE: one status byte (0 found, 1 not found), then the document when found."""

    found: bool
    data: dict[str, Any] | None

    @classmethod
    def from_payload(cls, payload: bytes) -> GetFastResponse:
        if not payload:
            raise _bad_fast("is empty")
        status = payload[0]
        if status == 0:
            return cls(True, deserialize(payload[1:]))
        if status == 1 and len(payload) == 1:
            return cls(False, None)
        raise _bad_fast(f"has status byte {status} and {len(payload) - 1} bytes after it")


def _bad_fast(what: str) -> errors.ProtocolError:
    return errors.ProtocolError(
        f"The server sent a GET_FAST answer that {what}. The connection was discarded.",
        operation="receive",
    )


# =================================================================================================
# Requests
# =================================================================================================


class Request:
    """Base of the requests. ``OPCODE`` is what is sent; ``RESPONSE_OPCODES`` are the success
    opcodes that may answer it (``ERROR`` may always answer); ``RESPONSE`` is the type built."""

    __slots__ = ()
    OPCODE: ClassVar[int]
    RESPONSE: ClassVar[type[Any]]
    RESPONSE_OPCODES: ClassVar[frozenset[int]]


@model
class AuthRequest(Request):
    OPCODE: ClassVar[int] = opcodes.AUTH
    username: str
    password: Secret | None = wire(default=None)
    protocol_version: int = wire(default=1, always=True)
    capabilities: list[str] = wire(default_factory=list[str], always=True)  # [] today
    client: str = ""
    token: Secret | None = wire(default=None)  # when present, the password is ignored


@model
class _BucketRequest(Request):
    database: str
    bucket: str


@model
class _KeyRequest(_BucketRequest):
    key: str = ""


@model
class PutRequest(_KeyRequest):
    OPCODE: ClassVar[int] = opcodes.PUT
    data: _DOC = wire(default_factory=dict[str, Any], always=True)
    write_concern: str = wire("writeconcern", default="")


@model
class UpsertRequest(PutRequest):
    """PUT's replacing form: the same members, answered with ``UPSERT_OK``."""

    OPCODE: ClassVar[int] = opcodes.UPSERT


@model
class BTreeInsertRequest(PutRequest):
    OPCODE: ClassVar[int] = opcodes.BTREE_INSERT


@model
class GetRequest(_KeyRequest):
    OPCODE: ClassVar[int] = opcodes.GET


@model
class BTreeLookupRequest(_KeyRequest):
    OPCODE: ClassVar[int] = opcodes.BTREE_LOOKUP


@model
class ConsistentGetRequest(_KeyRequest):
    OPCODE: ClassVar[int] = opcodes.CONSISTENT_GET


@model
class ExistsRequest(_KeyRequest):
    OPCODE: ClassVar[int] = opcodes.EXISTS


@model
class BlobReadRequest(_KeyRequest):
    OPCODE: ClassVar[int] = opcodes.BLOB_READ


@model
class DeleteRequest(_KeyRequest):
    OPCODE: ClassVar[int] = opcodes.DELETE
    expire_if: _DOC | None = None
    write_concern: str = wire("writeconcern", default="")


@model
class BatchPutRequest(_BucketRequest):
    OPCODE: ClassVar[int] = opcodes.BATCH_PUT
    data: list[_DOC] = wire(default_factory=list[_DOC], always=True)
    write_concern: str = wire("writeconcern", default="")


@model
class _PushRequest(_BucketRequest):
    data: _DOC = wire(default_factory=dict[str, Any], always=True)
    # Sent on every push, as int64, 0 when the caller gives none: the wire shape of a push does
    # not depend on the store type (spec 4.6).
    priority: int = wire(default=0, always=True, conv="int64")
    write_concern: str = wire("writeconcern", default="")


@model
class FifoPushRequest(_PushRequest):
    OPCODE: ClassVar[int] = opcodes.FIFO_PUSH


@model
class LifoPushRequest(_PushRequest):
    OPCODE: ClassVar[int] = opcodes.LIFO_PUSH


@model
class HeapPushRequest(_PushRequest):
    OPCODE: ClassVar[int] = opcodes.HEAP_PUSH


@model
class _PopRequest(_BucketRequest):
    limit: int = 0  # absent means 1; the server takes at most 100
    write_concern: str = wire("writeconcern", default="")


@model
class FifoPopRequest(_PopRequest):
    OPCODE: ClassVar[int] = opcodes.FIFO_POP


@model
class LifoPopRequest(_PopRequest):
    OPCODE: ClassVar[int] = opcodes.LIFO_POP


@model
class HeapPopRequest(_PopRequest):
    OPCODE: ClassVar[int] = opcodes.HEAP_POP


@model
class _PeekRequest(_BucketRequest):
    limit: int = 0  # absent or 0 means 1


@model
class FifoPeekRequest(_PeekRequest):
    OPCODE: ClassVar[int] = opcodes.FIFO_PEEK


@model
class LifoPeekRequest(_PeekRequest):
    OPCODE: ClassVar[int] = opcodes.LIFO_PEEK


@model
class _RangeRequest(_BucketRequest):
    low: str = ""  # an empty side is open
    high: str = ""
    limit: int = 0


@model
class BTreeRangeRequest(_RangeRequest):
    OPCODE: ClassVar[int] = opcodes.BTREE_RANGE


@model
class BTreeFirstRequest(_RangeRequest):
    OPCODE: ClassVar[int] = opcodes.BTREE_FIRST


@model
class BTreeLastRequest(_RangeRequest):
    OPCODE: ClassVar[int] = opcodes.BTREE_LAST


@model
class BlobWriteRequest(_KeyRequest):
    OPCODE: ClassVar[int] = opcodes.BLOB_WRITE
    data: bytes = wire(default=b"", always=True)
    metadata: _DOC | None = None
    write_concern: str = wire("writeconcern", default="")


@model
class CreateDbRequest(Request):
    OPCODE: ClassVar[int] = opcodes.CREATE_DB
    name: str


@model
class DeleteDbRequest(Request):
    OPCODE: ClassVar[int] = opcodes.DELETE_DB
    name: str


@model
class CreateBucketRequest(Request):
    OPCODE: ClassVar[int] = opcodes.CREATE_BUCKET
    database: str
    name: str
    type: str
    schema_json: str = wire("schemajson", default="")  # never set by the public API (spec 4.4)
    options: _DOC | None = None
    encrypted: bool = False
    replication_factor: int | None = None


@model
class DeleteBucketRequest(Request):
    OPCODE: ClassVar[int] = opcodes.DELETE_BUCKET
    database: str
    name: str


@model
class ListBucketsRequest(Request):
    OPCODE: ClassVar[int] = opcodes.LIST_BUCKETS
    database: str


@model
class ListDatabasesRequest(Request):
    OPCODE: ClassVar[int] = opcodes.LIST_DATABASES


@model
class BucketStatsRequest(_BucketRequest):
    OPCODE: ClassVar[int] = opcodes.BUCKET_STATS


@model
class BucketInfoRequest(_BucketRequest):
    OPCODE: ClassVar[int] = opcodes.BUCKET_INFO


@model
class CountRequest(_BucketRequest):
    OPCODE: ClassVar[int] = opcodes.COUNT
    filter: _DOC | None = None


@model
class DistinctRequest(_BucketRequest):
    OPCODE: ClassVar[int] = opcodes.DISTINCT
    field: str = ""
    filter: _DOC | None = None


@model
class SearchRequest(_BucketRequest):
    OPCODE: ClassVar[int] = opcodes.SEARCH
    filter: _DOC | None = None
    limit: int = 0


@model
class ScanRequest(_BucketRequest):
    OPCODE: ClassVar[int] = opcodes.SCAN
    filter: _DOC | None = None
    skip: int = 0
    limit: int = 0
    sort: _DOC | None = None
    projection: _DOC | None = None
    # From page 2 on: the previous page's bucket_id, so a bucket dropped and re-created between
    # two pages is not read as a continuation (wire.md section 3).
    expect_bucket_id: int = wire(default=0, conv="int64")


@model
class UpdateRequest(_KeyRequest):
    OPCODE: ClassVar[int] = opcodes.UPDATE
    set: _DOC | None = None
    unset: list[str] = field(default_factory=list[str])
    inc: _DOC | None = wire(default=None, conv="int64_values")
    push: _DOC | None = None
    add_to_set: _DOC | None = wire("addtoset", default=None)
    mul: _DOC | None = wire(default=None, conv="double_values")
    min: _DOC | None = None
    max: _DOC | None = None
    rename: _DOC | None = None
    filter: _DOC | None = None
    multi: bool = False
    write_concern: str = wire("writeconcern", default="")


@model
class PingRequest(Request):
    OPCODE: ClassVar[int] = opcodes.PING


@model
class HealthCheckRequest(Request):
    OPCODE: ClassVar[int] = opcodes.HEALTH_CHECK


@model
class ListNodesRequest(Request):
    OPCODE: ClassVar[int] = opcodes.LIST_NODES
    history: bool = False


@model
class DeclareStoreFieldsRequest(Request):
    OPCODE: ClassVar[int] = opcodes.DECLARE_STORE_FIELDS
    database: str
    name: str
    stores: list[_DOC] = field(default_factory=list[_DOC])


@model
class _FieldRequest(_KeyRequest):
    """The parent document is ``key``; the store field is ``field`` (operations.md section 5)."""

    field: str = ""
    element: str = ""
    path: list[Any] = wire(default_factory=list[Any])
    data: _DOC | None = None
    content: bytes = b""
    priority: int = wire(default=0, conv="int64")
    write_concern: str = wire("writeconcern", default="")


@model
class FieldPushRequest(_FieldRequest):
    OPCODE: ClassVar[int] = opcodes.FIELD_PUSH


@model
class FieldPopRequest(_FieldRequest):
    OPCODE: ClassVar[int] = opcodes.FIELD_POP
    limit: int = 0  # absent means 1; the server takes at most 100


@model
class FieldPeekRequest(_FieldRequest):
    OPCODE: ClassVar[int] = opcodes.FIELD_PEEK
    limit: int = 0


@model
class FieldPutRequest(_FieldRequest):
    OPCODE: ClassVar[int] = opcodes.FIELD_PUT


@model
class FieldGetRequest(_FieldRequest):
    OPCODE: ClassVar[int] = opcodes.FIELD_GET


@model
class FieldDeleteRequest(_FieldRequest):
    OPCODE: ClassVar[int] = opcodes.FIELD_DELETE


@model
class FieldLenRequest(_FieldRequest):
    OPCODE: ClassVar[int] = opcodes.FIELD_LEN


@model
class FieldListRequest(_FieldRequest):
    OPCODE: ClassVar[int] = opcodes.FIELD_LIST
    after: Any = None  # an element key, a position, or the opaque ``next`` of the last page
    limit: int = 0


@model
class GetFastRequest(_KeyRequest):
    """The one request whose payload is not BSON (wire.md section 5)."""

    OPCODE: ClassVar[int] = opcodes.GET_FAST


class RawRequest(Request):
    """A request the typed API does not offer: any opcode with a ready-made BSON document (or raw
    payload bytes). Only ``hoarddb._wire`` builds one; it is for the vectors and for operator
    opcodes, and carries no stability promise."""

    __slots__ = ("document", "opcode", "payload")
    RESPONSE_OPCODES: ClassVar[frozenset[int]] = frozenset()

    def __init__(
        self, opcode: int, document: Mapping[str, Any] | None = None, payload: bytes | None = None
    ) -> None:
        self.opcode = opcode
        self.document = document
        self.payload = payload


@dataclass(frozen=True, slots=True)
class RawResponse(Response):
    """Whatever frame answered a :class:`RawRequest`, undecoded."""

    opcode: int
    payload: bytes


# --- which response answers which request ---------------------------------------------------------

_ANSWERS: tuple[tuple[type[Request], type[Any], tuple[int, ...]], ...] = (
    (AuthRequest, AuthOkResponse, (opcodes.AUTH_OK,)),
    (PutRequest, WriteResponse, (opcodes.PUT,)),
    (UpsertRequest, WriteResponse, (opcodes.UPSERT_OK,)),
    (BTreeInsertRequest, WriteResponse, (opcodes.BTREE_INSERT,)),
    (GetRequest, GetResponse, (opcodes.GET_RESPONSE,)),
    (BTreeLookupRequest, GetResponse, (opcodes.BTREE_RESPONSE,)),
    (ConsistentGetRequest, ConsistentGetResponse, (opcodes.CONSISTENT_GET,)),
    (ExistsRequest, ExistsResponse, (opcodes.EXISTS_RESPONSE,)),
    (DeleteRequest, DeleteResponse, (opcodes.DELETE,)),
    (BatchPutRequest, BatchPutResponse, (opcodes.BATCH_PUT_OK,)),
    (FifoPushRequest, WriteResponse, (opcodes.FIFO_PUSH,)),
    (LifoPushRequest, WriteResponse, (opcodes.LIFO_PUSH,)),
    (HeapPushRequest, WriteResponse, (opcodes.HEAP_PUSH,)),
    (FifoPopRequest, PopResponse, (opcodes.FIFO_POP_RESPONSE,)),
    (LifoPopRequest, PopResponse, (opcodes.LIFO_POP_RESPONSE,)),
    (HeapPopRequest, PopResponse, (opcodes.HEAP_POP_RESPONSE,)),
    (FifoPeekRequest, PeekResponse, (opcodes.FIFO_PEEK,)),
    (LifoPeekRequest, PeekResponse, (opcodes.LIFO_PEEK,)),
    (BTreeRangeRequest, RangeResponse, (opcodes.BTREE_RESPONSE,)),
    (BTreeFirstRequest, RangeResponse, (opcodes.BTREE_FIRST,)),
    (BTreeLastRequest, RangeResponse, (opcodes.BTREE_LAST,)),
    (BlobWriteRequest, BlobWriteResponse, (opcodes.BLOB_WRITE,)),
    (BlobReadRequest, BlobReadResponse, (opcodes.BLOB_READ_RESPONSE,)),
    (CreateDbRequest, OkResponse, (opcodes.CREATE_DB,)),
    (DeleteDbRequest, OkResponse, (opcodes.DELETE_DB,)),
    (CreateBucketRequest, OkResponse, (opcodes.CREATE_BUCKET,)),
    (DeleteBucketRequest, OkResponse, (opcodes.DELETE_BUCKET,)),
    (DeclareStoreFieldsRequest, OkResponse, (opcodes.DECLARE_STORE_FIELDS,)),
    (ListBucketsRequest, ListBucketsResponse, (opcodes.LIST_BUCKETS,)),
    (ListDatabasesRequest, ListDatabasesResponse, (opcodes.LIST_DATABASES,)),
    (BucketStatsRequest, BucketStatsResponse, (opcodes.BUCKET_STATS,)),
    (BucketInfoRequest, BucketInfoResponse, (opcodes.BUCKET_INFO,)),
    (CountRequest, CountResponse, (opcodes.COUNT,)),
    (DistinctRequest, DistinctResponse, (opcodes.DISTINCT,)),
    (SearchRequest, SearchResponse, (opcodes.SEARCH,)),
    (ScanRequest, ScanResponse, (opcodes.SCAN,)),
    (UpdateRequest, UpdateResponse, (opcodes.UPDATE,)),
    (PingRequest, PongResponse, (opcodes.PONG,)),
    (HealthCheckRequest, HealthResponse, (opcodes.GET_RESPONSE,)),
    (ListNodesRequest, ListNodesResponse, (opcodes.GET_RESPONSE,)),
    (FieldPushRequest, FieldResponse, (opcodes.FIELD_PUSH,)),
    (FieldPopRequest, FieldResponse, (opcodes.FIELD_POP,)),
    (FieldPeekRequest, FieldResponse, (opcodes.FIELD_PEEK,)),
    (FieldPutRequest, FieldResponse, (opcodes.FIELD_PUT,)),
    (FieldGetRequest, FieldResponse, (opcodes.FIELD_GET,)),
    (FieldDeleteRequest, FieldResponse, (opcodes.FIELD_DELETE,)),
    (FieldLenRequest, FieldResponse, (opcodes.FIELD_LEN,)),
    (FieldListRequest, FieldListResponse, (opcodes.FIELD_LIST,)),
    (GetFastRequest, GetFastResponse, (opcodes.GET_FAST_RESPONSE,)),
)


def _register() -> dict[int, type[Request]]:
    table: dict[int, type[Request]] = {}
    for request, response, accepted in _ANSWERS:
        # frozen slots dataclasses allow class attributes to be set on the class object
        request.RESPONSE = response
        request.RESPONSE_OPCODES = frozenset(accepted)
        table[request.OPCODE] = request
    return table


REQUESTS: dict[int, type[Request]] = _register()


# --- encoding and decoding ---------------------------------------------------------------------


def request_document(request: Request) -> dict[str, Any]:
    """The request's wire document (not for ``GET_FAST``, whose payload is not BSON)."""
    return schema.to_wire(request)


def _get_fast_payload(request: GetFastRequest) -> bytes:
    parts: list[bytes] = []
    for what, text, limit, width in (
        ("database", request.database, 255, 1),
        ("bucket", request.bucket, 255, 1),
        ("key", request.key, 65535, 2),
    ):
        raw = text.encode("utf-8")
        if len(raw) > limit:
            raise errors.InvalidArgumentError(
                f"This GET_FAST {what} is {len(raw)} bytes; the format holds at most {limit}. "
                "Nothing was sent. Use a plain get instead.",
                operation="get_fast",
            )
        parts.append(len(raw).to_bytes(width, "little") + raw)
    return b"".join(parts)


def request_payload(request: Request) -> bytes:
    """The bytes of the frame's payload."""
    if isinstance(request, RawRequest):
        if request.payload is not None:
            return request.payload
        return serialize(request.document) if request.document is not None else b""
    if isinstance(request, GetFastRequest):
        return _get_fast_payload(request)
    if isinstance(request, PingRequest):
        return b""
    if isinstance(request, ListNodesRequest) and not request.history:
        return b""
    return serialize(request_document(request))


def request_frame(request: Request, request_id: bytes) -> bytes:
    """A whole frame; raises ``frame.FrameTooLarge`` before building a payload-sized buffer."""
    payload = request_payload(request)
    opcode = request.opcode if isinstance(request, RawRequest) else request.OPCODE
    return frame.encode_frame(opcode, request_id, payload)


def parse_response(request_cls: type[Request], reply: frame.Frame) -> Any:
    """Build the success response for ``request_cls`` from ``reply`` (an ``ERROR`` is not one)."""
    if request_cls is RawRequest:
        return RawResponse(reply.opcode, reply.payload)
    if reply.opcode not in request_cls.RESPONSE_OPCODES:
        got = opcodes.NAMES.get(reply.opcode, f"{reply.opcode:#04x}")
        raise errors.ProtocolError(
            f"The server answered {request_cls.__name__} with opcode {got}, which is not one "
            "of its answers. The connection was discarded.",
            operation="receive",
        )
    response = request_cls.RESPONSE
    from_payload = getattr(response, "from_payload", None)
    if from_payload is not None:
        return from_payload(reply.payload)
    document = deserialize(reply.payload) if reply.payload else {}
    return schema.from_wire(response, document)
