# Trust and the connection URI

How a driver knows it is talking to the right server before it sends a password, and how a developer
writes down where that server is. Language-neutral; binds every driver.

## 1. What the server presents

Each hoardDB node mints a **self-signed ed25519 certificate** at first start and keeps it in its data
directory (`<data dir>/keys/server.key`, PKCS#8 PEM, and `server.crt`, PEM). There is no certificate
authority, no hostname in the certificate, and no operator-supplied certificate in this release: a
server configured for one refuses to start. So a driver cannot use its platform's CA verification. It
verifies the **key**.

At every start the server logs its key's fingerprint, for example (JSON log, one line):

    {"msg":"TCP driver listener listening","address":"127.0.0.1:7433","fingerprint":"SHA256:1e69…90ea", …}

That line is the documented out-of-band source for a pin.

## 2. The fingerprint

**`SHA256:` followed by the 64 lowercase hex characters of SHA-256 over the raw 32-byte ed25519 public
key.** Not over the certificate's DER, and not over the SubjectPublicKeyInfo structure that wraps the
key: over the 32 key bytes alone, which are the last 32 bytes of the key's SPKI DER encoding.

- Parsing a fingerprint a user typed: the prefix and the hex are accepted case-insensitively and
  canonicalised to `SHA256:` + lowercase. Anything else (a bare digest, another prefix, the wrong
  length) is refused with the expected shape named. There is exactly one format; do not accept
  "close enough" forms, because loose normalisation accepts unrelated keys.
- Comparing a presented key with a pin: in constant time, before AUTH.
- **Every driver must produce the identical string for the identical certificate.** The vectors carry
  certificates and keys with their fingerprints (group `trust`), and a conformance leg checks it.

## 3. The trust policy

A driver refuses to connect unless it can authenticate the server by one of these, tried in this order:

1. **A pinned fingerprint** (`?fingerprint=` in the URI, or an option). The production path, and the
   only one that needs no prior state. A mismatch is a hard failure naming **both** fingerprints, never
   a prompt and never a silent re-trust.
2. **The shared trust store, `~/.hoarddb/known_servers`.** The hoardDB command-line tool writes this
   file when a person trusts a server interactively, so a developer who has connected the CLI once has
   already trusted that server for every driver. A driver reads it by default and writes it only through
   option 4. A key that **changed** for a recorded host is a hard failure naming the old and new
   fingerprints, never re-prompted.
3. **A prompt the program supplies** (a callback `(host, fingerprint) -> accept?`). A library has no
   terminal, so **the default is no prompt**: an unknown server is refused with a message giving the
   ways forward (pin it, trust it once with the CLI, or, for local development, option 4).
4. **Accept on first sight**: a ready-made prompt that trusts and records the first key a host shows,
   as SSH does, and logs a warning. Local development only; never a default; never in a first example.
5. **Insecure mode**: verification off. Named, loud, off by default; it **requires a reason string**,
   which is logged at warning level on every connection attempt. It is not reachable by forgetting a
   parameter, and it is **not a URI parameter** (a URI is copied between environments; a flag is typed
   each time).

A driver that sets its TLS library's "skip verification" switch does so only to replace CA-chain
verification with its own key check, and never without that check installed. A unit test constructs
every combination of trust options and asserts the check is present in each.

## 4. The `known_servers` file

- Default path `~/.hoarddb/known_servers`; directory mode `0700`, file mode `0600`.
- One server per line: `host:port <64 hex characters>`, where the hex is the **raw 32-byte ed25519
  public key**, not its fingerprint. Lines starting with `#` and blank lines are skipped; a malformed
  line is skipped.
- **Two different keys for the same `host:port` is an error** when the file is loaded, not a choice.
- A driver refuses a file or directory that other users can write (the error says the `chmod` to run),
  serialises its own writes, and appends atomically, because several processes share the file.
- The fix for a changed key, until the CLI gains a verb for it: delete that host's line from the file.
  (The server's own message for the same situation names a file called `known_hosts`; that file does not
  exist. A driver's message names `known_servers`.)

The vectors carry `known_servers` lines a driver must accept and refuse (group `trust`).

## 5. Platform notes

- **Go:** a TLS config with the platform verification switched off and a peer-certificate callback that
  parses the leaf certificate, requires an ed25519 key, computes the fingerprint and applies the policy.
  Minimum version TLS 1.3; `NextProtos` exactly `["hoarddb/1"]`.
- **Node.js:** `tls.connect` with `ALPNProtocols: ['hoarddb/1']`, `minVersion: 'TLSv1.3'`,
  `rejectUnauthorized: false` **only together with** a check run on `secureConnect` before anything is
  written. The certificate's public key comes from the raw DER (`getPeerCertificate(true).raw`): parse
  it, take the SPKI, take the last 32 bytes. A SHA-256 of the whole certificate, or of the SPKI, is a
  different value and will never match the server's log line. Do this first: it is the one part of a
  Node driver that can cost a day.

## 6. The connection URI

The first line of every example anyone writes, so it is frozen. Grammar (RFC 3986 productions for
`pchar`, `reg-name`, `IPv4address`, `IPv6address`):

```abnf
uri        = "hoarddb://" [ userinfo "@" ] hostlist [ path ] [ "?" params ]
userinfo   = username [ ":" password ]
username   = 1*pchar                  ; "@" ":" "/" "?" "#" MUST be percent-encoded
password   = *pchar                   ; same rule; may be empty
hostlist   = hostport *( "," hostport )
hostport   = host [ ":" port ]
host       = reg-name / IPv4address / "[" IPv6address "]"
port       = 1*5DIGIT                 ; 1-65535
path       = "/" [ database [ "/" bucket ] ]
database   = 1*pchar
bucket     = 1*pchar
params     = param *( "&" param )
param      = name "=" value
```

- **One scheme, `hoarddb`**, matched case-insensitively. `hoarddb+tls://` does not exist (TLS is
  unconditional). `hoarddb+srv://` is refused **by name** ("DNS SRV lookups are not supported"). Any
  other scheme is refused with `scheme "<what was typed>" is not a hoardDB scheme; use hoarddb://`.
- **Default port 7433.** `:4433` parses (a parser does not know the deployment's ports), though it is a
  node's ring port, not a driver port.
- **Hosts** are tried in the order written; the first that completes TLS and AUTH wins. Later hosts are
  a fallback for an unreachable one, not load balancing, and a driver does not choose a host by bucket.
  An empty host (`hoarddb://a,/app`) is an error. An IPv6 literal must be bracketed.
- **Path**: `/database` sets a default database, `/database/bucket` also a default bucket. An empty path
  means no default.
- **Credentials** map onto AUTH's `username` and `password`. A bare `@` (or `:`, `/`, `?`, `#`) in a
  password is refused **by name** ("the password contains an unencoded '@'; percent-encode it as %40").
  An omitted password means "from elsewhere": the driver reads `HOARDDB_ROOT_PASSWORD` (and, for an
  omitted user, `HOARDDB_ROOT_USER`, default `admin`). Every rendering of a URI for a log or an error
  replaces the password with `***`.
- **Parameters**, names matched case-insensitively, all optional:

  | Name | Repeatable | Sets | Default |
  |---|---|---|---|
  | `fingerprint` | yes | the expected server key (section 3) | none |
  | `authToken` | no | AUTH's `token` (`auth.md`) | none |
  | `clientName` | no | AUTH's `client` | none |
  | `dialTimeoutMs` | no | dial + TLS handshake + AUTH deadline per host, milliseconds | 10000 |

  Fingerprints match hosts **positionally**; exactly one fingerprint applies to every host; any other
  count is an error naming both counts. The value `SHA256%3A…` (an encoded colon) is accepted.
- **Refused by name**, each with a sentence saying why it does not exist here: `tls`, `ssl`, `insecure`,
  `sslVerify`, `srv`, `replicaSet`, `readPreference`, `w`, `journal`, `authSource`, `authMechanism`,
  `retryWrites`, `directConnection`. Durability (`w`) and trust bypass (`insecure`) are options in code,
  never URI text.
- **Any other parameter is an error** naming the accepted set. A `#fragment` is refused.
- `HOARDDB_URI` is read when the program passes no URI.

The vectors in `testdata/uri-vectors/v1.json` are **hand-maintained** (a URI never crosses the wire, so
there is nothing to generate them from) and normative: `cases[]` must parse to exactly `expect` with the
`defaults` materialised; `invalid[]` must be refused with a message containing `error_contains`. Tests
every driver carries besides the vector loop: the default port is 7433 asserted independently of the
file; a fingerprint is canonicalised; two fingerprints for three hosts is refused naming both counts;
an unknown parameter is refused naming the accepted set; a rendered URI never shows the password; the
SRV scheme is refused by name.
