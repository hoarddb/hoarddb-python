# SPDX-License-Identifier: Apache-2.0
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false, reportUnnecessaryIsInstance=false
# This module's job is to validate untyped runtime input (a caller may pass anything, whatever the
# annotations say), so the checks below are not "unnecessary" and the values are not known types.
"""Resolving ``connect()`` arguments into one private, immutable object (spec 4.2).

``ConnectOptions`` is a ``TypedDict``, a plain ``dict`` at runtime, so its ``repr`` would print the
caller's password and the driver cannot change that. So the caller's dict is read once here and
never kept: the secrets move into :class:`~hoarddb._secret.Secret` and everything else into a
frozen ``__slots__`` dataclass. Nothing in this module performs I/O; the environment is passed in.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, TypeVar

from hoarddb import errors
from hoarddb._core import uri as uri_mod
from hoarddb._core.uri import HostPort
from hoarddb._core.validate import MAX_TIMEOUT_SECONDS, check_write_concern
from hoarddb._secret import Secret
from hoarddb._trust import fingerprint, policy
from hoarddb._version import __version__
from hoarddb.options import ConnectOptions, PoolOptions, TlsOptions

DEFAULT_URI: Final = "hoarddb://localhost"
DEFAULT_USER: Final = "admin"
DEFAULT_DIAL_TIMEOUT: Final = 10.0
DEFAULT_REQUEST_TIMEOUT: Final = 30.0

_T = TypeVar("_T")
_CONNECT_KEYS: Final = tuple(ConnectOptions.__annotations__)


@dataclass(frozen=True, slots=True)
class ResolvedOptions:
    hosts: tuple[HostPort, ...]
    username: str
    password: Secret | None
    auth_token: Secret | None
    database: str | None
    bucket: str | None
    tls: TlsOptions
    pool: PoolOptions
    dial_timeout: float
    request_timeout: float
    write_concern: Literal["one", "majority", "all"] | None
    client_name: str | None
    logger: logging.Logger
    pins: tuple[str, ...]  # canonical; one applies to every host, else one per host in order
    display_uri: str  # the URI the options came from, password and authToken redacted

    def pin_for(self, index: int) -> str | None:
        return fingerprint.pin_for(self.pins, index)

    @property
    def client_string(self) -> str:
        base = f"hoarddb-python/{__version__}"
        return f"{base} ({self.client_name})" if self.client_name else base


def _bad(message: str, code: str = "INVALID_OPTIONS") -> errors.ConfigError:
    return errors.ConfigError(message, code=code, operation="connect")


def _text(name: str, value: Any) -> str | None:
    """A string option; an empty string counts as not given."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise _bad(f"{name}= must be a string, got {type(value).__name__}")
    return value or None


def _positive(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not value > 0:
        raise _bad(f"{name}= must be a positive number of seconds, got {value!r}")
    try:
        return float(value)
    except OverflowError:
        raise _bad(f"{name}= is too large to be a number of seconds") from None


def _dial_timeout(value: Any) -> float:
    seconds = _positive("dial_timeout", value)
    limit = uri_mod.MAX_DIAL_TIMEOUT_MS / 1000
    if seconds > limit:
        raise _bad(f"dial_timeout= is too large; the most accepted is {limit:g} seconds (one hour)")
    return seconds


def _request_timeout(value: Any) -> float:
    seconds = _positive("request_timeout", value)
    if seconds > MAX_TIMEOUT_SECONDS:
        raise _bad(
            f"request_timeout= is too large; the most accepted is {MAX_TIMEOUT_SECONDS:g} seconds "
            "(one hour)"
        )
    return seconds


def _convert(cls: type[_T], value: Any, name: str) -> _T:
    fields = [f.name for f in dataclasses.fields(cls)]  # type: ignore[arg-type]
    if isinstance(value, cls):
        return value
    if not isinstance(value, Mapping):
        raise _bad(f"{name}= must be hoarddb.{cls.__name__} or a dict, got {type(value).__name__}")
    for key in value:
        if key not in fields:
            raise _bad(
                f'Unknown option "{key}" in {name}=; the accepted options are {", ".join(fields)}'
            )
    return cls(**value)


def _hosts(value: Any) -> tuple[HostPort, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence) or not value:
        raise _bad("hosts= must be a non-empty list of 'host', 'host:port' or (host, port)")
    out: list[HostPort] = []
    for item in value:
        if isinstance(item, str):
            try:
                out.append(uri_mod.parse_host_port(item))
            except errors.ConfigError as bad:
                raise _bad(bad.message) from None
        elif isinstance(item, tuple) and len(item) == 2:
            host, port = item
            if not isinstance(host, str) or not host:
                raise _bad(f"hosts= entry {item!r} needs a host name as its first item")
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise _bad(f"hosts= entry {item!r} needs a port in 1-65535")
            try:
                uri_mod.check_host_name(host)
            except errors.ConfigError as bad:
                raise _bad(f"hosts= entry {item!r}: {bad.message}") from None
            out.append(HostPort(host, port))
        else:
            raise _bad(f"hosts= entry {item!r} must be 'host', 'host:port' or (host, port)")
    return tuple(out)


def _canonical_pins(values: Any) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, Sequence):
        raise _bad("tls fingerprints must be a list of 'SHA256:<64 hex>' strings")
    out: list[str] = []
    for value in values:
        try:
            out.append(fingerprint.canonicalise(value))
        except fingerprint.FingerprintError as bad:
            raise _bad(str(bad)) from None
        except TypeError:
            raise _bad("tls fingerprints must be a list of 'SHA256:<64 hex>' strings") from None
    return tuple(out)


