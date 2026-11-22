# The Go driver's API

Module `github.com/hoarddb/hoarddb-go`, package `hoarddb`, Go 1.23 or later (a floor, not a pin; the
cursor uses range-over-func). Binds the Go driver only. Every name here comes from a hoardDB concept
(the opcode, the store type, the bucket) or from a Go standard-library convention, never from another
database's driver.

The connection layer already exists: `Connect`, the options, trust, AUTH with tokens, the pool, `Ping`,
`ServerInfo`, `PoolStats` and the `Error` type, with the unstable wire package `hoarddbwire`. This
chapter is the whole v1 surface.

## 1. Shape

```
Client                one per program; a pool of connections; safe for many goroutines
 └─ Database          a name; no I/O, no resources
     └─ Bucket        a name; no I/O, no resources; every verb of every store type
         └─ FieldStore   one document's store field; no I/O, no resources
```

Rule 7 (predictable APIs): the same verb has the same name, argument order and result shape on every
type that answers it. Argument order is always `ctx`, then names from outer to inner (database, bucket,
key, element), then the document, then options. Options are small function types of this package; a
call that needs six or more inputs takes a struct.

**Handles cost nothing.** `client.Database("app").Bucket("users")` allocates two small structs, does
no I/O, holds no connection, goroutine, timer, cache entry or metric, and is registered nowhere. A
program may hold a million. The first error appears on the first operation, where it carries the fix.
The driver never looks a bucket's store type up: a verb the type does not answer is the server's
refusal, passed through.

## 2. Connecting

```go
func Connect(ctx context.Context, uri string, opts ...Option) (*Client, error) // "" reads HOARDDB_URI
func (c *Client) Close() error                      // idempotent, any goroutine
func (c *Client) Ping(ctx context.Context) error
func (c *Client) ServerInfo() ServerInfo            // version, protocol, capabilities; no round trip
func (c *Client) PoolStats() PoolStats              // aggregate counters, never per bucket
func (c *Client) Health(ctx context.Context) (*Health, error)
func (c *Client) ListNodes(ctx context.Context) ([]Node, error)
```

Options (an option overrides the URI, which overrides the environment):

| Option | Default | Notes |
|---|---|---|
| `WithCredentials(user, password)` | URI, else `HOARDDB_ROOT_USER` (`admin`) and `HOARDDB_ROOT_PASSWORD` | |
| `CredentialsFromEnv()` | | the explicit spelling of the environment rule |
| `WithToken(token)` | | a resumption token, not a password (`auth.md` section 3) |
| `WithFingerprint(fp...)` | | pins, positional per host (`trust.md` section 6) |
| `WithKnownServers(path)` | `~/.hoarddb/known_servers` | the trust store the CLI writes |
| `WithTrustPrompt(func(host, fp string) bool)` | none: an unknown server is refused | |
| `AcceptFirstSight` | | a ready-made prompt for local development; logs a warning |
| `WithInsecureSkipVerify(reason)` | off | reason required; logged at WARN on every dial; not reachable from a URI |
| `WithPoolSize(n)` | 4 per host | the point lane |
| `WithBulkPoolSize(n)` | 2 per host | the bulk lane: blobs, `Find` and `Range` pages, `Search`, `Distinct`, `InsertMany` |
| `WithRequestTimeout(d)` | 30 s | applied when the `ctx` has no deadline |
| `WithDefaultWriteConcern(wc)` | the server's default | `One`, `Majority`, `All` |
| `WithClientName(name)` | | appended to `hoarddb-go/<version>` in AUTH |
| `WithLogger(*slog.Logger)` | discard | a library does not write to stderr |

There is no option that turns TLS off, and no write concern or trust bypass in a URI.

## 3. Databases and buckets

