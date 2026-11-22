# hoarddb (Python driver)

The official Python driver for [hoardDB](https://hoarddb.com): blocking (`import hoarddb`) and
`asyncio` (`import hoarddb.aio`), one API, no runtime dependencies.

**Status: pre-release.** Nothing here is published to PyPI and the API can still change. Python 3.11 or
newer on Linux or macOS. Licence: Apache-2.0 (see `LICENSE`).

## Five minutes

Every block below is run by the test suite against a real server, so it cannot drift from this text.

**1. A virtual environment, then the install** (one terminal):

```bash
python -m venv .venv
source .venv/bin/activate
# The driver is not published to PyPI: install it from a clone of this repository.
git clone git@github.com:hoarddb/hoarddb-python.git
pip install ./hoarddb-python
```

**2. The server, with no arguments** (a second terminal):

```bash
hoardDB-server
```

On its first start the server prints the root credentials **once**: two `export` lines, a user and a
generated password. At **every** start it logs the key fingerprint it will present, on its
`TCP driver listener listening` line:

```
{"msg":"TCP driver listener listening","address":"127.0.0.1:7433","fingerprint":"SHA256:<64 hex digits>"}
```

**3. The client terminal.** Paste the two `export` lines the server printed (`HOARDDB_ROOT_USER` and
`HOARDDB_ROOT_PASSWORD`), then the pin. `<SHA256:64-hex-digits copied from the log line>` is the
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

    users.put("ada", {"name": "Ada", "age": 36})
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

        await users.put("ada", {"name": "Ada", "age": 36})
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
    job = jobs.pop()                                        # dict, or None when the queue is empty

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
   asks, and records the key in `~/.hoarddb/known_servers` (the file the `hoardDB` command-line tool
   shares); afterwards a bare `hoarddb://localhost` works. It sends no password and writes nothing unless
   you answer yes.
3. **Local development only:** `tls=hoarddb.TlsOptions(on_unknown_server=hoarddb.accept_first_sight)`
   trusts the first key a host shows and records it.

Options 2 and 3 are trust on first use: they are weaker than a pin, because anyone present at the first
connection could show you a different key. A key that later **changes** is refused in every case.

## The six store types

A bucket has a store type, and each type has its own verbs. Each example is a single file you can run
after step 3 (`python examples/hash.py`), and each one is run, line for line, by the tests.

| Type | Use it for | Verbs | Example |
|---|---|---|---|
| hash | documents by key | `put`, `get`, `update`, `delete`, `find`, `count` | `examples/hash.py` |
| btree | documents by key, in key order | the hash verbs plus `range`, `first`, `last` | `examples/btree.py` |
| fifo | a queue | `push`, `pop`, `peek` | `examples/fifo.py` |
| lifo | a stack | `push`, `pop`, `peek` | `examples/lifo.py` |
| heap | the highest priority first | `push(priority=...)`, `pop`, `peek` | `examples/heap.py` |
| blob | bytes with metadata | `put_blob`, `get_blob` | `examples/blob.py` |

A document can also carry stores of its own (`user.Purchases` is that user's own FIFO):
`examples/store_fields.py`. A bucket can declare field types that the server enforces:
`examples/typed_bucket.py`. A miss is a value, not an error: `get` of a missing key is `None`.

## Errors

Every failure is a `hoarddb.HoardDBError` (or a subclass such as `NotFoundError`, `TlsTrustError`,
`ConnectionFailedError`). `err.message` is the server's sentence verbatim, `err.hint` is what the driver
adds (the fix, in one sentence), `err.retryable` says whether repeating the same call is safe, and for a
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

`err.retryable` tells **you** whether repeating your own call, unchanged, is safe; it is not a statement
about what the driver did. The driver retries only reads, and only where it cannot do harm:

- A **read** (`get`, `exists`, `find`, `range`, `count`, `peek`, `ping`, ...) is retried once on a new
  connection when the connection is found closed or lost, and with backoff inside the call's deadline
  when the server says `UNAVAILABLE` or `OVERLOADED`.
- A **write** (`put`, `insert`, `insert_many`, `push`, `pop`, `delete`, `update`, `put_blob`) is
  **never retried by the driver**, because there is no idempotency key on the wire: a retry after an
  unknown outcome could undo someone else's newer write, or push twice. If the request was refused
  before it could be applied, the error says `write_outcome="not_applied"` and `retryable` is true; if
  the outcome is unknown, read the state back before repeating (`put`, `delete` and `$set` can be
  repeated after that; `push`, `pop` and `$inc` must not be).
- DDL (`create_*`, `drop_*`) is never retried either.
- A **failed login is never retried** in a loop. `RATE_LIMITED` is surfaced at once, with
  `retryable=True` meaning *you* may try again later.
- There is no `retry_writes` option.

## Compatibility

| Driver | Server runs in conformance |
|---|---|
| 0.0.0 (unreleased) | the pinned test server `test-server-5abce282d5eb` (see `testing/server.json`) |

## Where things are

| Path | What it is |
|---|---|
| `handbook/` | The driver handbook: the wire contract, authentication, trust, errors, operations and testing. |
| `testdata/` | The shared wire, URI and BSON vectors every driver must replay. |
| `examples/` | The runnable examples of this README. |
| `scripts/fetch-server.sh` | Downloads the test server binary named in `testing/server.json`; no test-server release is published yet. See `testing/README.md`. |
| `CHANGELOG.md` | Every user-visible change before 1.0. |

## Contributing

- The driver never imports or copies code from the hoardDB server, and a payload change that is not
  reflected in the vectors is a bug in both.
- `handbook/` and `testdata/` are the same in every driver repository and are not edited here. If the
  handbook is silent or contradicts the vectors, open an issue with the exact wording and the vector or
  section it concerns.
- Run `pytest -q -m "not server"` for everything that needs no server. The rest needs a
  `hoardDB-server` binary you built: `export HOARDDB_SERVER_BIN=/path/to/hoardDB-server`, then `pytest -q`.
  A test-server download (`scripts/fetch-server.sh`) is not available yet.
