# Testing a driver

How a hoardDB driver proves it is right: frozen vectors, a conformance harness, and real servers.
Language-neutral; binds every driver. **Tests are the contract**: a driver step is done when its tests
pass against a real server, and each test was seen to fail before the code that makes it pass.

## 1. The vector files

| File | Made by | Holds |
|---|---|---|
| `testdata/driver-vectors/v1.json` | **generated**, by driving a real hoardDB server with its own encoder; never edited by hand | frames, every operation's request and response, errors, auth, trust |
| `testdata/uri-vectors/v1.json` | **hand-maintained** (a URI never crosses the wire) | connection URIs that parse, and that are refused (`trust.md` section 6) |
| `testdata/bson-vectors/v1.json` | **generated**, by the server's own BSON package, with no server running | the BSON byte layer: the bytes of every type and edge, malformed inputs, size limits (section 1.3) |

All three are Apache-2.0 and are shared by every driver repository and read-only there. A driver never edits them
(handbook `README.md`, "Changing this handbook").

### 1.1 `driver-vectors/v1.json`: the format

```json
{
  "format": "hoarddb-driver-vectors/1",
  "license": "Apache-2.0",
  "server_commit": "<40 hex>",
  "server_version": "<the server's version string>",
  "server_env": {"HOARDDB_ROOT_USER": "admin", "HOARDDB_ROOT_PASSWORD": "vectors-root-password"},
  "normalisations": ["<one sentence per normalisation applied>"],
  "vectors": [ { … }, … ]
}
```

`server_commit` is the server source the file was generated from. `server_env` is the environment
every replaying server starts with, on top of section 3.1's (the vectors' root credential is a fixed
test fixture, not a secret). `normalisations` says, in sentences, every way a captured value was
rewritten so that two generations are byte-identical; a reviewer can re-capture from them.

Every vector has `name` (unique), `group`, `kind` and `status` (`"frozen"`, or `"provisional: <what it
waits for>"`; a runner reports a provisional vector's result and never fails on it). `notes` is
optional prose: read it, it often says why the answer is what it is. The kinds:

**`frame`**: codec only. `frame_hex` is a whole frame (or, for a refusal, as much of one as the decoder
reads); `expect` is `{"result": "accept", "version", "length", "opcode", "request_id_hex",
"payload_hex"}` or `{"result": "refuse", "refuse_because"}` with `length-too-small`,
`length-too-large`, `bad-version` or `truncated`. A driver's decoder must produce exactly the decoded
fields, or refuse **without allocating the body**; its encoder, given the decoded fields of an accepted
frame, must produce `frame_hex` byte for byte.

**`exchange`**: one request and the server's answer, captured live.

```json
{
  "name": "put on a hash bucket",
  "group": "write", "kind": "exchange", "status": "frozen",
  "store_type": "hash", "topology": "single", "replay": true, "connection": "authenticated",
  "setup": [
    {"opcode": 128, "opcode_name": "CREATE_DB", "payload_json": {"name": "v049"}},
    {"opcode": 129, "opcode_name": "CREATE_BUCKET", "payload_json": {"database": "v049", "name": "users", "type": "hash", …}}
  ],
  "request": {
    "opcode": 16, "opcode_name": "PUT",
    "request_id_hex": "766563746f722d69642d763100000031",
    "payload_json": {"database": "v049", "bucket": "users", "key": "ada",
                     "data": {"Born": {"$numberInt": "1815"}, "Name": "Ada"}, "writeconcern": ""},
    "payload_hex": "…",
    "allowed_fields": ["database", "bucket", "key", "data", "writeconcern"]
  },
  "response": {
    "opcode": 16, "opcode_name": "PUT",
    "request_id_hex": "766563746f722d69642d763100000031",
    "payload_hex": "…",
    "payload_json": {"id": "ada", "success": true, "message": "", "seq": {"$numberLong": "1"}},
    "fields": ["id", "success", "message", "seq", "copies_confirmed", "replication_factor", "write_outcome"],
    "vary": ["seq"],
    "additive_fields": ["copies_confirmed", "replication_factor", "write_outcome"],
    "absent_fields": []
  }
}
```

- `payload_json` is canonical Extended JSON (version 2): integer widths, binary subtypes, datetimes and
  object ids are explicit. Both drivers' BSON codecs read it.
- `connection` is `authenticated` (the harness opens a connection and sends a password `AUTH` as the
  root user of `server_env` before anything else) or `fresh` (nothing is sent before the setup; the
  `AUTH` vectors and the pre-authentication ones).
- `setup` is a list of requests (opcode and payload) the harness sends first, on the same connection,
  each expected to succeed. Every exchange uses its own database name (`v001`, `v002`, …), so vectors
  are independent; the harness runs them against a **fresh** server started with `server_env` and the
  vector's own `server_env` (an operator limit the vector needs, such as a small document limit), on
  the vector's `topology`: `single`, or `cluster3` (section 3.2; the vector is sent to `node1`, or to
  the node its notes name).