```go
func (c *Client) Database(name string) *Database
func (c *Client) DefaultDatabase() (*Database, error)   // from the URI path; the error names the missing segment
func (c *Client) DefaultBucket() (*Bucket, error)
func (c *Client) CreateDatabase(ctx context.Context, name string, opts ...CreateOption) error
func (c *Client) DropDatabase(ctx context.Context, name string) error
func (c *Client) ListDatabases(ctx context.Context) ([]DatabaseInfo, error)

func (d *Database) Name() string
func (d *Database) Bucket(name string) *Bucket
func (d *Database) CreateBucket(ctx context.Context, name string, kind StoreType, opts ...CreateOption) (*Bucket, error)
func (d *Database) DropBucket(ctx context.Context, name string) error
func (d *Database) ListBuckets(ctx context.Context) ([]BucketInfo, error)   // one frame, no paging: an operator call
func (d *Database) DeclareStoreFields(ctx context.Context, bucket string, stores ...StoreSpec) error

type StoreType string
const (FIFO StoreType = "fifo"; LIFO StoreType = "lifo"; Hash StoreType = "hash";
       BTree StoreType = "btree"; Heap StoreType = "heap"; Blob StoreType = "blob")

func WithIfNotExists() CreateOption                   // default off; an existing bucket of another type is still an error
func WithEncrypted() CreateOption
func WithReplicationFactor(n int) CreateOption        // needs the bucket_rf capability, else ErrUnsupportedByServer, nothing sent
func WithStores(stores ...StoreSpec) CreateOption     // store fields: StoreSpec{Name, Type StoreType, Stores []StoreSpec}
func WithIndexes(indexes ...IndexSpec) CreateOption   // IndexSpec{Fields []string, Unique bool, Name string, ExpireAfter time.Duration}; hash buckets only
func WithFieldTypes(mode TypeMode, fields ...FieldSpec) CreateOption // TypeMode: Strict, Warn, Off; FieldSpec{Name, Type string, Required bool, Members []FieldSpec, Elem *FieldSpec}
```

`CreateDatabase` of an existing name is `ErrAlreadyExists` unless `WithIfNotExists()`. `CreateBucket` of
an existing name is `ErrAlreadyExists` when the type differs (always) and when it matches without
`WithIfNotExists()` (the driver reads the server's idempotent "already exists" answer and reports it,
because rule 7 says `create` means the same everywhere). The godoc says that creating a bucket in a
database that does not exist creates the database.

### 3.1 What a bucket is declared as: `Info`

```go
// Info returns the bucket's declaration: its store type, its entry count and its top-level store
// fields, in one round trip (BUCKET_INFO). Read-only and safe to retry. Its cost does not depend on how
// many elements the store fields hold: it never walks them (use Stats for element counts). A bucket or
// database that does not exist is ErrNotFound; a caller without read permission is ErrPermissionDenied.
func (b *Bucket) Info(ctx context.Context) (*BucketDeclaration, error)

type BucketDeclaration struct {
    Name              string
    Type              StoreType        // the bucket's own store type
    Entries           int64
    Stores            []string         // the declared top-level store field names, in declaration order
    StoreFields       []StoreFieldInfo // the same fields with their engines, in the same order; empty when there are none
    ReplicationFactor int              // 0: the server sent none (not reported)
    Inherited         bool             // the bucket's replication factor comes from its database; false when absent
}

type StoreFieldInfo struct {
    Name   string
    Engine StoreType // the wire word as is: Hash, BTree, FIFO, LIFO, Heap, Blob; a word this driver does not know is passed through unchanged
}
```

`StoreFieldInfo.Engine` reuses `StoreType` because the wire's engine words are the catalog words
(`hash`, `btree`, `fifo`, `lifo`, `heap`, `blob`, no `Store` suffix): `StoreType` is a string type, so a
word added by a newer server arrives as a `StoreType` value no constant names, and is never dropped or
turned into an error. Compare with the constants for the ones you know, and treat any other as opaque.
`Info` lists top-level store fields only; a store nested inside an element is not listed. The wire's
`outside` member is not exposed. Like every handle verb it keeps no state: nothing is cached on the
`Bucket`, so the next `Info` asks the server again.

```go
decl, err := client.Database("app").Bucket("users").Info(ctx)
if errors.Is(err, hoarddb.ErrNotFound) {
    log.Fatal("no such bucket: create it first") // the error's own message carries the exact command
}
if err != nil {
    log.Fatal(err)
}
fmt.Printf("%s is a %s bucket with %d entries\n", decl.Name, decl.Type, decl.Entries)
for _, f := range decl.StoreFields { // label each store field of a document
    fmt.Printf("  %s: %s\n", f.Name, f.Engine)
}
```

## 4. Documents and results

