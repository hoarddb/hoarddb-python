# Changelog

User-visible changes before 1.0, newest first. Before 1.0 a change is made directly, with no alias
and no deprecation.

The driver sends the opcodes of the hoardDB server's earlier single-store engine, not the ring protocol
of a v1.1 server (see the README, "What this driver reaches on a v1.1 server").

## Unreleased

- Fixed: `pop()` returned `None` (from a store field, an empty `FieldEntry`) while the server removed the
  entry. The server answers a pop with a list, and the driver now reads it.
- **BREAKING: `pop()` returns a list**, on buckets and on store fields, as `peek()` does: one entry by
  default, up to 100 with the new `pop(limit=...)`, and `[]` when there is nothing to take. It returned
  one document or `None`. Code written for the old return value still runs and means something else,
  so change every call:

  ```python
  # A bucket, before:
  job = jobs.pop()
  if job is not None:
      handle(job)

  # A bucket, after:
  popped = jobs.pop()
  if popped:
      handle(popped[0])

  # A store field, before:
  entry = purchases.pop()
  if entry is not None:
      handle(entry.element)

  # A store field, after:
  popped = purchases.pop()
  if popped:
      handle(popped[0].element)
  ```

  The loop that silently changes meaning is the usual consumer:

  ```python
  # Before. With this release it NEVER ENDS: [] is not None, and handle() is given a list.
  while (job := jobs.pop()) is not None:
      handle(job)

  # After: [] ends the loop.
  while popped := jobs.pop():
      handle(popped[0])

  # Or take up to 100 in one request.
  while batch := jobs.pop(limit=100):
      for job in batch:
          handle(job)
  ```

  A pop answer that says an entry was removed and carries none the driver can read now raises
  `ProtocolError`; it is never reported as an empty queue.
- Changed: a `pop` whose answer is lost says in its hint how many entries it may have removed. A pop
  answer that says "not found" and still carries entries raises `ProtocolError`.
- Changed: the connection is kept after `DocumentExistsError` and `BatchTooLargeError`, as the server
  keeps it; a refused `put` no longer costs a new connection.
- Changed: the driver vectors under `testdata/` are refreshed to the current wire contract, and a test
  now fails when the copy differs from its source.

- New: `upsert(key, doc)` on `HashBucket`, `BTreeBucket` and `Bucket`, blocking and asyncio. It replaces
  the document under the key, or creates it.
- Changed: a `put`, `insert` or `insert_many` that names a key which already holds a document now raises
  `DocumentExistsError` (code `DOCUMENT_EXISTS`, `write_outcome="not_applied"`). It was reported as
  `InternalError` with the code `UNKNOWN(18)`. On a hash or btree bucket `put` stores a new document
  and never replaces one; its documentation said it replaced. (A heap replaces the entry under a held
  key, a fifo or lifo pushes a second entry, and a blob bucket refuses `put`.) The quick start uses
  `upsert`, so it can be run again.
- New: `BatchTooLargeError` (code `BATCH_TOO_LARGE`) for a batch the store refuses whole. It was
  reported as `InternalError` with the code `UNKNOWN(17)`.
- Fixed: `insert_many` halves a batch the server refuses with `BATCH_TOO_LARGE` and sends the halves in
  order, as its documentation says. It raised instead, because it only recognised the refusal an
  earlier server sent.
- Changed: when a later part of a halved `insert_many` fails after earlier parts were stored, the error's
  `write_outcome` is `"unknown"` and `retryable` is false, whatever the failed part reported for
  itself, and its hint says how many documents were written and to resend only
  `docs[written_count:]`. It could say `"not_applied"` with a hint that began "Nothing was stored".

- A per-call `timeout=` or `request_timeout=` of zero is now refused locally, as is a negative,
  non-finite or over-an-hour value. `timeout=0` raises `InvalidArgumentError`; `request_timeout=0`
  raises `ConfigError`.
- asyncio client: an idle connection that the server closed while the event loop was not running
  (the close sits unread in the socket) is now found at checkout and replaced. Before, the pool
  handed it out, and a write on it failed with `ConnectionFailedError` (outcome unknown). The
  blocking client already behaved this way.
- New: `python -m hoarddb trust <host[:port]>` shows the key a server presents, asks once on a
  terminal and records it in `~/.hoarddb/known_servers`, so that a bare `hoarddb://host` URI works. It
  sends no password and writes nothing unless you answer yes. The README now opens with the
  five-minute path, and `examples/` holds one runnable example per store type.
- Typing: `HoardDBError`'s attributes are now declared on the class, so `err.write_outcome` is
  `Literal["applied", "not_applied", "unknown"] | None` under pyright too (it was widened to
  `str | None`). New: `scripts/smoke_dist.py`, the wheel and sdist smoke test.
