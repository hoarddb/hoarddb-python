# Operations

What each request carries and what comes back, with the exact BSON field names. Language-neutral;
binds every driver. The vectors win where they disagree. Notation: `field?` is optional and absent when
empty; `field[]` is an array; `†` marks a response that also carries `copies_confirmed?`,
`replication_factor?` and `write_outcome?` (`errors.md` section 4).

## 1. Store types and their verbs

A bucket has one store type. The same verb means the same thing on every type that answers it, and a
driver does **not** look a bucket's type up before sending: a verb the type does not answer is refused
by the server, and the driver passes that refusal through.

| Driver verb | Opcode sent | fifo | lifo | hash | btree | heap | blob |
|---|---|---|---|---|---|---|---|
| put (keyed write, replaces) | `PUT` 10 | yes | yes | yes | yes | yes | yes |
| insert (keyless write, the server assigns the key) | `PUT` 10 with an empty `key` | yes | yes | yes | yes | yes | yes |
| insert many | `BATCH_PUT` 20 | yes | yes | yes | yes | yes | - |
| get | `GET` 11 | - | - | yes | yes | - | yes |
| exists | `EXISTS` 14 | - | - | yes | yes | - | yes |
| delete | `DELETE` 13 | - | - | yes | yes | - | yes |
| update | `UPDATE` 88 | - | - | yes | yes | - | - |
| push | `FIFO_PUSH` 30 | yes | yes | yes | yes | yes | - |
| pop | `FIFO_POP` 31 | yes | yes | - | - | yes | - |
| peek | `FIFO_PEEK` 33 | yes | yes | - | - | yes | - |
| range, first, last | `BTREE_RANGE` 52, `BTREE_FIRST` 89, `BTREE_LAST` 8A | - | - | - | yes | - | - |
| put blob, get blob | `BLOB_WRITE` 70, `BLOB_READ` 71 | - | - | - | - | - | yes |
| find, distinct | `SCAN` 8C, `DISTINCT` 8D | yes | yes | yes | yes | yes | - |
| count | `COUNT` 87 | yes | yes | yes | yes | yes | yes |
| search (secondary index) | `SEARCH` 8B | yes | yes | yes | yes | yes | - |
| stats | `BUCKET_STATS` 86 | yes | yes | yes | yes | yes | yes |
| consistent get (quorum read) | `CONSISTENT_GET` 9C | - | - | yes | yes | - | - |

"yes" means the server answers the verb on that type; "-" means it refuses (an `ERROR` frame or an
in-band `success: false` / `found: false` with a message, as the notes say). **A test in the server's
own suite sends every verb of this table to a bucket of every type, and fails if this table and the
server disagree**, so a "-" here is a refusal the server
really makes and a "yes" an answer it really gives. The vectors pin the wire shape of the ones a driver
sends.

Notes:

- The opcode families are aliases on the server (`wire.md` section 4): the server pushes into whatever
  bucket it is sent to, so a driver sends the `FIFO_*` family for push, pop and peek on fifo, lifo and
  heap buckets alike (there is no `HEAP_PEEK` opcode; `FIFO_PEEK` on a heap bucket peeks the heap).
  The vectors pin which opcode each verb sends.
- **`PUT` / insert on a queue or heap pushes.** On a fifo or lifo the document is pushed (the key, or the
  document's `_id_`, or a minted id, becomes its `_id_`); on a heap it is pushed at priority 0. A queue
  element cannot be read back by key. To choose a heap priority, push instead (`priority` on the push).
- **`PUT` / insert on a blob stores the document as the payload.** The document is marshalled to BSON
  and stored as the blob's bytes under the key (`key`, else the document's `_id_`, else a minted id), with
  no metadata. `BLOB_WRITE` is the verb for binary data: it carries the raw bytes and a `metadata`
  document, and answers `blobid` and `size`. A blob written by `PUT` reads back with `BLOB_READ` as the
  BSON bytes of the document.