```go
type Document []byte                              // one stored document: raw BSON, decoded only on request
func (d Document) Decode(into any) error
func (d Document) Lookup(path ...string) (Value, bool)
func (d Document) String() string                 // relaxed Extended JSON, for logs

type Value struct{ /* one BSON element */ }       // typed accessors: String, Int64, Float64, Bool, Time, ...
type Map = map[string]any                          // a literal document

type Receipt struct {                              // every single-document write
    ID                string                       // the key; server-assigned for Insert and Push
    Seq               uint64
    CopiesConfirmed   int                          // 0: not reported (a single node, or an older server)
    ReplicationFactor int
}
type BatchReceipt struct { IDs []string; Count int; CopiesConfirmed, ReplicationFactor int }
type Entry struct { Key string; Value Document }   // btree range, first, last
type Blob struct { Key, BlobID string; Data []byte; Size int64; Metadata Document }
type BlobReceipt struct { Receipt; BlobID string; Size int64 }
```

```go
// GetAs is Get then Decode: the document under key, decoded into a T (a struct with bson tags, or Map).
// A miss is ErrNotFound with the zero T; a document that does not decode into T is ErrInvalidArgument,
// naming the key and the field. A function, not a method, because Go methods take no type parameters.
func GetAs[T any](ctx context.Context, b *Bucket, key string, opts ...ReadOption) (T, error)
```

A document going in is `any`: a struct with `bson:"…"` tags, a `Map`, or a `Document` passed through
untouched. No type from the BSON library appears in any exported signature, field, godoc or example;
the codec sits behind `internal/codec` and could be replaced without an API change.

## 5. The verbs

```go
// keyed stores: hash, btree
func (b *Bucket) Put(ctx context.Context, key string, doc any, opts ...WriteOption) (Receipt, error)
func (b *Bucket) Insert(ctx context.Context, doc any, opts ...WriteOption) (Receipt, error)            // server assigns the key
func (b *Bucket) InsertMany(ctx context.Context, docs []any, opts ...WriteOption) (BatchReceipt, error)  // bulk lane
func (b *Bucket) Get(ctx context.Context, key string, opts ...ReadOption) (Document, error)            // ErrNotFound on a miss
func (b *Bucket) Exists(ctx context.Context, key string) (bool, error)
func (b *Bucket) Delete(ctx context.Context, key string, opts ...WriteOption) (bool, error)             // false: nothing was there
func (b *Bucket) Update(ctx context.Context, key string, update Update, opts ...WriteOption) (Document, error)

// queues and heaps: fifo, lifo, heap
func (b *Bucket) Push(ctx context.Context, doc any, opts ...WriteOption) (Receipt, error)              // heap: WithPriority optional; without it the element goes in at priority 0
func (b *Bucket) Pop(ctx context.Context, opts ...WriteOption) (Document, error)                       // ErrEmpty when there is nothing
func (b *Bucket) Peek(ctx context.Context, opts ...PeekOption) ([]Document, error)                       // ErrEmpty; WithLimit(n), 1 to 1000, default 1; pop order, none removed

// btree
type Bounds struct{ Gte, Lte string }                                                                   // an empty side is open
func (b *Bucket) Range(ctx context.Context, bounds Bounds, opts ...FindOption) (*Cursor, error)
func (b *Bucket) First(ctx context.Context) (Entry, error)                                              // ErrEmpty
func (b *Bucket) Last(ctx context.Context) (Entry, error)

// blob
func (b *Bucket) PutBlob(ctx context.Context, key string, data []byte, opts ...BlobOption) (BlobReceipt, error) // WithMetadata(doc); bulk lane
func (b *Bucket) GetBlob(ctx context.Context, key string) (*Blob, error)                               // ErrNotFound

// every type that answers them
func (b *Bucket) Find(ctx context.Context, filter any, opts ...FindOption) (*Cursor, error)            // SCAN; bulk lane
func (b *Bucket) Count(ctx context.Context, filter any) (int64, error)
func (b *Bucket) Distinct(ctx context.Context, field string, filter any) ([]Value, error)
func (b *Bucket) Search(ctx context.Context, filter any, opts ...FindOption) (*Cursor, error)          // indexed; hash buckets
func (b *Bucket) Stats(ctx context.Context) (*BucketStats, error)

func WithWriteConcern(wc WriteConcern) WriteOption   // overrides the client default for one call; any write
func WithPriority(p int64) WriteOption               // heap Push only; on any other verb an ErrInvalidArgument, not a silent drop
func Consistent() ReadOption                         // Get through CONSISTENT_GET; needs consistent_get
func Limit(n int) FindOption
func Skip(n int) FindOption
func SortBy(field string, dir Direction) FindOption  // one field; Ascending, Descending
func Projection(fields ...string) FindOption
func PageSize(n int) FindOption                      // default 100: the size of one SCAN

type Update struct {                                 // all nine operators are documented (operations.md) and pinned by the shared wire vectors
    Set      Map
    Unset    []string
    Inc      map[string]int64
    Push, AddToSet, Min, Max Map
    Mul      map[string]float64
    Rename   map[string]string
}
```

