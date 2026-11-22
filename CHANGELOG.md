# Changelog

User-visible changes before 1.0, newest first. Before 1.0 a change is made directly, with no alias
and no deprecation.

## Unreleased

- A per-call `timeout=` or `request_timeout=` of zero is now refused locally, as is a negative,
  non-finite or over-an-hour value. `timeout=0` raises `InvalidArgumentError`; `request_timeout=0`
  raises `ConfigError` (commit `34df0c5`).
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
