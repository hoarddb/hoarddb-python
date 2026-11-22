# SPDX-License-Identifier: Apache-2.0
"""Type tests for connecting and for the client: both surfaces, the option types, and the wrong
calls that must stay errors. An ``ignore`` that no longer hides an error fails both checkers (see
``tests/test_type_gate.py``), so a signature that is loosened turns a test red."""

from __future__ import annotations

from typing import Any, assert_type

import hoarddb
import hoarddb.aio
from hoarddb.types import DatabaseInfo


def _sync() -> None:
    with hoarddb.connect() as client:
        assert_type(client, hoarddb.Client)
        assert_type(client.list_databases(), list[DatabaseInfo])
        assert_type(client.database("app"), hoarddb.Database)
        assert_type(client.database("app").hash("users"), hoarddb.HashBucket[dict[str, Any]])
        client.database(1)  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]
        client.list_databases().append(1)  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]
        client.nope()  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
    hoarddb.connect("hoarddb://localhost", request_timeout=5.0, write_concern="majority")
    hoarddb.connect(write_concern="quorum")  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]
    hoarddb.connect(tls=hoarddb.TlsOptions(fingerprints=["SHA256:" + "0" * 64]))
    hoarddb.connect(tls={"insecure_reason": "local rig"})
    hoarddb.connect(pool={"size": 2})
    hoarddb.connect(retry_writes=True)  # type: ignore[call-arg]  # pyright: ignore[reportCallIssue]
    hoarddb.PoolOptions(size="4")  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]
    hoarddb.TlsOptions(insecure_reason=3)  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]


async def _async() -> None:
    async with hoarddb.aio.connect() as client:
        assert_type(client, hoarddb.aio.Client)
        assert_type(await client.list_databases(), list[DatabaseInfo])
        assert_type(client.database("app"), hoarddb.aio.Database)
        client.list_databases().append(1)  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
        client.database(1)  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]
    opened = await hoarddb.aio.connect()
    assert_type(opened, hoarddb.aio.Client)
    await opened.close()


async def _the_blocking_client_is_not_awaitable(client: hoarddb.Client) -> None:
    await client.list_databases()  # type: ignore[misc]  # pyright: ignore[reportGeneralTypeIssues]


def test_the_assertions_above_are_checked_by_the_type_checkers() -> None:
    assert callable(_sync) and callable(_async) and callable(_the_blocking_client_is_not_awaitable)