- **`push` on a hash or btree is a keyed put**: the document is stored under its `_id_`, or a minted id
  when it has none, and the answer is the push response (`id`). `pop` and `peek` on a hash or btree are
  still refused (`pop not supported for bucket type hash`; peek: `found: false` with a message), because
  there is no order to take from.
- **`get` and `exists` on a blob answer.** `exists` is true for a stored key. `get` answers `found: true`
  with the stored document when the payload is BSON (as a `PUT` to a blob writes it), or else a
  synthesised document `{_id_, blob_id, size, ...metadata}` that carries no payload bytes. `BLOB_READ`
  is the verb for the payload; the CLI's `db.<blob>.get("<key>")` sends `BLOB_READ`, and
  `db.<blob>.info("<key>")` shows the metadata.
- **`search` on a bucket with no index answers a full scan.** On a fifo, lifo, btree or heap (which have
  no indexes) and on a hash bucket whose indexes do not serve the filter, `SEARCH` walks the bucket and
  answers the matches with `index_used: "none — full scan"`, the same answer `find` gives. A blob is
  refused. Indexes, and therefore `unique`, exist on **hash buckets only** in this release.
- **A verb with no key is refused, not answered `false`.** `exists` on a fifo, lifo or heap is an
  `ERROR` frame `INVALID_ARGUMENT` (the message says the type has no keys); so is `exists` with an empty
  `key`, on any type. An `exists: false` always means the server looked and the key is not there.
- **A refusal that is an `ERROR` frame is `INVALID_ARGUMENT`, never `INTERNAL`.** The verbs a type refuses
  by an `ERROR` frame are `range`, `first`, `last` (anything but btree), `blob read` and `blob write`
  (anything but blob), `update` (anything but hash or btree) and, on a blob, `distinct`, a filtered
  `count`, a multi `update` and `search`. The message names the bucket, its type and the type the verb
  needs. A driver branching on the code can tell a mistaken verb from a server fault.
- **`find` on a blob is `found: false`** with the message that names the blob verbs (`get`, `info`).
- **`insert many` on a fifo or lifo** answers `count` and the `ids` it pushed, in order; on a blob it is
  refused (`batch insert not supported for bucket type blob`, `success: false`, nothing written).

## 2. Writes

| Operation | Request | Success response |
|---|---|---|
| put / insert | `database, bucket, key, data, writeconcern` | `id, success, message, seq` † |
| insert many | `database, bucket, data[], writeconcern` | `ids[], count, success, message` † |
| push | `database, bucket, data, priority (heap: optional, 0 when omitted), writeconcern` | `id, success, message, seq` † |
| pop | `database, bucket` | `found, data?, message` † (a pop consumes, so it is a write) |
| delete | `database, bucket, key, expire_if?{field, date_before, seconds_before}` | `success, message, spared?, skipped?` † |
| update | `database, bucket, key, set, unset[], inc, push, addtoset, mul, min, max, rename, filter, multi` | `success, doc, message` † |
| put blob | `database, bucket, key, data (binary), metadata` | `success, key, blobid, size, message` † |

- `data` is the caller's document, sent as a nested BSON document. The server does not keep its field
  order (`wire.md` section 3). `_id_` is the server's key field inside a stored document.
