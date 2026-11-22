# SPDX-License-Identifier: Apache-2.0
"""The connection URI (trust.md section 6): a hand-written parser, because no URL library takes a
comma-separated host list or can produce the named refusals.

    hoarddb://[user[:password]@]host[:port][,host[:port]...][/database[/bucket]][?params]

Every refusal is a :class:`~hoarddb.errors.ConfigError` (``INVALID_URI``) whose message says which
part is wrong and what shape is expected. None ever contains the password: messages quote the
offending part only when it is not a credential.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Final

from hoarddb import errors
from hoarddb._secret import Secret
from hoarddb._trust import fingerprint

DEFAULT_PORT: Final = 7433
DEFAULT_DIAL_TIMEOUT_MS: Final = 10000
MAX_DIAL_TIMEOUT_MS: Final = 3_600_000  # an hour; more is a typo, and 30 nines is 1e27 seconds
SCHEME: Final = "hoarddb"

_ACCEPTED: Final = ("fingerprint", "authToken", "clientName", "dialTimeoutMs")
_REFUSED: Final = {
    "tls": "TLS is always on; there is no parameter for it",
    "ssl": "TLS is always on; there is no parameter for it",
    "insecure": (
        "switching verification off is never URI text, because a URI is copied between "
        'environments; pass TlsOptions(insecure_reason="...") in code (the command-line tool '
        "spells it --insecure)"
    ),
    "sslverify": (
        "verification is never switched off by URI text; pin the server's key with "
        "fingerprint= instead"
    ),
    "srv": "DNS SRV lookups are not supported; list the hosts instead",
    "replicaset": (
        "there is no replica set to name; list the nodes as comma-separated hosts, tried in order"
    ),
    "readpreference": (
        "reads go to any node; to read your own write use get(key, consistent=True)"
    ),
    "w": 'durability is an option in code, never URI text: pass write_concern="majority"',
    "journal": 'durability is an option in code, never URI text: pass write_concern="all"',
    "authsource": "users are not scoped to an authentication database; give the user and password",
    "authmechanism": "there is one mechanism, a user and password (or a token); nothing to choose",
    "retrywrites": (
        "writes are never retried by the driver, because the wire has no idempotency key"
    ),
    "directconnection": "hosts are tried in the written order; there is no topology discovery",
}
_HOST_CHARS: Final = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~")
_HEXDIGITS: Final = frozenset("0123456789abcdefABCDEF")


@dataclass(frozen=True, slots=True)
class HostPort:
    host: str
    port: int

    def __str__(self) -> str:
        return f"[{self.host}]:{self.port}" if ":" in self.host else f"{self.host}:{self.port}"


@dataclass(frozen=True, slots=True)
class ParsedUri:
    hosts: tuple[HostPort, ...]
    username: str | None
    password: Secret | None
    database: str | None
    bucket: str | None
    fingerprints: tuple[str, ...]
    auth_token: Secret | None
    client_name: str | None
    dial_timeout_ms: int

    def pin_for(self, index: int) -> str | None:
        return fingerprint.pin_for(self.fingerprints, index)


def _bad(message: str) -> errors.ConfigError:
    return errors.ConfigError(message, code="INVALID_URI", operation="connect")


def _unquote(text: str, what: str) -> str:
    if "%" not in text:
        return text
    out = bytearray()
    i = 0
    while i < len(text):
        char = text[i]
        if char == "%":
            pair = text[i + 1 : i + 3]
            if len(pair) != 2 or not set(pair) <= _HEXDIGITS:
                raise _bad(f"the {what} has a bad percent escape; write %XX with two hex digits")
            out.append(int(pair, 16))
            i += 3
        else:
            out.extend(char.encode("utf-8"))
            i += 1
    # The refusal is raised outside the except block: inside it, the UnicodeDecodeError (which
    # holds the raw bytes of the password or token) would stay on the new error as __context__.
    decoded: str | None
    try:
        decoded = out.decode("utf-8")
    except UnicodeDecodeError:
        decoded = None
    if decoded is None:
        raise _bad(f"the {what} is not valid UTF-8 after percent-decoding")
    return decoded


def _parse_port(text: str, host: str) -> int:
    if not text.isascii() or not text.isdigit():
        raise _bad(f'the port for host "{host}" is not a number; use 1-65535')
    port = int(text)
    if not 1 <= port <= 65535:
        raise _bad(f'the port {port} for host "{host}" is outside 1-65535')
    return port


def _bad_host_char(host: str) -> bool:
    """True when ``host`` holds a character a registered name cannot: anything but letters,
    digits and ``-._~``, or a non-ASCII character that is not printable text (control characters,
    whitespace, separators)."""
    return any(
        c not in _HOST_CHARS and (c.isascii() or not c.isprintable() or c.isspace()) for c in host
    )


def _zone(raw: str) -> str:
    """The text after ``%25`` in an IPv6 zone (RFC 6874), percent-decoded and checked."""
    zone = _unquote(raw, "IPv6 zone")
    if not zone or _bad_host_char(zone) or set(zone) & set(":@[]%"):
        raise _bad(
            "the IPv6 zone after %25 is empty or holds a character an interface name cannot; "
            "write [fe80::1%25eth0]"
        )
    return zone


def _ipv6_literal(literal: str) -> str:
    """The checked IPv6 address of a bracketed host, with a zone ``%25name`` decoded to
    ``%name`` (what the resolver takes)."""
    address, percent, zone_raw = literal.partition("%")
    try:
        ipaddress.IPv6Address(address)
    except ValueError:
        raise _bad(f'"[{address}]" is not an IPv6 address') from None
    if not percent:
        return address
    if not zone_raw.startswith("25"):
        raise _bad(
            "an IPv6 zone is written %25 then the interface name, for example [fe80::1%25eth0]; "
            'a bare "%" would be read as a percent escape'
        )
    return f"{address}%{_zone(zone_raw[2:])}"


def check_host_name(host: str) -> None:
    """Refuse a host (already percent-decoded, or given in code) that cannot be a registered name
    or an IPv6 address; the message never carries more than the offending host's role."""
    if not host:
        raise _bad("an empty host in the host list; give a host name or address")
    if ":" in host:
        _ipv6_literal_unbracketed(host)
    elif _bad_host_char(host):
        raise _bad(
            "a host holds a character a host name cannot: use letters, digits, '-', '.', "
            "'_' or '~' (a percent-escape that decodes to a space, control character or one of "
            "'/', ':', '@' is refused)"
        )


