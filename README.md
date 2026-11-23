# hoarddb (Python driver)

The official Python driver for [hoardDB](https://hoarddb.com): blocking (`import hoarddb`) and
`asyncio` (`import hoarddb.aio`), one API, no runtime dependencies.

**Status: pre-release (0.0.0, unreleased), and it does not speak the ring protocol yet.** Nothing here
is published to PyPI and the API can still change. Python 3.11 or newer on Linux or macOS. Licence:
Apache-2.0 (see `LICENSE`). Read the next section before you build on it.

## What this driver reaches on a v1.1 server

A hoardDB v1.1 server is a ring server: a bucket declares fields, and a field can be a store (a queue, a
table, a heap) that every document carries for itself. The `hoarddb` command line, `hoarddb dump` and
`hoarddb restore` work on that data.

This driver was written before the ring. It sends the opcodes of the server's earlier single-store engine,
in which a bucket has one of six store types. A v1.1.0 server still answers those opcodes, and this is
what was found by running the driver against one:

- **Works:** connecting, pinning, `python -m hoarddb trust` (it showed the server's key), `create_database`, `create_bucket`, `put`,
  `upsert`, `get`, `update`, `delete`, `exists`, `insert`, `insert_many`, `find`, `count`, `info`,
  `list_buckets`, `push`, `pop` and `peek`, and the store-field verbs. Every file in `examples/` runs to
  the end, and the quick start below runs any number of times.
- **`pop` returns a list.** One document by default, up to 100 with `pop(limit=...)`, in pop order; an
  empty queue is `[]`. A store field's `pop` returns a list of `FieldEntry` in the same way.
- **On a hash or btree bucket `put` inserts; `upsert` replaces.** A `put` (or an `insert`) of a key that
  already holds a document is refused with `DocumentExistsError` and nothing is written. Replace a
  document with `upsert(key, doc)`, which also creates it when it is missing, or change some of its
  fields with `update`. The other store types answer a `put` of a held key differently: a heap replaces
  the entry, a fifo or lifo appends a second entry with the same `_id_`, and a blob bucket refuses
  `put` and `insert` altogether (`InvalidArgumentError`; use `put_blob`).
- **Its data is separate.** Its databases and buckets live in the earlier engine's own store. Nothing it
  creates is visible to the `hoarddb` command line or to a ring client, nothing created there is visible
  to it, and `hoarddb dump` does not include it.
- **Its data is on one node.** A v1.1.0 server refuses to start in the earlier engine's replicated mode,
  so on a cluster each node answers these opcodes from its own local store: a write through this driver
  stays on the node that took it and is not copied to the others.
- **There is no ring client for Python yet.** The Go module `github.com/hoarddb/hoarddb-go` has one, in
  its package `ring`.

## Five minutes

The blocks below are run by the test suite against a real server, straight from this text, and they
run against a v1.1.0 server any number of times.

**1. A virtual environment, then the install** (one terminal):

```bash
python -m venv .venv
source .venv/bin/activate
# The driver is not published to PyPI: install it from a clone of this repository.
git clone git@github.com:hoarddb/hoarddb-python.git
pip install ./hoarddb-python
```

**2. The server** (a second terminal). Build `hoarddb-server` from
[`github.com/hoarddb/hoarddb-server`](https://github.com/hoarddb/hoarddb-server) (no release binary is
published yet) and start it with a root password of your choosing:

```bash
HOARDDB_ROOT_PASSWORD='choose-a-long-password' hoarddb-server
```

Started from a terminal with no password set, it asks for one instead. The root user is `admin`. At
**every** start the server logs the key fingerprint it will present, on its
`TCP driver listener listening` line:

```
{"msg":"TCP driver listener listening","address":"127.0.0.1:7433","fingerprint":"SHA256:<64 hex digits>"}
```

**3. The client terminal.** Set the password you chose
(`export HOARDDB_ROOT_PASSWORD='choose-a-long-password'`), then the pin. `<SHA256:64-hex-digits copied from the log line>` is the
`fingerprint` value of the log line above, from `SHA256:` to the last hex digit; each server mints its
own key, so this one value is yours alone:

```bash
export HOARDDB_URI='hoarddb://localhost:7433/app?fingerprint=<SHA256:64-hex-digits copied from the log line>'
```

No pin yet? Run `python -m hoarddb trust localhost:7433` once, check the fingerprint it shows against
the server's log line, answer `yes`, and use `hoarddb://localhost:7433/app` with no `?fingerprint=` (see
"Trusting a server" below).

Because the URI is in the environment, the script is `hoarddb.connect()` and a fingerprint, a password
or a host never appears in code. Client (`quickstart.py`), blocking:

<!-- block: quickstart -->
```python
import hoarddb

# connect() with no argument reads HOARDDB_URI; the user and password come from
# HOARDDB_ROOT_USER and HOARDDB_ROOT_PASSWORD (the user defaults to "admin").
with hoarddb.connect() as client:
    client.create_database("app", if_not_exists=True)
    users = client.database("app").create_bucket("users", "hash", if_not_exists=True)

    users.upsert("ada", {"name": "Ada", "age": 36})     # put() would refuse a second run
    print(users.get("ada")["name"])                        # Ada
    for user in users.find({"age": {"$gte": 30}}):
        print(user["name"])                                 # Ada
```

Run it with `python quickstart.py`. The same program, `asyncio` (`quickstart_async.py`):

<!-- block: quickstart-async -->
```python
import asyncio
import hoarddb.aio as hoarddb

async def main() -> None:
    async with hoarddb.connect() as client:
        await client.create_database("app", if_not_exists=True)
        users = await client.database("app").create_bucket("users", "hash", if_not_exists=True)

        await users.upsert("ada", {"name": "Ada", "age": 36})
        print((await users.get("ada"))["name"])
        async for user in users.find({"age": {"$gte": 30}}):
            print(user["name"])

asyncio.run(main())
```

Typed, with a `TypedDict`, a queue and a bucket's declaration, in the same five minutes (it runs after
the steps above, in the same shell, so `HOARDDB_URI` is set):

