# SPDX-License-Identifier: Apache-2.0
"""Database-level operations: buckets and their declarations (spec 4.4, 4.5)."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

from hoarddb import errors
from hoarddb._core import diagnostics, failure
from hoarddb._core.call import Op
from hoarddb._core.messages import (
    BucketInfoRequest,
    BucketInfoResponse,
    BucketStatsRequest,
    BucketStatsResponse,
    CreateBucketRequest,
    DeclareStoreFieldsRequest,
    DeleteBucketRequest,
    ListBucketsRequest,
    ListBucketsResponse,
)
from hoarddb._core.validate import check_name, store_type_word
from hoarddb._ops.refs import BucketRef
from hoarddb._ops.support import call, context, payload
from hoarddb._poolstate import ServerFacts
from hoarddb.types import (
    BucketDeclaration,
    BucketInfo,
    BucketStats,
    DeclaredField,
    FieldSpec,
    FieldTypes,
    IndexSpec,
    StoreFieldInfo,
    StoreSpec,
    StoreStat,
)

_ALREADY: Final = re.compile(r"already exists \(type: (\w+)\)")
BUCKET_RF: Final = "bucket_rf"


# --- declarations to their wire form -------------------------------------------------------------


def store_doc(spec: StoreSpec, *, operation: str) -> dict[str, Any]:
    if not isinstance(spec, StoreSpec):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise errors.InvalidArgumentError(
            f"stores must be StoreSpec values, not {type(spec).__name__}; for example "
            'StoreSpec("Purchases", "fifo"). Nothing was sent.',
            operation=operation,
        )
    out: dict[str, Any] = {
        "name": check_name("store field", spec.name, operation=operation),
        "engine": store_type_word(spec.type, operation=operation),
    }
    if spec.stores:
        out["fields"] = [store_doc(s, operation=operation) for s in spec.stores]
    return out


def index_doc(spec: IndexSpec, *, operation: str) -> Any:
    fields = [check_name("index field", f, operation=operation) for f in spec.fields]
    if not fields:
        raise errors.InvalidArgumentError(
            "An IndexSpec needs at least one field, for example IndexSpec(['Email']). "
            "Nothing was sent.",
            operation=operation,
        )
    if (
        len(fields) == 1
        and not spec.unique
        and spec.name is None
        and spec.expire_after_seconds is None
    ):
        return fields[0]  # the plain form: an index by field name
    out: dict[str, Any] = {"fields": fields}
    if spec.unique:
        out["unique"] = True
    if spec.name is not None:
        out["name"] = spec.name
    if spec.expire_after_seconds is not None:
        out["expire_after_seconds"] = spec.expire_after_seconds
    return out


def field_doc(spec: FieldSpec, *, operation: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": check_name("field", spec.name, operation=operation),
        "type": spec.type,
    }
    if spec.required:
        out["required"] = True
    if spec.members:
        out["members"] = [field_doc(m, operation=operation) for m in spec.members]
    if spec.elem is not None:
        out["elem"] = field_doc(spec.elem, operation=operation)
    return out


def create_options(
    stores: Sequence[StoreSpec],
    indexes: Sequence[IndexSpec],
    field_types: FieldTypes | None,
    *,
    operation: str,
) -> dict[str, Any] | None:
    options: dict[str, Any] = {}
    if stores:
        options["stores"] = [store_doc(s, operation=operation) for s in stores]
    if indexes:
        options["indices"] = [index_doc(i, operation=operation) for i in indexes]
    if field_types is not None:
        options["fields"] = [field_doc(f, operation=operation) for f in field_types.fields]
        if field_types.mode is not None:
            if field_types.mode not in ("strict", "warn", "off"):
                raise errors.InvalidArgumentError(
                    f'field type mode "{field_types.mode}" is not one of: strict, warn, off. '
                    "Nothing was sent.",
                    operation=operation,
                )
            options["field_types"] = field_types.mode
    return options or None


# --- the operations ------------------------------------------------------------------------------


def create_bucket(
    database: str,
    name: str,
    type: object,  # noqa: A002 - the public keyword
    *,
    if_not_exists: bool,
    encrypted: bool,
    replication_factor: int | None,
    stores: Sequence[StoreSpec],
    indexes: Sequence[IndexSpec],
    field_types: FieldTypes | None,
    facts: ServerFacts | None,
    timeout: float | None,
) -> Op[str]:
    """Create a bucket and return its store type word."""
    op = "create_bucket"
    check_name("database", database, operation=op)
    check_name("bucket", name, operation=op)
    word = store_type_word(type, operation=op)
    options = create_options(stores, indexes, field_types, operation=op)
    if replication_factor is not None:
        if isinstance(replication_factor, bool) or replication_factor < 1:
            raise errors.InvalidArgumentError(
                f"replication_factor must be a whole number of at least 1, not "
                f"{replication_factor!r}. Nothing was sent.",
                operation=op,
            )
        if facts is not None and BUCKET_RF not in facts.capabilities:
            raise diagnostics.unsupported_feature(facts.version, BUCKET_RF, "replication_factor")
    request = CreateBucketRequest(
        database=database,
        name=name,
        type=word,
        options=options,
        encrypted=encrypted,
        replication_factor=replication_factor,
    )
    c = call(op, f"{database}/{name}", request, "DDL", timeout)
    reply = yield c
    failure.check_success(payload(reply), reply.request_id, context(c))
    message: str = reply.response.message
    existing = _ALREADY.search(message)
    if existing is None:
        return word
    # The server answers an idempotent success: the bucket was already there.
    if not if_not_exists or existing.group(1) != word:
        raise errors.AlreadyExistsError(
            message,
            operation=op,
            target=c.target,
            hint=failure.HINT_ALREADY_EXISTS if not if_not_exists else None,
        )
    declared = bool(options) or encrypted or replication_factor is not None
    if declared:
        yield from _check_declaration(database, name, stores, timeout)
    return word


def _check_declaration(
    database: str, name: str, stores: Sequence[StoreSpec], timeout: float | None
) -> Op[None]:
    """``if_not_exists=True`` applies no declaration to an existing bucket (measured: the server
    answers success and creates nothing). So that the caller is not left believing otherwise,
    one ``info()`` shows which requested store fields the bucket does not declare."""
    declaration: BucketDeclaration = yield from info(BucketRef(database, name), timeout=timeout)
    missing = [s.name for s in stores if s.name not in declaration.stores]
    if missing:
        raise errors.ConflictError(
            f'Bucket "{name}" already exists in database "{database}" without the store fields '
            f"{', '.join(missing)}, and create_bucket(if_not_exists=True) applies none of the "
            "requested declaration to an existing bucket.",
            operation="create_bucket",
            target=f"{database}/{name}",
            hint="Add them with db.declare_store_fields(bucket, stores). Indexes and typed "
            "fields are not compared here; check them with stats().",
        )


def drop_bucket(database: str, name: str, *, timeout: float | None) -> Op[None]:
    op = "drop_bucket"
    check_name("database", database, operation=op)
    check_name("bucket", name, operation=op)
    c = call(
        op, f"{database}/{name}", DeleteBucketRequest(database=database, name=name), "DDL", timeout
    )
    reply = yield c
    failure.check_success(payload(reply), reply.request_id, context(c))


def list_buckets(database: str, *, timeout: float | None) -> Op[list[BucketInfo]]:
    op = "list_buckets"
    check_name("database", database, operation=op)
    c = call(op, database, ListBucketsRequest(database=database), "READ", timeout)
    reply = yield c
    r: ListBucketsResponse = reply.response
    if not r.found and r.message:
        failure.check_found(payload(reply), reply.request_id, context(c))
    return [
        BucketInfo(b.name, b.type, b.entries, tuple(b.stores), b.replication_factor, b.inherited)
        for b in r.buckets
    ]


def declare_store_fields(
    database: str, bucket: str, stores: Sequence[StoreSpec], *, timeout: float | None
) -> Op[None]:
    op = "declare_store_fields"
    check_name("database", database, operation=op)
    check_name("bucket", bucket, operation=op)
    docs: list[Mapping[str, Any]] = [store_doc(s, operation=op) for s in stores]
    if not docs:
        raise errors.InvalidArgumentError(
            "declare_store_fields needs at least one StoreSpec. Nothing was sent.", operation=op
        )
    request = DeclareStoreFieldsRequest(database=database, name=bucket, stores=docs)
    c = call(op, f"{database}/{bucket}", request, "DDL", timeout)
    reply = yield c
    failure.check_success(payload(reply), reply.request_id, context(c))


def info(ref: BucketRef, *, timeout: float | None) -> Op[BucketDeclaration]:
    """The bucket's declaration in one round trip (BUCKET_INFO). Read-only and safe to retry."""
    c = call(
        "info",
        ref.target,
        BucketInfoRequest(database=ref.database, bucket=ref.name),
        "READ",
        timeout,
    )
    reply = yield c
    r: BucketInfoResponse = reply.response
    if r.bucket is None:
        raise _missing(ref, "info")
    row = r.bucket
    return BucketDeclaration(
        name=row.name or ref.name,
        type=row.type,
        entries=row.entries,
        stores=tuple(row.stores),
        store_fields=tuple(StoreFieldInfo(f.name, f.engine) for f in r.store_fields),
        replication_factor=row.replication_factor,
        inherited=row.inherited,
    )


