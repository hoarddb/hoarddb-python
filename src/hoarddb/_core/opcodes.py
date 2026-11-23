# SPDX-License-Identifier: Apache-2.0
"""The opcode table (wire.md section 4): names from the handbook, numbers pinned by the vectors."""

from __future__ import annotations

from typing import Final

# Node-to-node and operator opcodes. A driver never sends them; the server refuses each one on the
# driver port by name, and the vectors name them, so the table knows them (wire.md section 4).
GOSSIP: Final = 0x90
REPL_PUSH: Final = 0x91
REPL_ACK: Final = 0x92
MIGRATE_START: Final = 0x93
MIGRATE_DATA: Final = 0x94
MIGRATE_DONE: Final = 0x95
CLUSTER_PROPOSE: Final = 0x96
CLUSTER_ADD: Final = 0x97
MIGRATE: Final = 0x98
CLUSTER_REMOVE: Final = 0x99
CLUSTER_PROBE: Final = 0x9A
CLUSTER_PROBE_REPLY: Final = 0x9B
VERSIONED_GET: Final = 0x9D
MEMBERSHIP_CHANGE: Final = 0x9E

# Opcodes of the server that this driver does not send: the grouped batch, the leased queue
# verbs, the dead list, ALTER_BUCKET, the user verbs and the ring family. The vectors name them,
# so the table does too; no request class exists for any of them.
BATCH: Final = 0x22
BATCH_OK: Final = 0x23
BATCH_UPSERT: Final = 0x24
BATCH_UPSERT_OK: Final = 0x25
QUEUE_CLAIM: Final = 0x34
QUEUE_ACK: Final = 0x35
QUEUE_RELEASE: Final = 0x36
QUEUE_EXTEND: Final = 0x37
DEAD_LIST: Final = 0x38
DEAD_REQUEUE: Final = 0x39
DEAD_DISCARD: Final = 0x3A
ALTER_BUCKET: Final = 0x8E
USER_CREATE: Final = 0xB5
USER_ALTER: Final = 0xB6
USER_DROP: Final = 0xB7
USER_LIST: Final = 0xB8
USER_GET: Final = 0xB9
USER_GRANT: Final = 0xBA
USER_REVOKE: Final = 0xBB
RING_LIST: Final = 0xDA
RING_COUNT: Final = 0xDB
RING_KEYED: Final = 0xDC
RING_ADMIN: Final = 0xDD
RING_TOPOLOGY: Final = 0xDE
RING_REPLY: Final = 0xDF
RING_BATCH: Final = 0xE0
RING_GET_MANY: Final = 0xE1

AUTH: Final = 0x01
AUTH_OK: Final = 0x02
AUTH_FAIL: Final = 0x03
PUT: Final = 0x10
GET: Final = 0x11
GET_RESPONSE: Final = 0x12
DELETE: Final = 0x13
EXISTS: Final = 0x14
EXISTS_RESPONSE: Final = 0x15
UPSERT: Final = 0x16
UPSERT_OK: Final = 0x17
BATCH_PUT: Final = 0x20
BATCH_PUT_OK: Final = 0x21
FIFO_PUSH: Final = 0x30
FIFO_POP: Final = 0x31
FIFO_POP_RESPONSE: Final = 0x32
FIFO_PEEK: Final = 0x33
LIFO_PUSH: Final = 0x40
LIFO_POP: Final = 0x41
LIFO_POP_RESPONSE: Final = 0x42
LIFO_PEEK: Final = 0x43
BTREE_INSERT: Final = 0x50
BTREE_LOOKUP: Final = 0x51
BTREE_RANGE: Final = 0x52
BTREE_RESPONSE: Final = 0x53
HEAP_PUSH: Final = 0x60
HEAP_POP: Final = 0x61
HEAP_POP_RESPONSE: Final = 0x62
BLOB_WRITE: Final = 0x70
BLOB_READ: Final = 0x71
BLOB_READ_RESPONSE: Final = 0x72
CREATE_DB: Final = 0x80
CREATE_BUCKET: Final = 0x81
DELETE_DB: Final = 0x82
DELETE_BUCKET: Final = 0x83
LIST_BUCKETS: Final = 0x84
LIST_DATABASES: Final = 0x85
BUCKET_STATS: Final = 0x86
COUNT: Final = 0x87
UPDATE: Final = 0x88
BTREE_FIRST: Final = 0x89
BTREE_LAST: Final = 0x8A
SEARCH: Final = 0x8B
SCAN: Final = 0x8C
DISTINCT: Final = 0x8D
CONSISTENT_GET: Final = 0x9C
PING: Final = 0xA0
PONG: Final = 0xA1
ERROR: Final = 0xA2
HEALTH_CHECK: Final = 0xA3
LIST_NODES: Final = 0xA4
GET_FAST: Final = 0xC1
GET_FAST_RESPONSE: Final = 0xC2
FIELD_PUSH: Final = 0xD0
FIELD_POP: Final = 0xD1
FIELD_PEEK: Final = 0xD2
FIELD_PUT: Final = 0xD3
FIELD_GET: Final = 0xD4
FIELD_DELETE: Final = 0xD5
FIELD_LEN: Final = 0xD6
FIELD_LIST: Final = 0xD7
DECLARE_STORE_FIELDS: Final = 0xD8
BUCKET_INFO: Final = 0xD9

CODES: Final[dict[str, int]] = {
    name: value
    for name, value in dict(globals()).items()
    if name.isupper() and isinstance(value, int) and not name.startswith("_")
}
NAMES: Final[dict[int, str]] = {value: name for name, value in CODES.items()}
