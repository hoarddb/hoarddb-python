# Errors, the write outcome, and what to retry

How a hoardDB server says no, and what a driver does about it. Language-neutral; binds every driver.
The single most important rule: **pass the server's message through verbatim.** The server writes its
refusals as sentences that name the fix ("… Run 'create database app' …", "… ask an administrator to
run: grant role …"). A driver that replaces one with "server error" has destroyed the fix.

## 1. The error frame

An error is an `ERROR` frame (`0xA2`) in place of the success opcode, echoing the request ID, with this
payload:

| Field | Type | Present |
|---|---|---|
| `code` | integer | always (decode any width) |
| `message` | string | always |
| `write_outcome` | string: `applied`, `not_applied`, `unknown` | on refusals of a write, and some others (section 4) |
| `copies_confirmed` | integer | with `write_outcome`, when the server knows it |
| `replication_factor` | integer | with `copies_confirmed` |
| `constraint_refusal` | bool | optional, with `INVALID_ARGUMENT`: the document itself is refused (a path the document cannot hold), the same on every copy, rather than the request being malformed. A driver may ignore it |
| `max_result_bytes` | integer | only with `RESULT_TOO_LARGE` |
| `retained_bytes` | integer | only with `RESULT_TOO_LARGE`; a lower bound |

Tolerance rules, because an older server and a future bug look the same to a driver:

- A `code` of `0`, or no `code`, on an `ERROR` frame is `INTERNAL`. **Never success.**
- An unknown code `n` is kept as `UNKNOWN(n)` with the message, and treated like `INTERNAL`.
- An `ERROR` frame may be followed at once by the server closing the connection. A connection is reused
  after an `ERROR` frame **only** for the answers that are documented to leave it open (section 6);
  otherwise the driver discards it, or proves it with a `PING`.

## 2. The codes

| Code | Name | The server sends it for |
|---|---|---|
| `0x01` | `NOT_FOUND` | a missing bucket; a missing database (also for `list buckets` and `drop database`); a missing bucket for `bucket stats`, `bucket info` and `drop bucket`; an undeclared store field, or a field operation on a parent document that does not exist; a `GET_FAST` of a missing name |
| `0x02` | `ALREADY_EXISTS` | `create database` of a name that exists (`database "app" already exists`); `create bucket` of a name that exists **with another store type** (the message names both types and says a type cannot be changed in place) |
| `0x03` | `PERMISSION_DENIED` | the user's grants do not cover the request (`auth.md` section 5); a backup or restore by a non-root user |
| `0x04` | `INVALID_ARGUMENT` | a document or blob over its limit; a write too large to replicate; a request frame over 32 MiB; a bad update operand; an unknown write-concern word (on a cluster; a standalone node does not read the word); a refused path; a bad store-field or typed-field declaration sent to `DECLARE_STORE_FIELDS`; a field verb the field's store type does not answer, or an incomplete field address; a verb the bucket's store type does not answer and that is refused as an error rather than in-band (`BTREE_RANGE`, `BTREE_FIRST`, `BTREE_LAST` off a btree, `BLOB_READ` and `BLOB_WRITE` off a blob, single and multi `UPDATE` off a hash or btree, `DISTINCT`, a filtered `COUNT` and `SEARCH` on a blob; the message names the bucket, its type and the type the verb needs); `EXISTS` with an empty key or on a bucket type with no keys (fifo, lifo, heap), which is never answered `exists: false`; a node-to-node opcode on the driver port |
| `0x05` | `INTERNAL` | anything else, including an unsupported opcode and a request payload that does not decode |
| `0x06` | `UNAVAILABLE` | the cluster cannot answer **here, now**, and nothing about the request is wrong (section 5) |
| `0x07` | `TIMEOUT` | the write concern was not met in time. **The write is stored on the owner**: `write_outcome: applied`, with how many copies hold it |
| `0x08` | `CONFLICT` | a cluster member mismatch; a backup already running; a backup digest mismatch |
| `0x09` | `QUOTA_EXCEEDED` | not sent today |
| `0x0A` | `UNSUPPORTED_VERSION` | ALPN not served (connection-level); or a write to a bucket whose recorded typed-field declaration this server cannot enforce (written by a newer server, or failing this one's checks): **reads of that bucket still work**, the connection stays open, and no retry helps until an operator acts |
| `0x0B` | `EARLY_DATA_REJECTED` | never sent (retired) |
| `0x0C` | `NOT_AUTHENTICATED` | a request before AUTH succeeded |
| `0x0D` | `OVERLOADED` | connection-level: the server's or this address's connection budget is full |
| `0x0E` | `RESULT_TOO_LARGE` | a query result over the node's ceiling, or any response over 32 MiB; carries `max_result_bytes` and `retained_bytes` |
| `0x0F` | `DUPLICATE_KEY` | a write that would give a unique index a second document with the same key; nothing was stored |
| `0x10` | `FIELD_TYPE` | a document has a value of the wrong declared type, misses a required field, or has a top-level field name containing `.`, `[` or `]`; nothing was stored. The message names the field, the declared type, the type received and the fix |
| `0x11` | `BATCH_TOO_LARGE` | a batch put over the largest batch the store commits atomically; `write_outcome: not_applied`, nothing was stored. Split on the code, never on the message: halve the batch and resend. The connection stays open |
| `0x12` | `DOCUMENT_EXISTS` | a put, an insert or a batch put on a hash or btree bucket that names a key which already holds a document; `write_outcome: not_applied`, nothing was stored (a batch is refused whole). Replace with `UPSERT`, or change fields with `UPDATE`. The connection stays open |

## 3. Three channels, one error type

A refusal reaches a driver in one of three ways, and a driver folds all three into its one error type
**without losing which it was**:

1. **An `ERROR` frame** (section 1).
2. **In-band `success: false` + `message`** on a successful frame. Used by:
   - `create database` and `create bucket` for a bad or over-long name, an unsupported store type, a bad
     typed-field or store-field declaration, and a typed create while a cluster member's version lacks
     typed fields;
   - `delete` of a key that does not exist: `success: false`, message `document not found` (`blob not
     found` on a blob bucket), `write_outcome: not_applied`;
   - `update` of a key that does not exist;
   - (an earlier server only) a batch over the atomic bound ("none of it was written"). The current
     server refuses it with an `ERROR` frame, `BATCH_TOO_LARGE`.

   And one in-band **success** a driver must not mistake for a refusal: `create bucket` of a name that
   exists **with the same store type** answers `success: true`, message `bucket "b" already exists (type:
   hash)`. It is idempotent by design.
3. **In-band `found: false` + `message`** on reads, pops and peeks. **A miss is a successful answer,
   never an empty frame.** The absence messages are `document not found`, `queue is empty`, `heap is
   empty`, and the blob form `… not found in …`. A `found: false` with any other message is a refusal
   (for example, a verb the bucket's store type does not answer, such as `SCAN` of a blob bucket), not a
   miss. `SCAN` of a bucket that
   does not exist is also in-band `found: false`, while `GET` of one is an `ERROR` `NOT_FOUND`: a driver
   presents both as the same "not found".

Which in-band messages mean which class is pinned by the vectors; a driver classifies an in-band refusal
by the operation and the vector-pinned message, and falls back to `INVALID_ARGUMENT` with the message
verbatim.

## 4. The write outcome

Every refusal of a write says whether the write happened, on a single node as on a cluster. The writes
are the data verbs: put, insert, insert many, push, pop, delete, update, put blob and the field-store
writes.

| `write_outcome` | Means | A caller may |
|---|---|---|
| `not_applied` | nothing was written: the request was refused before anything was applied (a bucket or database that does not exist, a permission, the document or blob limit, a request that does not decode, a duplicate key, a type or path refusal, an update its operators refuse, a field verb on a missing document) | retry it safely, though the same request fails the same way unless something changed (section 6; after a backoff, if the code says the cluster is busy) |
| `applied` | the write is stored, though the request as a whole was refused (a write-concern `TIMEOUT`) | **not** resend a push or an increment: it would apply twice |
| `unknown` | the server cannot tell (ownership moved under the write; a forwarded write's answer was lost; the owner's own store failed the apply) | read it back before resending anything that is not idempotent |
| absent | not reported: a read, a DDL request, an older server, or a single node whose own store failed the write (its message says the store failed) | treat as unknown for a write |

A refused write with no `write_outcome` therefore comes from a server older than this rule, or from a
store failure on a single node: a commit the store reported as failed may still have reached its
memory. Either way the outcome is unknown, and a driver says so.

On a **cluster**, every data-mutation **success** also carries `copies_confirmed` and
`replication_factor`; on a single node, and on DDL, they are absent. `write_outcome: unknown` appears on
a success response only in the rare `success: false` case where the owner's own store failed.

**Write concern** (`one`, `majority`, `all`; empty means the server's configured default) is counted
over the copies that are **in sync**: an `all` write with a replica down is **answered success** with
`copies_confirmed` below `replication_factor`. Durability is a number a caller reads, not a refusal. A
write concern that cannot be met in time is `TIMEOUT` with `write_outcome: applied`, and its message
says how many copies hold the write and not to resend a push or an increment.

## 5. `UNAVAILABLE`, in detail

Each of these is answered with code `0x06`; the message says what is happening and what to do. The
`write_outcome` column is what a write carries:

| Situation | `write_outcome` | Message begins |
|---|---|---|
| a read reached a node that does not hold the bucket | (read) | `bucket "b" in database "d" is held by node <id> (cluster address <addr>) and its replicas; this node does not hold it …` |
| the cluster's membership is changing (epoch mismatch, or a node that is not the owner on its view) | `not_applied` | `the cluster's membership is changing and the write was not applied: …` |
| this node was removed from the cluster | `not_applied` | `this node was removed from the cluster at epoch N and no longer accepts writes; connect to <member> …` |
| the bucket's owner changed while the write was in flight | `unknown` | `the bucket's owner changed while the write was in flight: …` |
| a forwarded write's answer was lost | `unknown` | `the owner's answer to a forwarded request was lost: …` |
| the owner is still copying the bucket after a membership change (a read, a write, or a create of it) | read: none; write and create: `not_applied` | `… is copying it from the bucket's other holders now …`, ending "Retry in a moment" |
| the node has not been added to its cluster yet (it serves nothing that names a database or bucket) | `not_applied` | `This node has not been added to the cluster yet: …` |
| a cluster member's version lacks a feature the request needs | `not_applied` | names the member and the upgrade |

There is **no machine-readable retry hint**: no field names a delay, a node or a progress figure. A
driver backs off on its own schedule (100 ms doubling to 5 s, with full jitter, is the recommendation),
bounded by the caller's deadline, and surfaces the server's message when it gives up.

**Coming, and already handled by these rules:** while a node is joining a cluster, every schema change
on the cluster (create or drop a database or bucket, restore) will be refused `UNAVAILABLE`,
`not_applied`, naming the joining node and its progress. Today only the joining node itself refuses.
Reads and writes to existing buckets carry on.

## 6. The retry table

| Answer | Retry? |
|---|---|
| `UNAVAILABLE` on a read | yes, with backoff, bounded by the caller's deadline; another node may answer (the message names the holder) |
| `UNAVAILABLE` with `write_outcome: not_applied` | yes, with backoff, bounded by the caller's deadline |
| any answer with `write_outcome: unknown` | **never automatically**: the caller decides, after reading back |
| `TIMEOUT` (`write_outcome: applied`) | **never**: the write is stored |
| `OVERLOADED` | yes, that one dial, with backoff; the pool is fine. This is the all-zero-request-ID refusal of `wire.md` section 8, sent before any request is read. An `OVERLOADED` that carries a request's own ID is not that refusal: a read is resent, a write is **not** (its outcome is unknown unless the answer says otherwise) |
| `NOT_AUTHENTICATED` | re-authenticate that connection once; then resend the request once if it is a read |
| `RESULT_TOO_LARGE` | not as sent: page smaller (a cursor halves its page size as often as needed, down to 1 document, then reports), project, or narrow the filter |
| `NOT_FOUND`, `ALREADY_EXISTS`, `PERMISSION_DENIED`, `INVALID_ARGUMENT`, `DUPLICATE_KEY`, `FIELD_TYPE`, `DOCUMENT_EXISTS`, `CONFLICT`, `UNSUPPORTED_VERSION` | **never**: the same request fails the same way |
| `BATCH_TOO_LARGE` with `write_outcome: not_applied` | not as sent: halve the batch and send the halves in order |
| `INTERNAL`, `UNKNOWN(n)` | never automatically |
| the connection failed before a complete response arrived | a read: once, on a fresh connection. A write, and a `pop` (it consumes): **never**; the outcome is unknown and the error says so |
| an `AUTH_FAIL` | see `auth.md` section 1; never in a loop |

A retry is invisible to the caller except in the driver's aggregate statistics. A driver never retries
a write it cannot prove `not_applied`.

**Connections that survive an `ERROR` frame**: `RESULT_TOO_LARGE`; an oversize request on an
authenticated connection (`wire.md` section 2); `FIELD_TYPE`; `UNSUPPORTED_VERSION` for a bucket's
declaration; every `GET_FAST` refusal; and every handler refusal that is not followed by a close. A
driver that is unsure discards the connection; that is always safe, and costs one dial.

## 7. Errors the driver makes itself

Some failures never reach a server. Each is a sentence with three parts: what happened, what it means,
what to do. Codes for these are the driver's own (outside the wire range), and each is matchable:

| Situation | The sentence says |
|---|---|
| Server key unknown, nothing pinned | the host, the fingerprint it presented, and the three ways to trust it (pin it, trust it once with the CLI, or accept on first sight for local development) |
| Pinned or recorded key changed | both fingerprints; if the keys were rotated, which line of `known_servers` to delete; if not, that the connection may be intercepted; that the driver will not retry |
| Credentials refused | the user; that the driver does not retry; where the root password comes from (`HOARDDB_ROOT_PASSWORD`, or the one the server asked for on its first start) |
| `RATE_LIMITED` | that it is not a credential failure; to wait; that the driver stopped retrying |
| Unreachable | the address, the network error, and (for `localhost`) that the server listens on loopback by default and the container image on every interface |
| An option the server did not advertise | the option, the capability it needs, the server's version; that nothing was sent |
| A request over 32 MiB | the arithmetic, and the remedy (smaller batches, or a blob) |
| The caller's deadline passed | the operation and target; for a write, that the outcome is unknown and not to resend a push or an increment |
| A bad URI | the part that is wrong and the expected shape (`trust.md` section 6) |

No message or log line contains a password or a token.