- **Write concern** (`writeconcern`: `one`, `majority`, `all`, or empty for the server's default) may be
  sent on **every** data mutation: put, insert, insert many, push, pop, delete, update, put blob and the
  field-store writes. A cluster and a single node both refuse an unknown word (`INVALID_ARGUMENT`,
  `write_outcome: not_applied`, the vector `put on one node with a write concern the server does not
  know`). A driver still checks the word itself before sending (the server also accepts `1` for `one`). On a
  single node there are no replicas and a known level has no effect.
- **A refused write** (an `ERROR` frame for any of the data mutations above) carries `write_outcome`
  on a single node as on a cluster: `not_applied` for every refusal decided before anything was applied,
  such as a bucket that does not exist (the vectors `put to a bucket that does not exist` and `fifo push
  to a bucket that does not exist`). `errors.md` section 4 has the values and the one case where a
  single node leaves it absent.
- **Delete of a missing key** answers `success: false`, `document not found` (`blob not found` on a
  blob bucket), `write_outcome: not_applied`. A driver reports it as "nothing was deleted", not as an
  error; repeating a delete is safe. `expire_if` makes the delete conditional on a date field being older
  than a bound; `spared` (the condition kept the document) and `skipped` say why nothing was deleted.
- **Update** changes one document named by its key; there is no update by filter, and a missing key is
  `success: false`. All nine operators are documented and pinned by a vector (`update with push`,
  `update with mul`, `update with min and max`, `update with rename`, `update with every operator` and
  the three earlier ones). A key may be a path into a value (`"Address.City"`, `"Lines[0].Qty"`). A driver
  sends an operator only when its caller asks and passes the server's answer through. `doc` is the
  document after the change. The server applies them in the order of this table, and an update is all or
  nothing: when one operator is refused, nothing is written.

  | Operator | Wire field | Form | Rule |
  | --- | --- | --- | --- |
  | `$set` | `set` | document | stores each value at its key |
  | `$unset` | `unset` | **list** of names | removes each field; an array position is refused |
  | `$inc` | `inc` | document of int64 | adds; an absent field is created; refused on a non-number and past the 64-bit range |
  | `$push` | `push` | document | appends, even a value already there |
  | `$addToSet` | `addtoset` (one word) | document | appends unless an equal value is there |
  | `$mul` | `mul` | document of **doubles** | multiplies; an absent field stays absent; refused on a non-number |
  | `$min` | `min` | document | keeps the smaller; an absent field is set |
  | `$max` | `max` | document | keeps the larger; an absent field is set |
  | `$rename` | `rename` | document of strings (old name to new) | moves a field; a new name that already exists does nothing |
- **Insert many** is committed atomically up to the store's bound (`wire.md` section 10); over it the
  whole batch is refused in-band and nothing is written; halve and resend.
- **Typed fields.** A bucket may declare field types (section 4). A write whose document does not match
  is refused `FIELD_TYPE` and nothing is stored. Today the check runs on put, insert, push and insert
  many; **an update is not yet checked** against the declaration.

## 3. Reads

| Operation | Request | Success response |
|---|---|---|
| get | `database, bucket, key` | `found, data?, message` |
| exists | `database, bucket, key` | `exists` |
| consistent get | `database, bucket, key` | `found, data?, version, repaired, message?` |
| peek | `database, bucket, limit?` | `found, items?[document], message` |
| range / first / last | `database, bucket, low, high, limit` | `entries[{key, value?}], message` |
| find (`SCAN`) | `database, bucket, filter?, skip?, limit?, sort?{field: 1 or -1}, projection?{field: 1 or 0}` | `found, database, bucket, documents[], total, has_more, message?, bucket_id?, index_used?` |
| search | `database, bucket, filter, limit` | `results[], total, has_more, message, index_used` |
| count | `database, bucket, filter` | `count, message` |
| distinct | `database, bucket, field, filter` | `values[], message` |
| get blob | `database, bucket, key` | `found, blobid, data (binary), size, metadata, message` |

- A miss is `found: false` with a message, never an error frame (`errors.md` section 3).
- **`find` has no server-side cursor.** One `SCAN` returns one page plus `total` (the exact match count)
  and `has_more`; the next page is a new `SCAN` with a larger `skip`. A driver's cursor hides the paging
  and is honest about it: **a cursor is not a snapshot**. Between pages a concurrent insert ahead of the
  boundary can show a document twice and a concurrent delete can hide one. `sort` takes one field.
- On a page that answers `RESULT_TOO_LARGE`, a cursor halves its page size and retries, as often as
  needed: at most log2(`pageSize`)+1 attempts per page, down to 1 document. A page size that worked is
  kept for later pages, and a later page may halve again. If a single document still exceeds the result
  ceiling, the cursor reports the error with both numbers.
- **`search` says when it was cut.** `total` is the number of documents returned (at most `limit`), not
  the number that match; a search has no `skip` and does not count past its limit. `has_more` is `true`
  when at least one further matching document exists beyond `limit`, the same name and meaning as
  `find`'s, and `false` for a complete answer (including `limit` 0, which means no limit). A search
  cannot be paged: the answer to `has_more: true` is a larger `limit`, or `find` when the count matters.
- `find` of a bucket that does not exist is `found: false`; present it as "not found", like `get`.
- `range` bounds are strings; an empty side is open. `first` and `last` return at most one entry.
- `consistent get` reads every copy, repairs a stale one (`repaired`), and returns the newest
  (`version`); it is the read-your-writes verb on a cluster.

## 4. Databases and buckets

| Operation | Request | Success response |
|---|---|---|
| create database | `name` | `success, message` |
| drop database | `name` | `success, message` |
| list databases | (empty) | `databases[{name, bucketcount}], message` |
| create bucket | `database, name, type, schemajson, options, encrypted, replication_factor?` | `success, message` |
| drop bucket | `database, name` | `success, message` |
| list buckets | `database` | `found, database, buckets[{name, type, entries, stores[], replication_factor?, inherited?}], message, encrypted?` |
| bucket info | `database, bucket` (`BUCKET_INFO` D9; drivers: `Info` in Go, `info` in Node, returning a `BucketDeclaration`) | `found, database, bucket{name, type, entries, stores[], replication_factor?, inherited?, outside?}, store_fields?[{name, engine}]` |
| bucket stats | `database, bucket` | `found, database, bucket, type, entries, logical_bytes?, indexes[], message, stores[{name, engine, parent?, elements}], encrypted?, fields?[{name, type, required?, store?}], field_types?, writes_refused?` |
| health | (empty) | `nodeid, uptime, version, bucketcount, healthy, capacity?, memtable?, encryption?, membership?, membership_detail?, store_refusal?, host?` (`store_refusal`: why the store refuses writes, only while it does; `healthy` is then false) |
| list nodes | (empty) | `nodes[{nodeid, addr, fingerprint, joinedat (datetime), status}]` |

- `type` is one of `hash`, `btree`, `fifo`, `lifo`, `heap`, `blob`.
- **Bucket info** costs the same whatever the store fields hold (the drivers' `Info` / `info` is read-only,
  safe to retry, and never walks elements): `bucket.stores[]` names the declared
  store fields (the same names `list buckets` gives), and `store_fields[]` says what each one is, `engine`
  being one of `hash`, `btree`, `fifo`, `lifo`, `heap`, `blob` (the spelling `bucket stats` uses, without a
  declaration's `Store` suffix). Both are in declaration order and cover the top-level fields only; a
  nested store is not listed. `store_fields` is a sibling of `bucket`, not a member of it, and is absent
  for a bucket with no store fields. Use it to label each store field of a document; `bucket stats` also
  counts the elements and walks every one of them to do it.
- **Create database** of an existing name is `ALREADY_EXISTS`. **Create bucket** of an existing name is
  `success: true` ("already exists (type: hash)") when the type matches and `ALREADY_EXISTS` when it
  does not. A driver's "create if missing" option therefore accepts the first and still fails on the
  second. **Create bucket in a database that does not exist creates the database**; a driver documents
  this rather than hiding it.
- **Names** are at most 255 bytes; an over-long one is refused in-band.
- `replication_factor` on create is gated by the `bucket_rf` capability; `0` or absent means "the node's
  default". `encrypted` asks for at-rest encryption of the bucket.
- `options` carries the bucket's declarations:

  | Key | Shape | Meaning |
  |---|---|---|
  | `stores` | `[{name, engine, fields?[…]}]` | **store fields**: each document of this bucket carries its own store of `engine` (one of the six types) under `name`; `fields` nests stores inside a store's elements |
  | `indices` | `["Field", …]` or `[{fields[], unique?, name?, type?, expire_after_seconds?, desc?}]` | secondary indexes, hash buckets only; `unique` makes a duplicate a `DUPLICATE_KEY` refusal; `expire_after_seconds` expires documents by a date field |
  | `fields` | `[{name, type, required?, members?[…], elem?{…}}]` | **typed fields**: `type` is a word, case-insensitive: `any`, `string`, `int32`, `int`, `double`, `decimal`, `number`, `bool`, `datetime`, `bsontimestamp`, `binary`, `uuid`, `objectid`, `object`, `array`, `regex`, `null`, with the synonyms `integer`, `int64`, `float`, `boolean`, `date`, `timestamp`; `members` declares an object's fields and `elem` an array's element type |
  | `field_types` | `"strict"`, `"warn"` or `"off"` | how a declaration is enforced; `strict` when `fields` is given without it |

  A bad declaration is refused in-band with a message naming the problem. On a cluster, a typed create
  is refused in-band while any member runs a version without typed fields.
- **Drop** removes the bucket or database and its data, at once.
- **A missing name is an error here, not a miss.** `list buckets` of a database that does not exist,
  `bucket stats` of a bucket that does not exist, and `drop` of either answer `ERROR NOT_FOUND`; the
  `found` field of those answers is `true` whenever it is present. (`find` of a missing bucket is the
  one in-band `found: false`: `errors.md` section 3.)
- `list buckets` answers a whole database in **one frame**, with no paging; at hundreds of thousands of
  buckets it approaches the frame limit. A driver documents it as an operator call.
- `list nodes`' `addr` is each node's **cluster** address, not its driver port: a driver never dials it.
- `bucket info` answers the one bucket's row of `list buckets` without counting any store: it costs the
  same whether a store field holds ten elements or ten million, so use it (not `bucket stats`) to learn
  which store fields a bucket declares. A missing bucket or database is `NOT_FOUND`, as for `bucket stats`,
  and it needs the same read permission. Its `entries` is the bucket's own document count, not its stores'
  elements (only `bucket stats` counts those).
- `health`'s `host` describes the machine, per node: `{cpus, host_cpus?, memory_total_bytes?,
  memory_used_bytes?, memory_source?, load?{one_minute, five_minutes, fifteen_minutes}}`. `cpus` is what the
  process may use (a container's CPU quota rounds up and caps it; `host_cpus` appears only when it did).
  `memory_total_bytes` is the container's memory limit when one below the machine's is set
  (`memory_source` `cgroup`), else the machine's (`host`); `memory_used_bytes` is what is in use within
  that scope, not counting reclaimable file cache. `load` is the machine's load average. Every part but
  `cpus` is absent when the node cannot read it (a node off Linux sends `cpus` alone), so absence is not an
  error and an idle `0.00` load is still present. These are facts for display; a driver never decides
  anything from them. Like all of `health`, they are served to an authenticated connection only.
- `health`'s `membership` is `confirmed`, `not added yet`, `membership refused` or `waiting for
  founders`; a node that is not `confirmed` refuses requests that name a database or bucket.

## 5. Store fields

A store field belongs to **each document**: if bucket `User` declares `Purchases` as a fifo store, every
user document has its own `Purchases` queue. The parent document is named by its key; the field by its
name. Store fields are declared at create (`options.stores`) or added later with
`DECLARE_STORE_FIELDS`; they cannot be removed.

Every field verb takes `FieldRequest`:

| Field | Meaning |
|---|---|
| `database`, `bucket` | the parent bucket |
| `key` | the **parent document's** key |
| `field` | the store field's name |
| `element?` | the element key, for hash, btree and blob fields |
| `path?` | addresses a store nested inside an element: `[<outer field>, <outer element key or position or {priority, id}>]`; then `field` names the inner store |
| `data?` | the element document (for a blob field: its metadata) |
| `content?` | a blob field element's bytes |
| `priority?` | a heap field element's priority |
| `writeconcern?` | as for any write |

| Verb | Opcode | Field types | Answer |
|---|---|---|---|
| field push | D0 | fifo, lifo, heap | `FieldResponse` † |
| field pop | D1 | fifo, lifo, heap | `FieldResponse` † |
| field peek | D2 | fifo, lifo, heap | `FieldResponse` (`items`) |
| field put | D3 | hash, btree, blob | `FieldResponse` † |
| field get | D4 | hash, btree, blob | `FieldResponse` |
| field delete | D5 | hash, btree, blob | `FieldResponse` † |
| field len | D6 | all | `FieldResponse` (`len`) |
| field list | D7 | all | `FieldListResponse` |

- `FieldResponse`: `success, message?, found, element?, element_key?, position?, priority?, entry_id?,
  len, items?, blob_id?, content?` †. `element` is the element document a pop or get read; `element_key`,
  `position`, `priority` and `entry_id` say where it was (by engine). A peek answers `items` instead and
  leaves those five unset: `items[{element?, element_key?, position?, priority?, entry_id?}]` are the next
  elements in pop order, each with where it is (the same entry as a list's).
- **peek** takes a count: `limit` on the request (bucket `PEEK` and `FIELD_PEEK` alike; absent or 0 means 1,
  a negative `limit` or one above 1000 is `INVALID_ARGUMENT`) and answers a list, `items`, in pop order, for a
  one-entry peek too: the entries a run of pops would return, removed from nowhere. `found` is true when
  `items` is not empty. A peek is refused whole with `RESULT_TOO_LARGE` (0x0E) when its entries from the
  second on would pass the result ceiling, never answered short; the first entry is always answered, as a
  pop would return it. A peek of a hash, btree or blob bucket is `found:false` with a message that says why;
  of a hash, btree or blob field it is `INVALID_ARGUMENT`. `limit` belongs to peek alone: `FIELD_POP` and the
  other field verbs have no such member.
- `FIELD_LIST` takes the `FieldRequest` fields plus `after?` (where the page starts, exclusive: an element
  key, a position, or the opaque `next` of the previous page) and `limit?` (default 100, at most 1,000),
  and answers `FieldListResponse{success, message?, entries[{element?, blob_id?, element_key?, position?,
  priority?, entry_id?}], has_more, next?}`. `next` is opaque bytes: pass it back unchanged.
- An undeclared field, or a parent document that does not exist, is `NOT_FOUND`; a verb the field's type
  does not answer is `INVALID_ARGUMENT`.
- `DECLARE_STORE_FIELDS` takes `{database, name (the bucket), stores[]}` (the same shape as
  `options.stores`) and answers `{success, message?}`.

## 6. On a cluster

- **A driver talks to any node.** It does not route by the ring, and keeps no per-bucket routing state.
- **Writes** reaching a node that is not the bucket's owner are forwarded to the owner.
- **Reads** are answered by a node that holds a copy; one that holds none forwards to the owner. In a
  rare race (the node's view of who holds the bucket is stale) the read is refused `UNAVAILABLE`, naming
  the holder; retry.
- **`find` reads one node**, not the whole cluster, and **can miss a write that was just
  acknowledged** if that node's copy has not applied it yet. `consistent get` is the verb for reading
  back what you just wrote.
- During a membership change, an owner that is still copying a bucket answers `UNAVAILABLE` "retry in a
  moment" (`errors.md` section 5).
- On a single node there are no replicas: `copies_confirmed` and `replication_factor` are absent and
  write concern has no effect. A standalone node in this release **does not yet sync each write to disk
  before acknowledging it**; a driver's documentation does not promise that it does.
