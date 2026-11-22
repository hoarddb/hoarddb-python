# hoardDB driver handbook

Everything a hoardDB client driver is built from, in one place. It is written for a developer who has a driver repository, this handbook and the frozen test vectors, and
nothing else: no server source and no other documents.

Licensed under the Apache License, Version 2.0, like the drivers it describes. Copyright 2026
Boeger IT Sh.p.k.

## Precedence

1. **The frozen test vectors win** (`testdata/driver-vectors/v1.json`, `testdata/uri-vectors/v1.json`).
   They were produced by driving a real hoardDB server with its own encoder. Where this handbook and a
   vector disagree, the vector is right and this handbook has a bug: report it (see "Changing this
   handbook" below).
2. **Then this handbook.** The language-neutral chapters bind every driver. A language chapter
   (`api-go.md`) binds only its driver.
3. **Nothing else.** Not another database's driver, documentation, option names or error text. hoardDB
   drivers are clean-room: see "Clean room" below.

A vector marked `"status": "provisional: <reason>"` is informative, never a failure; the reason names
what it waits for.

Each driver lives in its own repository (the Go driver is `github.com/hoarddb/hoarddb-go`) and carries
this handbook and the frozen vectors. The Go driver's no-server-import boundary test
(`boundary_test.go`) runs in its repository.

## The chapters

| File | What it decides | Binds |
|---|---|---|
| [`wire.md`](wire.md) | transport, ports, ALPN, the 22-byte frame, request IDs, the opcode table, BSON payload rules, limits, timeouts, connection-level refusals | every driver |
| [`trust.md`](trust.md) | TLS 1.3 trust without a certificate authority: fingerprints, pinning, the shared `known_servers` file, the named insecure mode; the connection URI | every driver |
| [`auth.md`](auth.md) | the AUTH exchange, the eight refusal reasons and what to do on each, tokens and refresh, the failed-login limiter, roles | every driver |
| [`errors.md`](errors.md) | error codes, the three refusal channels, the write outcome, the retry table, which connection survives which error | every driver |
| [`operations.md`](operations.md) | the six store types and which verbs each answers, every operation's request and response fields, bucket declarations, field stores, cluster behaviour | every driver |
| [`testing.md`](testing.md) | the vector format, the conformance legs, how to get a real server (one node and three) to test against, CI | every driver |
| [`api-go.md`](api-go.md) | the Go driver's public API | the Go driver |
| `api-node.md` | the Node.js/TypeScript driver's public API | written when that driver starts |

## Words

hoardDB is the product's name in prose. **In code everything is lowercase**: the Go module is
`github.com/hoarddb/hoarddb-go`, the Go package is `hoarddb`, the npm package is `hoarddb`, the URI
scheme is `hoarddb://`, the ALPN string is `hoarddb/1`.

- **Database**: a namespace of buckets.
- **Bucket**: the unit of storage. Every bucket has one **store type**: `fifo`, `lifo`, `hash`, `btree`,
  `heap` or `blob`.
- **Store field**: a field a bucket declares whose value, in **each document**, is itself a store of one
  of the six types. Every user document can carry its own queue, its own table, its own heap.
- **Node**: one server process. A **cluster** is several nodes; each bucket has an **owner** node and
  replicas.
- **Write concern**: `one`, `majority` or `all`: how many copies must confirm a write before it is
  acknowledged.

hoardDB is built for very many small buckets: one per entity, and one per entity's store field, so
millions of them. A driver must therefore hold **no per-bucket state**: a bucket handle costs a few
bytes, does no I/O when created and holds no connection, goroutine, timer, cache entry or metric.

## Clean room

Drivers are written from this handbook, the vectors and the server's observable behaviour only. A
driver author must not read, copy, port, translate or closely paraphrase the source, documentation,
comments, examples, error messages or test suites of any other database's drivers or connectors, and
must not name another database product in code, identifiers, error messages, documentation or examples.
BSON is a data format; using it is not a claim of compatibility with anything. A published library may
be used through its public API as a black box (the BSON codec is one); its source is not read.

## Changing this handbook

The handbook and `testdata/` are the same in every driver repository, so a change to one is a change
to all of them. If something here is wrong, missing, or contradicted by a server you ran, open an
issue that says what you read, what you observed, and the test or command that shows it. A driver
that "fixes" a vector to make its own test pass has hidden a wire change from every other driver.

This handbook was derived from the hoardDB server at the commit recorded in
`testdata/driver-vectors/v1.json` (`server_commit`).
