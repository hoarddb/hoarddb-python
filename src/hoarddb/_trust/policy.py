# SPDX-License-Identifier: Apache-2.0
"""The trust policy (trust.md section 3), as a pure function and the messages that explain it.

Order: a pin; else the shared ``known_servers`` file; else the program's callback; else refuse. A
pin that does not match, and a recorded key that has changed, are hard failures that name both
fingerprints: never a prompt, never retried, never rescued by another source.
"""

from __future__ import annotations

import logging
from enum import Enum, auto

from hoarddb import errors
from hoarddb._core.uri import HostPort
from hoarddb._trust import fingerprint
from hoarddb.options import ServerKey, TlsOptions

fingerprint_of = fingerprint.fingerprint_of_key


class Verdict(Enum):
    PINNED = auto()  # the key matches the pin
    PIN_MISMATCH = auto()
    RECORDED = auto()  # the key matches the shared file
    KEY_CHANGED = auto()  # the file records another key for this host
    UNKNOWN = auto()  # ask the program's callback, else refuse


def evaluate(presented: bytes, pin: str | None, recorded: bytes | None) -> Verdict:
    if pin is not None:
        ok = fingerprint.pins_equal(fingerprint.fingerprint_of_key(presented), pin)
        return Verdict.PINNED if ok else Verdict.PIN_MISMATCH
    if recorded is not None:
        return Verdict.RECORDED if recorded == presented else Verdict.KEY_CHANGED
    return Verdict.UNKNOWN


def untrusted(host: HostPort, presented: str) -> errors.TlsTrustError:
    return errors.TlsTrustError(
        f"The server at {host} presented key {presented}, which this client has not been told to "
        "trust. Nothing was sent: your password never left this process. Trust it one of three "
        f'ways: pin it (`hoarddb.connect("hoarddb://{host}?fingerprint={presented}")`, the value '
        f"the server prints at startup), trust it once with `python -m hoarddb trust {host}`, or "
        "for local development pass "
        "tls=`hoarddb.TlsOptions(on_unknown_server=hoarddb.accept_first_sight)`.",
        code="TLS_UNTRUSTED",
        operation="connect",
        target=str(host),
    )


def pin_mismatch(host: HostPort, presented: str, pinned: str) -> errors.TlsTrustError:
    return errors.TlsTrustError(
        f"The server at {host} presented {presented} but this client pinned {pinned}. "
        "Refusing to connect, and not retrying. If you rotated the server's key on purpose, "
        "update the pin; if not, the connection may be intercepted.",
        code="TLS_FINGERPRINT_MISMATCH",
        operation="connect",
        target=str(host),
    )


def key_changed(
    host: HostPort, presented: str, recorded: str, path: object
) -> errors.TlsTrustError:
    return errors.TlsTrustError(
        f"The server at {host} presented {presented} but {path} records {recorded} for it. "
        "Refusing to connect, and not retrying. If the server's key was rotated on purpose, "
        f"delete the line for {host} from {path}; if not, the connection may be intercepted.",
        code="TLS_KEY_CHANGED",
        operation="connect",
        target=str(host),
    )


def accept_first_sight(key: ServerKey) -> bool:
    """A ready-made ``on_unknown_server`` for local development: trust the first key a host shows
    (and let the policy record it), as SSH does, and say so. Never a default."""
    logging.getLogger("hoarddb").warning(
        "Trusting the first key %s presented by %s:%s and recording it in known_servers "
        "(accept_first_sight). This is trust on first use: it is weaker than a pin against "
        "anyone present at this first connection.",
        key.fingerprint,
        key.host,
        key.port,
    )
    return True


def refuse_unremembered_first_sight(tls: TlsOptions) -> None:
    """``accept_first_sight`` trusts what it sees *and relies on known_servers to remember it*.
    Without the file it would accept any key on every dial, which is verification switched off
    with only a warning; that is what ``insecure_reason`` is for."""
    if not tls.use_known_servers and tls.on_unknown_server is accept_first_sight:
        raise errors.ConfigError(
            "accept_first_sight with use_known_servers=False would trust whatever key shows up on "
            "every connection, because nothing remembers the first one. Leave use_known_servers "
            "on, pin the key with fingerprints=[...], or, for a throwaway test server, say so "
            'with TlsOptions(insecure_reason="...").',
            code="INVALID_OPTIONS",
            operation="connect",
        )
