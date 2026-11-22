# SPDX-License-Identifier: Apache-2.0
"""Blob verbs: ``put_blob`` and ``get_blob``, on the bulk lane (spec 4.6)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hoarddb import errors
from hoarddb._core import failure
from hoarddb._core.call import Op
from hoarddb._core.messages import BlobReadRequest, BlobReadResponse, BlobWriteRequest
from hoarddb._core.validate import check_name
from hoarddb._ops.refs import BucketRef
from hoarddb._ops.support import call, context, payload, require_mapping, write_concern
from hoarddb.types import Blob, BlobWriteResult


def put_blob(
    ref: BucketRef,
    key: str,
    data: bytes,
    *,
    metadata: Mapping[str, Any] | None,
    wc: str | None,
    timeout: float | None,
) -> Op[BlobWriteResult]:
    op = "put_blob"
    check_name("key", key, operation=op)
    if not isinstance(data, bytes | bytearray | memoryview):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise errors.InvalidArgumentError(
            f"A blob is bytes, not a {type(data).__name__}. Pass bytes(...), or store a document "
            "with put(). Nothing was sent.",
            operation=op,
        )
    meta = None if metadata is None else require_mapping(metadata, "metadata", operation=op)
    request = BlobWriteRequest(
        database=ref.database,
        bucket=ref.name,
        key=key,
        data=bytes(data),
        metadata=meta,
        write_concern=write_concern(wc, None, operation=op),
    )
    c = call(op, ref.target, request, "WRITE", timeout, lane="bulk")
    reply = yield c
    failure.check_success(payload(reply), reply.request_id, context(c))
    r = reply.response
    return BlobWriteResult(
        r.key or key, r.blob_id, r.size, r.copies_confirmed, r.replication_factor
    )


def get_blob(ref: BucketRef, key: str, *, timeout: float | None) -> Op[Blob | None]:
    op = "get_blob"
    check_name("key", key, operation=op)
    c = call(
        op,
        ref.target,
        BlobReadRequest(database=ref.database, bucket=ref.name, key=key),
        "READ",
        timeout,
        lane="bulk",
    )
    reply = yield c
    if not failure.check_found(payload(reply), reply.request_id, context(c)):
        return None
    r: BlobReadResponse = reply.response
    return Blob(key, r.blob_id, r.size, r.data, dict(r.metadata or {}))