def _ipv6_literal_unbracketed(host: str) -> None:
    address, percent, zone = host.partition("%")
    try:
        ipaddress.IPv6Address(address)
    except ValueError:
        raise _bad(
            "a host holds ':' but is not an IPv6 address; give a host name, or an IPv6 address"
        ) from None
    if percent and (not zone or _bad_host_char(zone) or set(zone) & set(":@[]%")):
        raise _bad("the IPv6 zone is empty or holds a character an interface name cannot")


def parse_host_port(text: str) -> HostPort:
    """``host``, ``host:port`` or ``[ipv6]:port``; the port defaults to 7433."""
    if not text:
        raise _bad("an empty host in the host list; give a host name or address")
    if text.startswith("["):
        close = text.find("]")
        if close == -1:
            raise _bad(f'the IPv6 host "{text}" has no closing "]"')
        literal = _ipv6_literal(text[1:close])
        tail = text[close + 1 :]
        if not tail:
            return HostPort(literal, DEFAULT_PORT)
        if not tail.startswith(":"):
            raise _bad(f'unexpected text "{tail}" after the IPv6 host "[{literal}]"')
        return HostPort(literal, _parse_port(tail[1:], literal))
    if text.count(":") > 1:
        raise _bad(
            f'"{text}" looks like an IPv6 address; write it in brackets, for example [::1]:7433'
        )
    host, _, port = text.partition(":")
    if not host:
        raise _bad("an empty host in the host list; give a host name or address")
    if not set(host) <= _HOST_CHARS | {"%"}:
        raise _bad(f'the host "{host}" has characters a host name cannot hold')
    decoded = _unquote(host, "host")
    check_host_name(decoded)
    return HostPort(decoded, _parse_port(port, decoded) if ":" in text else DEFAULT_PORT)


_ESCAPES: Final = {"/": "%2F", "?": "%3F"}


def _first_delimiter(rest: str) -> int:
    """Index of the first ``/`` or ``?`` in ``rest``, or -1."""
    return next((i for i, char in enumerate(rest) if char in "/?"), -1)


def _delimiter_before_at(rest: str) -> str | None:
    """The ``/`` or ``?`` that precedes an ``@`` (so the ``@`` cannot end a user-info), or None.

    A password that holds an unencoded ``/`` or ``?`` would otherwise end the authority early and
    be read as a port, a host or a path. Whatever precedes the ``@`` is never quoted.
    """
    first = _first_delimiter(rest)
    if first != -1 and "@" in rest[first:]:
        return rest[first]
    return None


def _split_authority(rest: str) -> tuple[str, str]:
    for i, char in enumerate(rest):
        if char in "/?":
            return rest[:i], rest[i:]
    return rest, ""


