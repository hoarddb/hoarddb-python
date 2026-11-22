# SPDX-License-Identifier: Apache-2.0
"""``connect_pinned``: the one place a TLS context exists, and the one place verification is off.

hoardDB servers present a self-signed ed25519 certificate with no hostname in it, so chain
verification is replaced by a key check (trust.md sections 1 to 3). ``verify_mode = CERT_NONE`` and
``check_hostname = False`` are set here and only here, and this function also installs the check: it
dials, completes the handshake, checks ALPN and the TLS version, reads the leaf certificate **in
binary form** (with verification off the dict form is an empty ``{}``), takes the ed25519 key,
applies the policy, and only then hands the socket back. It writes **nothing** to the socket, so
the ``AUTH`` frame that holds the password cannot have been sent to a server nobody trusted.

``load_default_certs`` and ``create_default_context`` are never used: the system CA store is not
the trust root.
"""

from __future__ import annotations

import inspect
import logging
import socket
import ssl
import warnings
from dataclasses import dataclass
from typing import Final, Literal, Protocol

from hoarddb import errors
from hoarddb._core.uri import HostPort
from hoarddb._trust import der, fingerprint, policy
from hoarddb._trust.known_servers import KnownServers
from hoarddb.options import ServerKey, TlsOptions

ALPN: Final = "hoarddb/1"


def _context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.set_alpn_protocols([ALPN])
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def note_insecure(logger: logging.Logger, host: HostPort, reason: str, stacklevel: int = 3) -> None:
    """Insecure mode is loud: a WARNING on every dial (the every-time channel) and one
    ``InsecureTlsWarning`` per location (Python's default filter shows a warning once)."""
    message = (
        f"TLS verification is off for {host} (insecure_reason: {reason}). "
        "This connection can be intercepted."
    )
    logger.warning(message)
    warnings.warn(errors.InsecureTlsWarning(message), stacklevel=stacklevel)


class TlsLike(Protocol):
    """What the key check needs of a TLS connection: ``ssl.SSLSocket`` and ``ssl.SSLObject`` both
    have it, so the blocking and the asyncio connection share one check."""

    def selected_alpn_protocol(self) -> str | None: ...

    def version(self) -> str | None: ...

    def getpeercert(self, binary_form: Literal[True]) -> bytes | None: ...


def new_context() -> ssl.SSLContext:
    """The one TLS context (see the module docstring); the asyncio dial uses it too."""
    return _context()


def handshake_failed(host: HostPort, exc: ssl.SSLError) -> errors.ConnectionFailedError:
    return errors.ConnectionFailedError(
        f"The TLS handshake with {host} failed ({exc.reason or exc}). hoardDB requires "
        f"TLS 1.3 with the ALPN protocol {ALPN}: is this a hoardDB driver port?",
        operation="connect",
        target=str(host),
    )


def _fail(sock: socket.socket, error: BaseException) -> BaseException:
    sock.close()  # no close_notify and no application bytes: only the TCP close
    return error


@dataclass(frozen=True, slots=True)
class Unknown:
    """A key no pin and no recorded entry vouches for: the program's callback decides."""

    host: HostPort
    key: bytes
    presented: str
    store: KnownServers | None

    def server_key(self) -> ServerKey:
        return ServerKey(self.host.host, self.host.port, self.presented, self.key)


