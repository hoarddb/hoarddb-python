# SPDX-License-Identifier: Apache-2.0
"""The one pin canonicaliser (trust.md section 2).

A fingerprint is ``SHA256:`` followed by 64 lowercase hex characters. There is exactly one format:
the prefix and the hex are accepted in any case and canonicalised, and nothing else is ("close
enough" forms would accept unrelated keys). The URI parser and ``TlsOptions.fingerprints`` both
come through :func:`canonicalise`, so a pin is judged by one rule wherever it was written.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Sequence
from typing import Final

from hoarddb._trust import der

PREFIX: Final = "SHA256:"
HEX_LENGTH: Final = 64
_HEX: Final = frozenset("0123456789abcdef")


class FingerprintError(ValueError):
    """The text is not a fingerprint; the message names the expected shape."""


def canonicalise(value: str) -> str:
    # ASCII only: str.upper() would let U+017F (long s) pass for the "S" of the prefix.
    head = value[: len(PREFIX)]
    if not head.isascii() or head.upper() != PREFIX:
        raise FingerprintError(
            f'fingerprint "{value}" must start with "{PREFIX}" followed by {HEX_LENGTH} hex '
            "characters (the value the server prints at startup)"
        )
    digest = value[len(PREFIX) :]
    if digest.isascii():
        digest = digest.lower()
    if len(digest) != HEX_LENGTH:
        raise FingerprintError(
            f'fingerprint "{value}" must have exactly {HEX_LENGTH} hex characters after '
            f'"{PREFIX}", found {len(digest)}'
        )
    if not set(digest) <= _HEX:
        raise FingerprintError(
            f'fingerprint "{value}" must be hex (0-9, a-f) after "{PREFIX}"; '
            "it contains other characters"
        )
    return PREFIX + digest


def pin_for(pins: Sequence[str], index: int) -> str | None:
    """The pin for the host at ``index``: one pin applies to every host, otherwise they pair by
    position (trust.md section 6)."""
    if not pins:
        return None
    return pins[0] if len(pins) == 1 else pins[index]


def fingerprint_of_key(key: bytes) -> str:
    """``SHA256:`` and the hex of SHA-256 over the **32 raw key bytes**: not the certificate's DER
    and not the SPKI that wraps the key (trust.md section 2)."""
    if len(key) != 32:
        raise ValueError(f"an ed25519 key is 32 bytes, got {len(key)}")
    return PREFIX + hashlib.sha256(key).hexdigest()


def fingerprint_of_certificate(cert: bytes) -> str:
    return fingerprint_of_key(der.ed25519_public_key(cert))


def pins_equal(a: str, b: str) -> bool:
    """Constant-time comparison of two canonical fingerprints."""
    return hmac.compare_digest(a.encode("ascii"), b.encode("ascii"))