- `Pop` takes `WriteOption`s because a pop consumes: it is a write, it carries a write concern, and it
  is **never retried**.
- `Delete` returns `false, nil` for a key that was not there (the server's `success: false`,
  `not_applied`); repeating a delete is safe.
- `ErrEmpty` wraps `ErrNotFound`: `errors.Is(err, hoarddb.ErrNotFound)` catches both, and a caller can
  still tell an empty queue from a missing key.
- `Get` with `Consistent()` reads every copy and is the read-your-writes verb on a cluster.

### 5.1 The cursor

```go
type Cursor struct{ /* unexported */ }
func (c *Cursor) Next(ctx context.Context) bool      // fetches the next page when the current one is spent
func (c *Cursor) Document() Document                 // valid until the next call to Next
func (c *Cursor) Err() error
func (c *Cursor) Close() error                       // releases nothing on the server today
func (c *Cursor) Total() int64                       // from the first page: for Find the exact match count; for Search the number returned (see has_more in operations.md)
func (c *Cursor) All(ctx context.Context) ([]Document, error)
func (c *Cursor) Documents(ctx context.Context) iter.Seq2[Document, error]
```

The godoc says plainly: **a cursor is not a snapshot** (`operations.md` section 3). On
`RESULT_TOO_LARGE` the cursor halves its page size and retries, as often as needed (at most
log2(`PageSize`)+1 attempts per page, down to 1 document). A page size that worked is kept for later
pages, and a later page may halve again. If a single document still exceeds the ceiling, the cursor
returns the error with both numbers.

## 6. Store fields

```go
func (b *Bucket) Field(key, field string) *FieldStore         // document key, field name; no I/O
func (f *FieldStore) Field(element, field string) *FieldStore // a store nested inside an element (the wire's path)

func (f *FieldStore) Push(ctx context.Context, doc any, opts ...WriteOption) (Receipt, error)   // fifo, lifo, heap
func (f *FieldStore) Pop(ctx context.Context, opts ...WriteOption) (Document, error)            // ErrEmpty
func (f *FieldStore) Peek(ctx context.Context, opts ...PeekOption) ([]Document, error)          // ErrEmpty; WithLimit(n), as for a bucket
func (f *FieldStore) Put(ctx context.Context, element string, doc any, opts ...WriteOption) (Receipt, error) // hash, btree
func (f *FieldStore) Get(ctx context.Context, element string) (Document, error)                 // ErrNotFound
func (f *FieldStore) Delete(ctx context.Context, element string, opts ...WriteOption) (bool, error)
func (f *FieldStore) PutBlob(ctx context.Context, element string, data []byte, opts ...BlobOption) (BlobReceipt, error)
func (f *FieldStore) GetBlob(ctx context.Context, element string) (*Blob, error)
func (f *FieldStore) Len(ctx context.Context) (int64, error)
func (f *FieldStore) List(ctx context.Context, opts ...FindOption) (*Cursor, error)             // FIELD_LIST pages; PageSize up to 1,000
```