- `request.payload_hex` is what the server's own encoder produced. A driver does **not** have to match
  it byte for byte (field order and the omission of empty optional fields are free, `wire.md` section 3).
  It must: encode the request from its typed request type, then show that the bytes decode to a document
  whose fields are all in `allowed_fields` and which carries every field of `payload_json` with an equal
  value. Two allowances: a field whose `payload_json` value is empty (`""`, `null`, `0`, `false`, an
  empty array or document) may be absent instead, because the server's encoder writes some empty fields
  a driver omits; and integers are equal by value whatever their width (the server's encoder writes
  `protocol_version` as `int64`; a driver may write `int32`). A request with `payload_hex` and no
  `payload_json` is raw bytes on purpose (`GET_FAST`'s compact form, or a payload that is not BSON):
  the harness sends it as it is.
- `allowed_fields` is the request type's wire names minus the ones a client never sends (`wire.md`
  section 3). A driver's request type uses no other name.
- `response.payload_hex` is the server's answer as captured, then normalised. A driver decodes it into
  its typed response and shows the typed values equal `payload_json`, except the fields in `vary`
  (values that differ run to run: ids, sequence numbers, timestamps, versions, uptimes, tokens), whose
  **presence and BSON type** it checks instead. A `vary` entry is a path: `seq`, `data._id_`,
  `nodes[].joinedat` (`[]` is every element of an array).
- `fields` is every wire name the answer's **type** can carry, nested names as paths
  (`buckets[].name`), whether or not this answer carries it. A driver's typed response may model any
  subset of it and must decode every one of them without failing. A server change that adds or removes
  one changes the vectors, so it cannot reach drivers unannounced.
- `additive_fields` may be present or absent and are never required (they appear on a cluster and not on
  one node, describe the capturing host, or come from a newer server). `absent_fields` must not be
  present.
- An `ERROR` answer adds `"error": {"code": 1, "name": "NOT_FOUND", "write_outcome": "not_applied",
  "message_contains": ["…"]}`, and an `AUTH_FAIL` answer `"auth_fail": {"reason": 1, "name":
  "CREDENTIALS", "message_contains": ["…"]}`: the error the driver must report. `message_contains`
  holds only stable parts of the server's sentence; the whole sentence is in `payload_json`, and a driver
  passes it through verbatim.
- `replay: false` marks an answer the harness cannot reproduce by sending the vector (a token minted at
  capture time, a user created or a node stopped before the request, or a setup too large for the
  file); `notes` says which. It is still a decode vector, and leg 3 skips it; the leg named in its notes
  exercises the behaviour live.

**`decode`**: a frame the server sends that is not the answer to a vector's request: an answer a test
cannot provoke on demand (an error with code `0`, an unknown code, an empty `AUTH_FAIL`), built with the
server's own types and encoder, or a connection-level refusal captured live (`OVERLOADED` and the ALPN
refusal, with a zero request ID; the answer to an oversize request header, whose bytes are in
`request_header_hex`). `frame_hex` is the frame; `expect` is what the driver reports:
`{"result": "error", "code", "name", "request_id_zero", "connection_fatal", "message_contains"?}`,
`{"result": "auth_fail", "auth_fail_reason", "name", "request_id_zero"}`, or `{"result": "answer",
"payload_json"}` (a success frame whose typed values must equal `payload_json`). A decode vector captured
on a tuned server carries its `server_env`.

**`trust`**: a key, `{"public_key_hex", "certificate_pem", "fingerprint"}` (the fingerprint each driver
must compute from the key, and from the certificate), or a `known_servers` case `{"known_servers":
{"file_text", "entries": {"host:port": "<raw key hex>"}}}` (the file loads, to exactly these entries) or
`{"known_servers": {"file_text", "error_contains": "…"}}` (loading the file is refused).

### 1.2 What the vectors cover