<!-- block: typed -->
```python
from typing import TypedDict
import hoarddb

class User(TypedDict, total=False):
    name: str
    age: int

with hoarddb.connect() as client:
    db = client.database("app")
    db.create_bucket("users", "hash", if_not_exists=True)
    users = db.hash("users", doc_type=User)                # HashBucket[User]; no I/O
    jobs = db.create_bucket("jobs", "fifo", if_not_exists=True)
    jobs.push({"task": "welcome-email", "to": "ada"})
    for item in jobs.peek(limit=5):                         # none removed, pop order
        print(item["task"])
    done = jobs.pop()                                       # a list: [] when the queue is empty

    decl = users.info()                                     # BucketDeclaration; one round trip
    print(decl.name, decl.type, decl.entries)
    for store in decl.store_fields:
        print(" ", store.name, store.engine)
```

## Trusting a server

A hoardDB server has a self-signed key and no certificate authority, so the driver checks the key
itself, **before** it sends a password. If it does not know the key, `connect` raises `TlsTrustError`
and sends nothing. There are three ways to say "this is my server", in this order of preference:

1. **Pin it** (production, CI): put the fingerprint from the server's startup line in the URI, as in
   step 3 above (`?fingerprint=SHA256:...`), or pass `tls=hoarddb.TlsOptions(fingerprints=[...])`. A
   key that differs from the pin is refused naming both fingerprints, and never retried.
2. **Trust it once from a terminal:** `python -m hoarddb trust localhost:7433` shows the fingerprint,
   asks, and records the key in `~/.hoarddb/known_servers` (the file the `hoarddb` command-line tool
   shares); afterwards a bare `hoarddb://localhost` works. It sends no password and writes nothing unless
   you answer yes.
3. **Local development only:** `tls=hoarddb.TlsOptions(on_unknown_server=hoarddb.accept_first_sight)`
   trusts the first key a host shows and records it.

Options 2 and 3 are trust on first use: they are weaker than a pin, because anyone present at the first
connection could show you a different key. A key that later **changes** is refused in every case.

## The six store types

A bucket has a store type, and each type has its own verbs. Each example is a single file you can run
after step 3 (`python examples/hash.py`), any number of times.

| Type | Use it for | Verbs | Example |
|---|---|---|---|
| hash | documents by key | `put`, `upsert`, `get`, `update`, `delete`, `find`, `count` | `examples/hash.py` |
| btree | documents by key, in key order | the hash verbs plus `range`, `first`, `last` | `examples/btree.py` |
| fifo | a queue | `push`, `pop`, `pop(limit=...)`, `peek` | `examples/fifo.py` |
| lifo | a stack | `push`, `pop`, `pop(limit=...)`, `peek` | `examples/lifo.py` |
| heap | the highest priority first | `push(priority=...)`, `pop`, `pop(limit=...)`, `peek` | `examples/heap.py` |
| blob | bytes with metadata | `put_blob`, `get_blob` | `examples/blob.py` |

