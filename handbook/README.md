# hoardDB driver handbook

Everything a hoardDB client driver is built from, in one place. It is written for a developer who has a driver repository, this handbook and the frozen test vectors, and
nothing else: no server source and no other documents.

Licensed under the Apache License, Version 2.0, like the drivers it describes. Copyright 2026
Boeger IT Sh.p.k.

## Read this first: what this handbook describes

This handbook describes the protocol **this driver sends**: the opcodes of the hoardDB server's earlier
single-store engine, in which a bucket has one of six store types. It does not describe the ring protocol
of a hoardDB v1.1 server. That one is described in the handbook of the `hoarddb-go` repository
(`github.com/hoarddb/hoarddb-go`, directory `handbook/`).

A v1.1.0 server still answers the opcodes described here. Read these four points before the chapters:
where a chapter disagrees with one of them, the point is right for a v1.1.0 server.

1. **The data is separate.** The server answers these opcodes from the earlier engine's own store. Its
   databases and buckets are not the ring's: the `hoarddb` command line, `hoarddb dump` and a ring client
   do not see them, and this driver does not see theirs.
2. **There is no cluster for this protocol.** A v1.1.0 server refuses to start in the earlier engine's
   replicated mode. Every statement here about a cluster, an owner node, forwarding, replicas, copies of
   a bucket, a write concern met by several nodes, a node joining or leaving, and the three-node test
   recipe describes that mode. On a v1.1.0 cluster each node answers these opcodes from its own local
   store, and nothing is copied between nodes.
3. **The server and the command line are `hoarddb-server` and `hoarddb`.** A first start with no root
   password set asks for one on a terminal, or stops and says how to set `HOARDDB_ROOT_PASSWORD`; it does
   not print generated credentials.
4. **No test-server binary is published.** Build the server from `github.com/hoarddb/hoarddb-server`
   (`testing.md` section 3).

The chapters were written against the server commit recorded in `testdata/driver-vectors/v1.json`
(`server_commit`). Apart from the notes added for the points above, they have not been checked again,
statement by statement, against a v1.1.0 server.

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

Each driver lives in its own repository (the Go driver is `github.com/hoarddb/hoarddb-go`). This
repository carries this handbook and the vectors this driver replays.

## The chapters

| File | What it decides | Binds |
|---|---|---|
| [`wire.md`](wire.md) | transport, ports, ALPN, the 22-byte frame, request IDs, the opcode table, BSON payload rules, limits, timeouts, connection-level refusals | every driver |
| [`trust.md`](trust.md) | TLS 1.3 trust without a certificate authority: fingerprints, pinning, the shared `known_servers` file, the named insecure mode; the connection URI | every driver |
| [`auth.md`](auth.md) | the AUTH exchange, the eight refusal reasons and what to do on each, tokens and refresh, the failed-login limiter, roles | every driver |
| [`errors.md`](errors.md) | error codes, the three refusal channels, the write outcome, the retry table, which connection survives which error | every driver |
| [`operations.md`](operations.md) | the six store types and which verbs each answers, every operation's request and response fields, bucket declarations, field stores, cluster behaviour | every driver |
| [`testing.md`](testing.md) | the vector format, the conformance legs, how to get a real server (one node and three) to test against, CI | every driver |
| [`api-go.md`](api-go.md) | the public API of the Go module's root package `hoarddb`, which sends the same opcodes; it does not describe the Go ring client | the Go root package |

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

hoardDB is built for many small buckets, thousands per node. A driver must therefore hold **no
per-bucket state**: a bucket handle costs a few bytes, does no I/O when created and holds no connection,
goroutine, timer, cache entry or metric.

## Clean room

Drivers are written from this handbook, the vectors and the server's observable behaviour only. A
driver author must not read, copy, port, translate or closely paraphrase the source, documentation,
comments, examples, error messages or test suites of any other database's drivers or connectors, and
must not name another database product in code, identifiers, error messages, documentation or examples.
BSON is a data format; using it is not a claim of compatibility with anything. A published library may
be used through its public API as a black box (the BSON codec is one); its source is not read.

## Changing this handbook

The handbook and `testdata/` are not edited by this driver's build. If something here is wrong, missing, or contradicted by a server you ran, open an
issue that says what you read, what you observed, and the test or command that shows it. A driver
that "fixes" a vector to make its own test pass has hidden a wire change from every other driver.

This handbook was derived from the hoardDB server at the commit recorded in
`testdata/driver-vectors/v1.json` (`server_commit`), which is older than v1.1.0.