One exchange per client-facing opcode and per store type it applies to; the miss and refusal shapes
(`found: false`, `success: false`); every error code the server sends; the AUTH success, every
`AUTH_FAIL` reason and the empty payload; the oversize-request answer; the size refusals; every
`create bucket` declaration key; the field-store verbs; the write outcome on a three-node cluster (an
`all` write with one node stopped, answered with `copies_confirmed` below `replication_factor`; the
refusals of a node not yet added); one vector per wire name that is easy to get wrong (`writeconcern`,
`addtoset`, `blobid`, `bucketcount`, `nodeid`, `joinedat`, `schemajson`); BSON edge values
(datetime to the millisecond, object id, every binary subtype in use, `int32` against `int64`,
`decimal128`); the delete request's `expire_if` and the response's `spared` and `skipped`.

What is deliberately not a vector, and where it is tested instead:

- Query pipelining (not offered by any server yet), and a server that **does not** answer (a bad
  version byte): there is no response to freeze; that is a harness leg.
- Anything whose request or setup is tens of megabytes, because the file would be: a 16 MiB binary
  value, the replication-fit refusal (a ~31 MiB document), the response-frame guard, and a batch over
  the atomic bound (about 5 MB). Leg 6 provokes them live.
- Refusals a test cluster cannot provoke deterministically (a membership change under a write, a read
  at a node that does not hold the bucket, `CONFLICT`); `errors.md` names them, and a driver handles
  them by code and `write_outcome`, which the decode vectors pin.

### 1.3 `bson-vectors/v1.json`: the BSON codec corpus

A driver that writes its own BSON codec (Python and C++) is held to this file **byte for byte**. A
driver that uses a library for BSON does not need it. The bytes were written by the server's own
encoder and read by its decoder, never typed in by hand, and the generator is committed beside the
server (`make bson-vectors`; `make bson-vectors-check` and `go test ./cmd/gen-testvectors` fail when
the file and the encoder disagree, so a byte can only change in a reviewed diff).

The header has `format` (`hoarddb-bson-vectors/1`), `server_commit`, `generated_by`, `limits` (the
depth limit and the frame and document sizes the vectors sit on) and two glossaries, `value_forms` and
`kinds`, which are the normative description of the rest. Values are written in a **typed JSON form**
so a test can build them without a BSON library: a document is `{"$doc": [[key, value], ...]}`, an
ordered list of pairs (order is the wire order, and a key may repeat); an int32 is
`{"$numberInt": "5"}`, an int64 `{"$numberLong": "5"}`, a double `{"$numberDouble": "1.5"}` (also
`"NaN"`, `"Infinity"`, `"-Infinity"`, `"-0"`), a decimal128 `{"$numberDecimal": "1.50"}`, a datetime
`{"$date": {"$numberLong": "-1"}}`, and so on for object id, binary, timestamp, regex and the rest;
strings, booleans, null and arrays are plain JSON. Every vector is one line, with `name`, `group`,
`kind` and optional `notes`. The kinds:

- **`encode`**: `document` and `hex`. Writing the document must give exactly these bytes, and reading the
  bytes must give the document back.
- **`decode`**: `hex` and `document`. Bytes a decoder must accept, with `reencodes_identically` saying
  whether writing the result gives the same bytes (false for an input a writer would normalise, such as
  array keys out of order or regex options). `last_wins_document` is the mapping form of a repeated key.
- **`reject`**: `hex`, `refusal` (`ProtocolError`) and `reason`. A decoder must refuse these bytes with
  that class, and never crash, hang, allocate by the declared length or raise a language-level
  exception. `server_decoder` records what the server does with the same bytes. Where it says
  `accepted`, the spec is stricter than the server (invalid UTF-8, nesting past the depth limit): the
  driver follows the spec, and `notes` says so. Match on `refusal`, never on `reason`: `reason` labels
  the first rule the generator broke on purpose, but an input can break several rules at once and
  decoders check them in different orders, so two correct decoders can name different reasons for the
  same bytes.
- **`encode_refuse`**: `document` and `refusal` (`InvalidArgumentError`). An encoder must refuse before
  writing a byte (a key holding NUL).
- **`datetime_floor`**: `instant`, `expected_millis` and `hex`. Sub-millisecond digits floor toward
  negative infinity: `1969-12-31T23:59:59.9995Z` is `-1`, not `0`.
- **`size`**: a `recipe`, `document_bytes` and `sha256`, for documents too large to list as hex (24 MiB
  and the largest frame payload, each at the limit and one byte over). Build `{key: fill repeated
  fill_bytes times}`, encode it, and check the length and the SHA-256; `fits_one_frame` says whether a
  driver may send it.

A driver replays the file in its unit layer (section 4, no server needed) and reports each failing
vector by `name`. The file is shared by every driver repository and read-only there, like the other two, and a
driver never edits it; a change a driver needs is a change to the generator.

## 2. The conformance harness

One runner per driver, in the driver's own repository and language (Go: a `conformance` package run by
`go test`), reporting one line per leg. Every driver must be green on every leg; it is the gate for every
later language too.