A document can also carry stores of its own (`user.Purchases` is that user's own FIFO):
`examples/store_fields.py`. A bucket can declare field types that the server enforces:
`examples/typed_bucket.py`. A miss is a value, not an error: `get` of a missing key is `None`.

On a hash or btree bucket `put` stores a new document and never replaces one: a key that already holds
a document raises `DocumentExistsError`, and nothing is written. `upsert` replaces the document, or
creates it. That rule is the keyed buckets' own. Through the generic `Bucket`, a `put` of a held key
replaces the entry on a heap and appends a second entry with the same `_id_` on a fifo or lifo, with no
error; on a blob bucket `put` and `insert` are refused with `InvalidArgumentError` (use `put_blob`).

`pop` removes what it returns and always answers a list, as `peek` does: one document unless you pass
`limit` (1 to 100), and `[]` when there is nothing to take.

## Errors

Every failure is a `hoarddb.HoardDBError` (or a subclass such as `NotFoundError`, `TlsTrustError`,
`ConnectionFailedError`). `err.message` is the server's sentence verbatim, `err.hint` is what the driver
adds (the fix, in this driver's own calls), `err.retryable` says whether repeating the same call is safe, and for a
write `err.write_outcome` is `"applied"`, `"not_applied"` or `"unknown"`.

## Cancellation (asyncio)

When a task is cancelled while a write is in flight, the driver closes the connection that was in use and
re-raises `asyncio.CancelledError`; it never swallows it. The server cannot be told to stop, so **a
cancelled write is not known to have happened**: it may be stored or not. A write that must complete
even if its caller is cancelled is protected with `asyncio.shield`, and the caller then learns the
outcome before the cancellation continues:

<!-- block: shield -->
```python
# A write that must not be abandoned by a cancelled caller: shield it, then await the result.
write = asyncio.ensure_future(users.put("ada", doc))
try:
    result = await asyncio.shield(write)
except asyncio.CancelledError:
    result = await write        # the write was not cancelled; learn its outcome first
    raise                       # then let the cancellation continue
```

A call that needs a definite answer within a time limit is wrapped in `asyncio.timeout` instead.

## Retries

`err.retryable` tells **you** whether repeating your own call, unchanged, is safe and can succeed; it is
not a statement about what the driver did. The driver retries reads, and a write only when the server
says nothing was applied and that waiting can help:

- A **read** (`get`, `exists`, `find`, `range`, `count`, `peek`, `ping`, ...) is retried once on a new
  connection when the connection is found closed or lost, and with backoff inside the call's deadline
  when the server says `UNAVAILABLE` or `OVERLOADED`.
- A **write** (`put`, `upsert`, `insert`, `insert_many`, `push`, `pop`, `delete`, `update`, `put_blob`) is
  resent by the driver in one case only: the server answers `UNAVAILABLE` with
  `write_outcome="not_applied"`, so nothing was written and the same request may succeed a moment
  later; it is then resent with backoff inside the call's deadline. It is never resent after a lost
  connection, a timeout or any answer whose outcome is not `not_applied`, because there is no
  idempotency key on the wire: a resend could undo someone else's newer write, or push twice.
- `write_outcome="not_applied"` means nothing was written. It does not mean `retryable`: a refusal such
  as `DocumentExistsError` or `InvalidArgumentError` is `not_applied` and **not** retryable, because the
  same call fails the same way. `retryable` is true when the request never left (nothing was sent) or
  the server was unavailable with nothing applied.
- If the outcome is `"unknown"`, read the state back before repeating (`upsert`, `delete` and `$set` can
  be repeated after that; a repeated `put` of a document that did land is refused with
  `DocumentExistsError`; `push`, `pop` and `$inc` must not be repeated). A `pop` whose answer is lost
  says how many entries it may have removed; an `insert_many` that failed part-way says how many
  documents were written and is always `"unknown"`: resend only `docs[written_count:]`.
- DDL (`create_*`, `drop_*`) is never retried either.
- A **failed login is never retried** in a loop. `RATE_LIMITED` is surfaced at once, with
  `retryable=True` meaning *you* may try again later.
- There is no `retry_writes` option.

## Compatibility

| Driver | Server | What was run |
|---|---|---|
| 0.0.0 (unreleased) | hoardDB v1.1.0, the earlier engine's store only | Run and passing: the 533 server tests that need one node (`pytest -m server`, leaving out the two three-node files), which include the 204 single-node vectors of the opcodes this driver sends, the README blocks, and every file in `examples/` run twice. Not run: the 33 tests of `tests/conformance/test_leg3_cluster.py` and `test_leg9_cluster.py`, which start three nodes in the earlier engine's replicated mode; a v1.1.0 server refuses to start in it. Not replayed: the 63 single-node vectors of opcodes this driver does not send |

## Where things are

| Path | What it is |
|---|---|
| `handbook/` | The handbook this driver was built from: the earlier engine's wire contract, authentication, trust, errors, operations and testing. The ring protocol is in the handbook of the `hoarddb-go` repository. |
| `testdata/` | The wire, URI and BSON vectors this driver replays. |
| `examples/` | The runnable examples of this README. |
| `scripts/fetch-server.sh` | Downloads the test server binary named in `testing/server.json`; no test-server release is published yet. See `testing/README.md`. |
| `CHANGELOG.md` | Every user-visible change before 1.0. |

## Contributing

- The driver never imports or copies code from the hoardDB server, and a payload change that is not
  reflected in the vectors is a bug in both.
- `handbook/` and `testdata/` are not edited here. If the handbook is silent or contradicts the
  vectors, open an issue with the exact wording and the vector or section it concerns.
- Run `pytest -q -m "not server"` for everything that needs no server. The rest needs a
  `hoarddb-server` binary you built: `export HOARDDB_SERVER_BIN=/path/to/hoarddb-server`, then `pytest -q`.
  Against v1.1.0, run the tests that need one node:
  `pytest -q -m server --deselect tests/conformance/test_leg3_cluster.py --deselect tests/conformance/test_leg9_cluster.py`
  (533 tests, all passing). The two files left out hold the 33 three-node tests: they start the earlier
  engine's replicated mode, which a v1.1.0 server refuses at start, so they cannot run against it. A
  test-server download (`scripts/fetch-server.sh`) is not available yet.