The verbs and their meanings are the bucket's (rule 7): `users.Field("ada", "Purchases").Push(ctx,
order)` pushes onto Ada's own purchase queue exactly as `queue.Push(ctx, order)` pushes onto a fifo
bucket. A nested store is reached through the element that holds it: `users.Field("ada",
"Recipes").Field("pie", "Steps")`. An undeclared field or a missing parent document is `ErrNotFound`.

## 7. Errors

`*hoarddb.Error` is the one error type every call returns (it exists; this adds the write outcome):

```go
type Error struct {
    Code    Code         // a wire code 0x01..0x10, or a driver code from 0x100
    Op      string       // "put", "find", "connect", ...
    Target  string       // "app/users/ada", or a host for a connection error
    Message string       // the server's sentence verbatim, or the driver's with its fix; never a secret
    Reason  AuthReason   // for ErrAuthFailed
    Outcome WriteOutcome // OutcomeNotReported, OutcomeApplied, OutcomeNotApplied, OutcomeUnknown
    CopiesConfirmed, ReplicationFactor int
    MaxResultBytes, RetainedBytes      int64 // for ErrResultTooLarge
    Err     error        // the cause: a network error, context.DeadlineExceeded
}
```

Sentinels, matched with `errors.Is`: `ErrNotFound`, `ErrEmpty`, `ErrAlreadyExists`,
`ErrPermissionDenied`, `ErrInvalidArgument`, `ErrInternal`, `ErrUnavailable`, `ErrTimeout`, `ErrConflict`,
`ErrQuotaExceeded`, `ErrUnsupportedVersion`, `ErrNotAuthenticated`, `ErrOverloaded`, `ErrResultTooLarge`,
`ErrDuplicateKey`, `ErrFieldType`; the driver's own: `ErrAuthFailed`, `ErrUntrustedServer`,
`ErrServerKeyChanged`, `ErrFingerprintMismatch`, `ErrInvalidURI`, `ErrUnsupportedByServer`,
`ErrClosed`, `ErrUnreachable`. An unknown wire code keeps its number and matches `ErrInternal`.
`errors.Is(err, context.DeadlineExceeded)` works when the caller's deadline was the cause; the server's
own `TIMEOUT` is `ErrTimeout`, a different thing.

In-band refusals (`errors.md` section 3) become the same `*Error`, classed by the vector-pinned
message, else `ErrInvalidArgument`; a miss on `Get` is `ErrNotFound`, on `Pop`/`Peek`/`First`/`Last`
`ErrEmpty`.

**Retries** follow `errors.md` section 6 exactly. In Go terms: `Get`, `Exists`, `Find` and `Search`
pages, `Count`, `Distinct`, `Range`, `First`, `Last`, `Peek`, `Stats`, `Info`, `Len`, `List`, the list calls,
`Ping` and `Health` are retried once on a fresh connection after a connection failure; `UNAVAILABLE`
on a read, and any refusal with `OutcomeNotApplied`, is retried with backoff within the caller's
deadline; nothing else is retried, and never `Put`, `Insert`, `InsertMany`, `Push`, `Pop`, `Update`,
`Delete`, `PutBlob` or a login on an unknown outcome. A cancelled or timed-out write returns
`OutcomeUnknown`, and its message says not to resend a push or an increment.

## 8. Concurrency and the pool

A `*Client` is safe for concurrent use. Two lanes per host (point 4, bulk 2), dialled lazily; a call
that finds its lane busy waits for its `ctx`. One request in flight per connection in v1; each
connection is a request-ID demultiplexer with one reader goroutine, so the pool notices a closed
connection before it is used. Idle connections are closed after 120 s, or kept alive with `PING` at
60 s. Cancelling a call closes the connection it was using (the server cannot be told to stop), and the
pool redials. Dial backoff is 100 ms doubling to 5 s with full jitter, per host; `OVERLOADED` backs off
that dial only; a trust failure is not retried. `Close` waits for every goroutine the client started.

## 9. Not in v1

`GET_FAST` as an API (the driver may use it inside `Get`); caller-visible pipelining (waits for the
server's `query_pipelining`); user administration, backup and restore, and cluster operations (the
unstable `hoarddbwire` layer can send them; the public API does not); routing by the ring (a driver
talks to any node); an ORM or a query builder.

## 10. `hoarddbwire`: the unstable layer

`github.com/hoarddb/hoarddb-go/hoarddbwire` holds the frame codec, the opcode and error tables, the
trust sub-package, `Conn` (`Dial`, `Do`, `RoundTrip`) and **one request and one response type per
opcode, with an explicit `bson` tag on every field**, equal to the wire names in `operations.md`. It
carries **no stability promise** except the frame layout and the numeric opcode and error values; its
godoc says so on its first line. A test fails if any exported identifier of the public package mentions
a `hoarddbwire` type or a BSON-library type.