def resolve_options(
    uri: str | None, options: Mapping[str, Any], env: Mapping[str, str]
) -> ResolvedOptions:
    for key in options:
        if key not in _CONNECT_KEYS:
            raise _bad(
                f'Unknown option "{key}" in connect(); the accepted options are '
                f"{', '.join(_CONNECT_KEYS)}"
            )

    text = uri if uri is not None else (env.get("HOARDDB_URI") or DEFAULT_URI)
    parsed = uri_mod.parse_uri(text)

    tls = _convert(TlsOptions, options.get("tls", TlsOptions()), "tls")
    pool = _convert(PoolOptions, options.get("pool", PoolOptions()), "pool")
    policy.refuse_unremembered_first_sight(tls)
    callback: object = tls.on_unknown_server
    if callback is not None and not callable(callback):
        raise _bad(
            f"tls on_unknown_server must be a function taking a ServerKey and returning True to "
            f"trust the key, but got a {type(callback).__name__} that is not callable"
        )
    for field in ("size", "bulk_size"):
        value: object = getattr(pool, field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise _bad(f"pool {field} must be a whole number of at least 1, got {value!r}")
    waiters: object = pool.max_waiters
    if isinstance(waiters, bool) or not isinstance(waiters, int) or waiters < 0:
        raise _bad(f"pool max_waiters must be a whole number, 0 or more, got {waiters!r}")
    _positive("pool idle_timeout", pool.idle_timeout)

    option_pins = _canonical_pins(tls.fingerprints)
    uri_pins = parsed.fingerprints
    reason: object = tls.insecure_reason
    if reason is not None:
        if not isinstance(reason, str) or not reason.strip():
            raise _bad(
                "insecure_reason must say why verification is off; it is logged on every dial"
            )
        if option_pins or uri_pins:
            raise _bad(
                "this client was given both a pin and insecure_reason; the pin would be ignored. "
                "Remove one."
            )

    hosts = _hosts(options["hosts"]) if "hosts" in options else parsed.hosts
    if "hosts" in options and len(uri_pins) > 1 and not option_pins:
        raise _bad(
            f"hosts= replaces the URI's host list, but the URI carries {len(uri_pins)} "
            "fingerprints that pair with its hosts by position. Give the pins in "
            "TlsOptions.fingerprints instead."
        )
    pins = option_pins or uri_pins
    if len(pins) > 1 and len(pins) != len(hosts):
        raise _bad(
            f"{len(pins)} fingerprints for {len(hosts)} hosts; give one fingerprint for every "
            "host, or one per host in order"
        )

    if "username" in options and not options["username"]:
        raise _bad(
            "username= must not be empty; leave it out to use the URI, the environment or admin"
        )
    username = (
        _text("username", options.get("username"))
        or parsed.username
        or env.get("HOARDDB_ROOT_USER")
        or DEFAULT_USER
    )
    password_text = _text("password", options.get("password"))
    password = (
        Secret(password_text)
        if password_text
        else (parsed.password if parsed.password else None)
        or (Secret(env["HOARDDB_ROOT_PASSWORD"]) if env.get("HOARDDB_ROOT_PASSWORD") else None)
    )
    token_text = _text("auth_token", options.get("auth_token"))
    auth_token = (
        Secret(token_text) if token_text else (parsed.auth_token if parsed.auth_token else None)
    )
    if password is None and auth_token is None:
        raise errors.ConfigError(
            f'No password was given for user "{username}" on {hosts[0]}. '
            "Set HOARDDB_ROOT_PASSWORD (the server prints it once on first start), "
            "or pass password=...",
            code="NO_PASSWORD",
            operation="connect",
        )

    write_concern = options.get("write_concern")
    if write_concern is not None:
        check_write_concern(write_concern)

    dial = options.get("dial_timeout")
    logger: object = options.get("logger", logging.getLogger("hoarddb"))
    if not isinstance(logger, logging.Logger):
        raise _bad(f"logger= must be a logging.Logger, got {type(logger).__name__}")
    return ResolvedOptions(
        hosts=hosts,
        username=username,
        password=password,
        auth_token=auth_token,
        database=_text("database", options.get("database")) or parsed.database,
        bucket=_text("bucket", options.get("bucket")) or parsed.bucket,
        tls=dataclasses.replace(tls, fingerprints=option_pins),
        pool=pool,
        dial_timeout=_dial_timeout(dial) if dial is not None else parsed.dial_timeout_ms / 1000,
        request_timeout=_request_timeout(options.get("request_timeout", 30.0)),
        write_concern=write_concern,
        client_name=_text("client_name", options.get("client_name")) or parsed.client_name,
        logger=logger,
        pins=pins,
        display_uri=uri_mod.redact(text),
    )
