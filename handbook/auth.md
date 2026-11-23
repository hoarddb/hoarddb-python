# Authentication

The first frame on every connection. Language-neutral; binds every driver.

## 1. The exchange

After TLS, the driver sends `AUTH` (`0x01`) with this BSON payload:

| Field | Type | Meaning |
|---|---|---|
| `username` | string | the user |
| `password` | string | the password; ignored when `token` is non-empty |
| `protocol_version` | integer | `1`, the protocol major (the ALPN `hoarddb/1`) |
| `capabilities` | array of strings | what the client supports; send `[]` today. **Never send `pipeline_window=<n>`**: it was never implemented. Unknown strings are ignored by the server |
| `client` | string | the driver's name and version, for the server's logs: `hoarddb-go/<version>`, `hoarddb-node/<version>`; a program may append its own name (the URI's `clientName`) |
| `token` | string, optional | a resumption token (section 3); when present, the password in the same frame is ignored |

**Success** is `AUTH_OK` (`0x02`):

| Field | Type | Meaning |
|---|---|---|
| `success` | bool | `true` |
| `token` | string | a resumption token, when the server has the `auth_token` capability |
| `message` | string | human text |
| `token_expires_at` | integer, Unix seconds | when this token expires |
| `token_deadline` | integer, Unix seconds | when the chain of refreshed tokens ends; never extended by a refresh |
| `protocol_version` | integer | the server's protocol major |
| `server_version` | string | the server's version |
| `capabilities` | array of strings | `wire.md` section 6 |
| `password_min_length` | integer | the server's shortest accepted new password, in characters (section 5) |

**Failure** is `AUTH_FAIL` (`0x03`) with `{reason (integer), message (string, optional)}`. An **empty**
`AUTH_FAIL` payload, which older servers send, is read as `CREDENTIALS`; zero is not a valid reason.

| `reason` | Name | What the driver does |
|---|---|---|
| 1 | `CREDENTIALS` | stop. Never retry the same password. The error says which user and that the driver does not retry |
| 2 | `TOKEN_EXPIRED` | fall back to the password, **once**, on a **new** connection |
| 3 | `TOKEN_INVALID` | same as 2 |
| 4 | `TOKEN_UNSUPPORTED` | the server has no tokens: discard tokens and use the password |
| 5 | `PROTOCOL_MISMATCH` | stop; the message names both versions; the error names the upgrade to do |
| 6 | `RATE_LIMITED` | back off; **no credential was checked**; do not loop (section 4) |
| 7 | `ALREADY_AUTHED` | a second password AUTH on a connection that used its one (section 2): a driver bug, or a retry on the same connection |
| 8 | `INTERNAL` | a server-side failure, or a node that has not yet received its user configuration, or a cluster follower whose copy of it is too old. Not a credential failure: retryable on another node or after a short wait |

The message is passed to the caller verbatim; the driver adds what happened and what to do, and never
a password or token.

## 2. One password attempt per connection

The server claims a connection's single password AUTH **before** it checks the password, so a failed
password AUTH spends it: a second password AUTH on the same connection is refused `ALREADY_AUTHED`.
**After any failed password AUTH, a driver closes the connection and dials a new one** if it tries
again at all. The one exception is the busy refusal that checked no credential (`RATE_LIMITED` issued
by the server's derivation gate), which gives the claim back.

A token AUTH is exempt: it may be sent on an already authenticated connection, which is how a
connection refreshes its token.

## 3. Tokens

A token is a **resumption credential**: minted by a successful password AUTH, presented only in
`AUTH.token` (never per request), and accepted on another connection to the same server. A token a
node does not accept (for example after failing over to another host) is answered `TOKEN_INVALID`,
which the fallback below handles. Integers here, as everywhere, are decoded at any width (`wire.md` section 3). Two lifetimes:

- `token_expires_at`: the token's own expiry (900 s by default).
- `token_deadline`: the end of the refresh chain (12 hours by default, jittered ±10 % per chain). A
  refresh returns a new token with a new `token_expires_at` and the **same** `token_deadline`.

A driver:

1. keeps the password in memory only as long as it may need it again (it will, at `token_deadline`);
2. after the first password AUTH, presents the **token**, not the password, on every new connection, so
   the server does not repeat a deliberately slow password verification for each connection a pool opens;
3. refreshes at **half** of the token's lifetime, with **one refresh in flight per client**
   (single-flight), by sending a token AUTH on a connection it already holds;
4. when `token_deadline` has passed, or the server answers `TOKEN_EXPIRED` / `TOKEN_INVALID`, falls back
   to the password **once**, staggering the fallback with jitter so a pool of connections does not
   stampede;
5. ignores the token entirely when `capabilities` lacks `auth_token`.

A token is not an API key: it expires and is minted by a password. A driver offers no "API key"
option. A token supplied by the program (the URI's `authToken`, or an option) is used for the first
connection and refreshed like any other.

## 4. The failed-login limiter

The server counts consecutive authentication failures **per source address**:

- from the 5th failure, the next attempt is delayed 1 s, doubling per failure to a cap of 30 s;
- at 20 failures (operator-tunable; 0 disables the limiter) the address is refused outright for 60 s with
  `RATE_LIMITED`, and the connection is closed;
- the count is forgotten after 15 minutes without a failure, and cleared by a successful password AUTH;
- wrong passwords, unknown users and rejected tokens count; a `RATE_LIMITED` refusal does not.

**A driver never retries a failed AUTH in a loop**, and never retries `CREDENTIALS`, `RATE_LIMITED` or
`PROTOCOL_MISMATCH` at all. A failed login in a loop is the credential-stuffing pattern the limiter
exists to stop, and it locks out every other process on the same machine. For the same reason, a test
suite that exercises failed logins runs them against **its own server**, never one shared with other
tests.

## 5. Users and roles

Every user holds grants of `{role, database}`. Roles: `reader`, `writer`, `db-manager`, `user-manager`,
`cluster-manager`, `backup-operator`, `restore-operator`, `root`. Every request is checked against the user's grants before
it runs; a refusal is `PERMISSION_DENIED` (`0x03`) with a message of the form `permission denied: user
"<user>" lacks "<action>" on "<resource>"`, followed by the user's roles and the `grant role …`
command an administrator would run. A driver's documentation says a refused call is a **role** problem,
never that every user is an administrator.

The root user exists from the first start: `admin` by default (`HOARDDB_ROOT_USER`), with the password
from `HOARDDB_ROOT_PASSWORD` (or the file named by `HOARDDB_ROOT_PASSWORD_FILE`); when neither is set on
a first start from a terminal the server asks for one, and a first start with no terminal refuses,
writes nothing and says to set one. Only the password's hash is stored; the password is never written to
disk, so a driver's tests supply `HOARDDB_ROOT_PASSWORD` rather than read it back.

When a user is dropped or disabled, the server closes that user's live connections (`wire.md`
section 8).

A driver that offers user administration (`USER_CREATE`, and `USER_ALTER` with new credentials)
derives the Argon2id credential itself and sends only that, so the server never sees the new password
and cannot measure it. The driver applies the policy: before deriving, it refuses a password shorter
than `AUTH_OK`'s `password_min_length`, counted in Unicode characters, with an error that names the
length, the minimum and `HOARDDB_PASSWORD_MIN_LENGTH`, and never the password. It uses the number from
the server it is connected to, never a constant of its own; absent or zero means the server reported
none, and the driver refuses rather than guess.
