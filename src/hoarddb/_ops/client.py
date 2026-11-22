# SPDX-License-Identifier: Apache-2.0
"""Client-level operations: databases, ``ping``, ``health``, ``list_nodes`` (spec 4.3).

Plain functions returning generators (spec 3.3); the sync and the asyncio client run the same ones.
"""

from __future__ import annotations

from hoarddb import errors
from hoarddb._core import failure
from hoarddb._core.call import Op
from hoarddb._core.messages import (
    CreateDbRequest,
    DeleteDbRequest,
    HealthCheckRequest,
    HealthResponse,
    ListDatabasesRequest,
    ListDatabasesResponse,
    ListNodesRequest,
    ListNodesResponse,
    PingRequest,
)
from hoarddb._core.validate import check_name
from hoarddb._ops.support import call, context, payload
from hoarddb.types import DatabaseInfo, Health, NodeInfo


def ping(*, timeout: float | None) -> Op[None]:
    yield call("ping", None, PingRequest(), "READ", timeout)


def health(*, timeout: float | None) -> Op[Health]:
    reply = yield call("health", None, HealthCheckRequest(), "READ", timeout)
    r: HealthResponse = reply.response
    return Health(
        node_id=r.node_id,
        uptime=r.uptime,
        version=r.version,
        bucket_count=r.bucket_count,
        healthy=r.healthy,
        membership=r.membership,
        store_refusal=r.store_refusal,
        capacity=r.capacity,
        memtable=r.memtable,
        encryption=r.encryption,
        host=r.host,
        reasons=tuple(r.reasons),
    )


def list_nodes(*, timeout: float | None) -> Op[list[NodeInfo]]:
    reply = yield call("list_nodes", None, ListNodesRequest(), "READ", timeout)
    r: ListNodesResponse = reply.response
    return [NodeInfo(n.node_id, n.addr, n.fingerprint, n.joined_at, n.status) for n in r.nodes]


def create_database(name: str, *, if_not_exists: bool, timeout: float | None) -> Op[None]:
    check_name("database", name, operation="create_database")
    c = call("create_database", name, CreateDbRequest(name=name), "DDL", timeout)
    try:
        reply = yield c
    except errors.AlreadyExistsError:
        if if_not_exists:
            return
        raise
    failure.check_success(payload(reply), reply.request_id, context(c))


def drop_database(name: str, *, timeout: float | None) -> Op[None]:
    check_name("database", name, operation="drop_database")
    c = call("drop_database", name, DeleteDbRequest(name=name), "DDL", timeout)
    reply = yield c
    failure.check_success(payload(reply), reply.request_id, context(c))


def list_databases(*, timeout: float | None) -> Op[list[DatabaseInfo]]:
    c = call("list_databases", None, ListDatabasesRequest(), "READ", timeout)
    reply = yield c
    r: ListDatabasesResponse = reply.response
    return [DatabaseInfo(d.name, d.bucket_count) for d in r.databases]