| Leg | Asserts | Server |
|---|---|---|
| 1 Codec | every `frame`, `exchange` (decode and request-encode checks) and `decode` vector | none |
| 2 Trust | every `trust` vector: the identical fingerprint string from a key and from its certificate; `known_servers` parsing; an unknown key is refused; a changed key is refused naming both; insecure mode is off unless asked for with a reason | one node |
| 3 Replay | every `exchange` vector with `replay: true` and its topology, sent by the driver's **public API** where a verb exists and by its raw wire layer otherwise, compared as in 1.1 | one node, and three |
| 4 Workload | per store type: write N documents through the public API, read them back, assert every value is equal (field by field and BSON type for type; the server does not keep a document's field order, `wire.md` section 3) and report `hits=N misses=0`. A driver that returns nothing with no error fails here | one node |
| 5 Errors | the same bad requests produce the same code and error class, and the server's message verbatim: missing bucket, missing database, duplicate database, bucket of another type, unauthenticated request, oversized value, a mistyped document | one node |
| 6 Limits | a document just under and just over 24 MiB; a blob at 16 MiB and just over; a write that cannot fit a replication frame; the driver refuses a frame over 32 MiB before sending, and a raw-socket test shows the server answers one by its request ID; a raw-socket test shows a bad version byte is closed without an answer; a query over the result ceiling is `RESULT_TOO_LARGE` and the **same connection** then answers `PING`; the per-address cap surfaces `OVERLOADED` with a zero request ID and the pool survives it (start the server with a small cap) | one node, tuned |
| 7 Lifecycle | the server killed mid-request: the driver reports a local `UNAVAILABLE`-class error, does not hang, and recovers on the next request after a restart; an idle connection the server closed is detected and replaced, not used | one node |
| 8 Auth | **on a dedicated server**: a wrong password is `CREDENTIALS` and the next attempt dials a new connection; a token minted on one connection authenticates another; an expired or invalid token falls back to the password exactly once; `RATE_LIMITED` is not retried in a loop; a dropped user's pooled connections close and the next call reports the refusal | one node, its own |
| 9 Cluster | an `all` write with one node stopped is answered success with `copies_confirmed` below `replication_factor`; a write concern word the server does not know is refused; a write at a node not yet added is `UNAVAILABLE`, `not_applied`; `consistent get` reads back a just-acknowledged write; a node that leaves does not break the pool | three nodes |
| 10 Pipelining | provisional until a server advertises `query_pipelining` | — |

## 3. Getting a real server

**No test-server binary is published yet**, so `scripts/fetch-server.sh` fails with HTTP 404. Build
`hoarddb-server` (and the `hoarddb` command line, where a leg needs it) from
`github.com/hoarddb/hoarddb-server` and set `HOARDDB_SERVER_BIN` (and `HOARDDB_CLI_BIN`). What follows
describes the download for when a release carries one.

The driver repositories do not contain the server's source. They test against the **server binary**,
published as an asset of a release of the driver repository, and named, with its SHA-256, in
`testing/server.json`:

```json
{ "tag": "test-server-<server commit, 12 hex>",
  "server_commit": "<40 hex>",
  "assets": { "linux-amd64": {"name": "hoardDB-server-linux-amd64", "sha256": "<64 hex>"},
              "linux-arm64": {"name": "hoardDB-server-linux-arm64", "sha256": "<64 hex>"} } }
```

The binary is from the **same server commit as the vectors** (`server_commit` in both files must
match). `scripts/fetch-server.sh` downloads the asset for the machine's platform through the GitHub
API, checks the SHA-256, and prints the path; tests read it from `HOARDDB_SERVER_BIN`. A CI job uses the
workflow's token; an interactive session uses a token with read access to the repository's releases.
The binary is the hoardDB server, under its own licence (shipped beside it as an asset); it is a test
fixture, never vendored into the driver's source tree.

### 3.1 One node

    HOARDDB_DATA_DIR=<empty dir>                  # a fresh directory per test server
    HOARDDB_TCP_LISTEN=127.0.0.1:<free port>      # the driver port
    HOARDDB_LISTEN=127.0.0.1:<free port>          # the ring identity (nothing else binds it)
    HOARDDB_TCP_INTERNODE_LISTEN=127.0.0.1:<free port>
    HOARDDB_METRICS_LISTEN=127.0.0.1:<free port>  # default 9090 would collide between servers
    HOARDDB_ROOT_USER=admin
    HOARDDB_ROOT_PASSWORD=<a test password>
    HOME=<a private directory>                    # so no real ~/.hoarddb is touched

Start the binary with only these in its environment, and wait for the log line that carries
`"fingerprint":"SHA256:…"` (`trust.md` section 1): that is "ready", and it is the pin. Stop it with
SIGTERM, then SIGKILL after 15 s. Operator limits a leg needs are set the same way, for example
`HOARDDB_TCP_MAX_CONNS_PER_IP`, `HOARDDB_TCP_AUTH_MAX_FAILS`, `HOARDDB_MAX_DOCUMENT_BYTES`,
`HOARDDB_MAX_BLOB_BYTES`, `HOARDDB_QUERY_MAX_RESULT_BYTES`, `HOARDDB_TCP_IDLE_TIMEOUT`.

### 3.2 Three nodes, on one machine

**This recipe does not start a v1.1.0 server.** It selects the earlier engine's replicated mode
(`HOARDDB_REPLICATION_FACTOR` with a seed list), which a v1.1.0 server refuses at start. It is kept for the
server commit named in `testing/server.json`.

Proven on loopback (three processes, one machine) with the server this handbook was derived from:

1. For each node `nodeN`, make a data directory and write its identity into `<data dir>/keys/`: an
   ed25519 key as PKCS#8 PEM (`-----BEGIN PRIVATE KEY-----`) in `server.key` (mode `0600`) and a
   self-signed X.509 certificate for it as PEM in `server.crt`. Compute each node's fingerprint
   (`trust.md` section 2).
2. Choose, per node, a ring port `R` whose successor `R+1` is also free (the internode listener is
   always on the ring port plus one), a driver port and a metrics port.
3. Start every node with the single-node variables above, except `HOARDDB_TCP_INTERNODE_LISTEN`, plus:

       HOARDDB_NODE_ID=nodeN
       HOARDDB_LISTEN=127.0.0.1:R_N
       HOARDDB_SEED_NODES=node1@127.0.0.1:R_1,node2@127.0.0.1:R_2,node3@127.0.0.1:R_3
       HOARDDB_SEED_FINGERPRINTS=127.0.0.1:R_1=SHA256:<fp1>,127.0.0.1:R_2=SHA256:<fp2>,127.0.0.1:R_3=SHA256:<fp3>
       HOARDDB_CLUSTER_TOKEN=<the same 64 hex characters on every node>
       HOARDDB_REPLICATION_FACTOR=3

   and the **same** root user and password on every node.
4. Ready when every node's `HEALTH_CHECK` answers `membership: confirmed` (the logs also say
   "successfully joined cluster via seed"). Until then a node refuses requests that name a database or
   bucket with `UNAVAILABLE`.