def vet(
    tls_conn: TlsLike,
    host: HostPort,
    pin: str | None,
    tls: TlsOptions,
    log: logging.Logger,
) -> Unknown | None:
    """Everything that decides trust except the program's callback. Raises on a refusal (the caller
    closes the connection, having written nothing); returns None when the key is trusted, or the
    :class:`Unknown` to ask the callback about."""
    insecure = tls.insecure_reason
    if tls_conn.selected_alpn_protocol() != ALPN:
        raise errors.UnsupportedVersionError(
            f"The server at {host} did not agree to the protocol {ALPN} "
            f"(it chose {tls_conn.selected_alpn_protocol()!r}). This driver speaks {ALPN}; "
            "is this a hoardDB driver port?",
            operation="connect",
            target=str(host),
        )
    if tls_conn.version() != "TLSv1.3":
        raise errors.ConnectionFailedError(
            f"The server at {host} negotiated {tls_conn.version()}; hoardDB requires TLS 1.3.",
            operation="connect",
            target=str(host),
        )
    certificate = tls_conn.getpeercert(binary_form=True)  # never the dict form: it is {} here
    if not certificate:
        raise errors.TlsTrustError(
            f"The server at {host} presented no certificate, so its key cannot be checked. "
            "Nothing was sent.",
            code="TLS_UNTRUSTED",
            operation="connect",
            target=str(host),
        )
    if insecure is not None:
        note_insecure(log, host, insecure, stacklevel=4)
        return None
    try:
        key = der.ed25519_public_key(certificate)
    except der.DerError as bad:
        raise errors.TlsTrustError(
            f"The server at {host} presented a certificate this driver cannot trust: {bad}. "
            "Nothing was sent.",
            code="TLS_KEY_TYPE",
            operation="connect",
            target=str(host),
        ) from None
    presented = fingerprint.fingerprint_of_key(key)

    store = KnownServers.for_options(tls.known_servers_path) if tls.use_known_servers else None
    recorded = store.lookup(host) if store is not None and pin is None else None
    verdict = policy.evaluate(key, pin, recorded)
    if verdict in (policy.Verdict.PINNED, policy.Verdict.RECORDED):
        return None
    if verdict is policy.Verdict.PIN_MISMATCH:
        assert pin is not None
        raise policy.pin_mismatch(host, presented, pin)
    if verdict is policy.Verdict.KEY_CHANGED:
        assert store is not None
        assert recorded is not None
        raise policy.key_changed(
            host, presented, fingerprint.fingerprint_of_key(recorded), store.path
        )
    if tls.on_unknown_server is None:
        raise policy.untrusted(host, presented)
    return Unknown(host, key, presented, store)


def settle(unknown: Unknown, answer: object, log: logging.Logger) -> None:
    """Apply the callback's answer: only the object ``True`` trusts the key (and records it)."""
    if answer is not True:  # only an explicit True trusts a key; "no" and 1 do not
        raise policy.untrusted(unknown.host, unknown.presented)
    if unknown.store is not None:
        try:
            unknown.store.record(unknown.host, unknown.key)
        except OSError as exc:
            log.warning("Could not record the key for %s in known_servers: %s", unknown.host, exc)


def awaitable_callback_error() -> errors.ConfigError:
    return errors.ConfigError(
        "on_unknown_server returned an awaitable, but this is the blocking client. "
        "Use the asyncio client (import hoarddb.aio) or pass a plain function.",
        code="INVALID_OPTIONS",
        operation="connect",
    )


def check_options(tls: TlsOptions, pin: str | None) -> None:
    """The combinations refused before any dial, whichever connection is dialling."""
    policy.refuse_unremembered_first_sight(tls)
    if tls.insecure_reason is not None and pin is not None:
        raise errors.ConfigError(
            "this client was given both a pin and insecure_reason; the pin would be ignored. "
            "Remove one.",
            code="INVALID_OPTIONS",
            operation="connect",
        )


def connect_pinned(
    host: HostPort,
    pin: str | None,
    tls: TlsOptions,
    *,
    timeout: float = 10.0,
    logger: logging.Logger | None = None,
) -> ssl.SSLSocket:
    log = logger or logging.getLogger("hoarddb")
    check_options(tls, pin)
    raw = socket.create_connection((host.host, host.port), timeout=timeout)
    try:
        sock = _context().wrap_socket(raw)
    except ssl.SSLError as exc:
        raw.close()
        raise handshake_failed(host, exc) from None
    except BaseException:
        raw.close()
        raise
    try:
        unknown = vet(sock, host, pin, tls, log)
        if unknown is not None:
            callback = tls.on_unknown_server
            assert callback is not None
            answer = callback(unknown.server_key())
            if inspect.isawaitable(answer):
                if inspect.iscoroutine(answer):
                    answer.close()  # we will not await it; do not leave it "never awaited"
                raise awaitable_callback_error()
            settle(unknown, answer, log)
        return sock
    except BaseException as error:
        raise _fail(sock, error) from None