def parse_uri(text: str) -> ParsedUri:
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in text):
        raise _bad("the URI contains a control character; remove it")
    scheme, sep, rest = text.partition("://")
    if not sep:
        raise _bad(
            'a hoardDB URI starts with "hoarddb://", for example hoarddb://localhost:7433/app'
        )
    if scheme.lower() == "hoarddb+srv":
        raise _bad("DNS SRV lookups are not supported; list the hosts instead: hoarddb://a,b,c")
    if scheme.lower() != SCHEME:
        raise _bad(f'scheme "{scheme}" is not a hoardDB scheme; use hoarddb://')
    if "#" in rest:
        raise _bad("a fragment (#...) is not part of the URI grammar; encode a literal # as %23")

    delimiter = _delimiter_before_at(rest)
    if delimiter is not None:
        raise _bad(
            f"the password contains an unencoded '{delimiter}'; percent-encode it as "
            f"{_ESCAPES[delimiter]} (any other '@' after the first '/' or '?' must be written %40)"
        )
    authority, tail = _split_authority(rest)
    path, _, query = tail.partition("?")

    username: str | None = None
    password: Secret | None = None
    hostlist = authority
    if "@" in authority:
        if authority.count("@") > 1:
            raise _bad("the password contains an unencoded '@'; percent-encode it as %40")
        userinfo, _, hostlist = authority.partition("@")
        user_text, has_password, password_text = userinfo.partition(":")
        if not user_text:
            raise _bad("the user name is empty; write user:password@host or leave the user out")
        if ":" in password_text:
            raise _bad("the password contains an unencoded ':'; percent-encode it as %3A")
        username = _unquote(user_text, "user name")
        if has_password:
            password = Secret(_unquote(password_text, "password"))

    hosts = tuple(parse_host_port(entry) for entry in hostlist.split(","))

    database: str | None = None
    bucket: str | None = None
    if path:
        segments = path[1:].split("/")
        if len(segments) > 2:
            raise _bad("the path holds more than /database/bucket; remove the extra segment")
        if segments[0]:
            database = _unquote(segments[0], "database name")
        if len(segments) == 2:
            if not segments[1]:
                raise _bad("the path ends in '/' with no bucket name; write /database/bucket")
            if database is None:
                raise _bad("the path names a bucket but no database; write /database/bucket")
            bucket = _unquote(segments[1], "bucket name")

    fingerprints: list[str] = []
    seen: set[str] = set()
    token: Secret | None = None
    client_name: str | None = None
    dial_ms = DEFAULT_DIAL_TIMEOUT_MS
    for param in query.split("&") if query else []:
        name, eq, raw = param.partition("=")
        if not eq or not name:
            raise _bad(
                "a query parameter is not written name=value (its text is not quoted here in "
                "case it is a secret); write each as name=value, separated by &"
            )
        key = name.lower()
        if key in _REFUSED:
            raise _bad(f'the parameter "{name}" is not supported: {_REFUSED[key]}')
        canonical = next((a for a in _ACCEPTED if a.lower() == key), None)
        if canonical is None:
            raise _bad(
                f'unknown parameter "{name}"; the accepted parameters are {", ".join(_ACCEPTED)}'
            )
        value = _unquote(raw, f'value of "{canonical}"')
        if canonical == "fingerprint":
            try:
                fingerprints.append(fingerprint.canonicalise(value))
            except fingerprint.FingerprintError as bad:
                raise _bad(str(bad)) from None
            continue
        if canonical in seen:
            raise _bad(f'the parameter "{canonical}" is given more than once')
        seen.add(canonical)
        if canonical == "authToken":
            token = Secret(value)
        elif canonical == "clientName":
            client_name = value
        else:
            if not value.isascii() or not value.isdigit() or not value.strip("0"):
                raise _bad("dialTimeoutMs must be a positive whole number of milliseconds")
            significant = value.lstrip("0")
            # Length first: int() of thousands of digits raises ValueError (and costs time).
            if (
                len(significant) > len(str(MAX_DIAL_TIMEOUT_MS))
                or int(significant) > MAX_DIAL_TIMEOUT_MS
            ):
                raise _bad(
                    f"dialTimeoutMs is too large; the most accepted is {MAX_DIAL_TIMEOUT_MS} "
                    "milliseconds (one hour)"
                )
            dial_ms = int(significant)

    if len(fingerprints) > 1 and len(fingerprints) != len(hosts):
        raise _bad(
            f"{len(fingerprints)} fingerprints for {len(hosts)} hosts; give one fingerprint "
            "for every host, or one per host in order"
        )
    return ParsedUri(
        hosts,
        username,
        password,
        database,
        bucket,
        tuple(fingerprints),
        token,
        client_name,
        dial_ms,
    )


def redact(text: str) -> str:
    """The URI with the password and the ``authToken`` value replaced by ``***``.

    Tolerant on purpose: it is used on error paths, so it never raises and never needs the URI to
    be valid. Everything else is left as written.
    """
    head, sep, rest = text.partition("://")
    if not sep:
        return text
    if _delimiter_before_at(rest) is not None and ":" in rest[: rest.rfind("@")]:
        # A '/' or '?' inside the password: everything up to the last '@' is user-info.
        at = rest.rfind("@")
        rest = rest[:at].partition(":")[0] + ":***" + rest[at:]
    authority, tail = _split_authority(rest)
    if "@" in authority:
        at = authority.rfind("@")
        userinfo, host = authority[:at], authority[at:]
        if ":" in userinfo:
            userinfo = userinfo.partition(":")[0] + ":***"
        authority = userinfo + host
    path, qsep, query = tail.partition("?")
    if qsep:
        params: list[str] = []
        for param in query.split("&"):
            if param[:9].lower() == "authtoken":  # also masks a misspelt or "=-less" token
                params.append(f"{param[:9]}=***")
            else:
                params.append(param)
        query = "&".join(params)
    return f"{head}://{authority}{path}{qsep}{query}"