5. A write on any node is acknowledged with `copies_confirmed`; with all three up the default write
   concern reports 2 or 3 of 3.

To stop a node for a leg, SIGTERM it; to bring it back, start it again with the same data directory and
variables.

### 3.3 With containers

The Go repository's `testing/compose/` holds the same three-node cluster as a compose file. Like the
recipe in 3.2 it starts the earlier engine's replicated mode and does not come up with a v1.1.0 binary.
This repository has no compose file.

## 4. Test layers inside a driver

| Layer | What | Needs a server |
|---|---|---|
| Unit | codec (and, for a driver with its own BSON codec, the whole of `bson-vectors/v1.json`), error decode, URI parser, fingerprint, `known_servers` read and write, option validation, secret redaction | no |
| Component | the connection and the pool against an **in-process fake server** written with the language's own TLS: out-of-order responses, a socket closed mid-frame, a silent server (deadline), `OVERLOADED` on dial, a close after an error frame, an idle close, token expiry and fallback, ALPN refusal | no |
| Fuzz | the frame decoder, the URI parser, the error decoder: no crash, no allocation past the 32 MiB cap | no |
| Race and leaks | the race detector over everything; every background task counted before connect and after close, under load | no |
| Conformance | section 2 | yes |
| Examples | every runnable example and every README snippet is compiled, and run against a real server | yes |

**Failing first.** Every test is written before the code that makes it pass and is seen to fail on an
assertion (not a compile error or a timeout). A negative test is proven by breaking the guard it relies
on by hand, watching it fail, and restoring the code exactly.

## 5. Continuous integration

What a driver's pull-request checks are meant to cover: the unit, component and fuzz-seed tests with the
race detector; the linter; the vulnerability check; and the licence and boundary checks (the driver
imports no server code; every source file carries the Apache-2.0 header; no third-party type appears in
the public API; no other database product is named outside the allow-listed lines). The conformance
harness needs a server binary, so until a test-server binary is published it is run by hand against a
binary built from source (section 3).