def stats(ref: BucketRef, *, timeout: float | None) -> Op[BucketStats]:
    """Counts and walks every store element: not cheap with large store fields (see ``info``)."""
    c = call(
        "stats",
        ref.target,
        BucketStatsRequest(database=ref.database, bucket=ref.name),
        "READ",
        timeout,
    )
    reply = yield c
    r: BucketStatsResponse = reply.response
    if not r.found and r.message:
        failure.check_found(payload(reply), reply.request_id, context(c))
    return BucketStats(
        name=r.bucket or ref.name,
        type=r.type,
        entries=r.entries,
        logical_bytes=r.logical_bytes,
        indexes=tuple(r.indexes),
        stores=tuple(StoreStat(s.name, s.engine, s.parent, s.elements) for s in r.stores),
        encrypted=r.encrypted,
        fields=tuple(DeclaredField(f.name, f.type, f.required, f.store) for f in r.fields),
        field_types=r.field_types,
        writes_refused=r.writes_refused,
    )


def open_bucket(ref: BucketRef, *, timeout: float | None) -> Op[str]:
    """One ``BUCKET_STATS`` round trip to learn the real store type (``Database.open``)."""
    c = call(
        "open",
        ref.target,
        BucketStatsRequest(database=ref.database, bucket=ref.name),
        "READ",
        timeout,
    )
    reply = yield c
    r: BucketStatsResponse = reply.response
    if not r.type:
        raise _missing(ref, "open")
    return r.type


def _missing(ref: BucketRef, operation: str) -> errors.NotFoundError:
    return errors.NotFoundError(
        f'bucket "{ref.name}" not found in database "{ref.database}"',
        operation=operation,
        target=ref.target,
        hint=failure.HINT_NOT_FOUND,
    )
