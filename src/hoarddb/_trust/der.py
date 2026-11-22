# SPDX-License-Identifier: Apache-2.0
"""A small DER walker: ``Certificate -> tbsCertificate -> subjectPublicKeyInfo`` -> the ed25519 key.

About a screenful, no dependency. It is strict: no indefinite lengths, no high tag numbers, no
trailing bytes, and a key of any type but ed25519 is refused *by name* instead of being hashed into
a fingerprint that would never match (spec 5.1 item 3).
"""

from __future__ import annotations

import base64
import binascii
from typing import Final


class DerError(ValueError):
    """The bytes are not a certificate holding an ed25519 key; the message says what is wrong."""


ED25519_OID: Final = bytes.fromhex("2b6570")  # 1.3.101.112
_KEY_TYPES: Final = {
    bytes.fromhex("2a864886f70d010101"): "RSA",
    bytes.fromhex("2a8648ce3d0201"): "EC",
    bytes.fromhex("2b656e"): "X25519",
    bytes.fromhex("2b6571"): "Ed448",
}
_SEQUENCE: Final = 0x30
_INTEGER: Final = 0x02
_OID: Final = 0x06
_BIT_STRING: Final = 0x03
_VERSION: Final = 0xA0


def _tlv(data: bytes, pos: int, end: int) -> tuple[int, int, int]:
    """One tag-length-value at ``pos``: returns ``(tag, value_start, value_end)``."""
    if pos >= end:
        raise DerError("the certificate is truncated")
    tag = data[pos]
    pos += 1
    if tag & 0x1F == 0x1F:
        raise DerError("the certificate uses a high tag number, which X.509 does not")
    if pos >= end:
        raise DerError("the certificate is truncated")
    first = data[pos]
    pos += 1
    if first < 0x80:
        length = first
    elif first == 0x80:
        raise DerError("the certificate uses an indefinite length, which DER forbids")
    else:
        count = first & 0x7F
        if count > 4 or pos + count > end:
            raise DerError("the certificate has a length that runs past its end")
        raw = data[pos : pos + count]
        if raw[0] == 0:
            raise DerError("the certificate has a length that is not minimally encoded")
        length = int.from_bytes(raw, "big")
        if length < 0x80:
            raise DerError("the certificate has a length that is not minimally encoded")
        pos += count
    stop = pos + length
    if stop > end:
        raise DerError("the certificate has a length that runs past its end")
    return tag, pos, stop


def _expect(data: bytes, pos: int, end: int, tag: int, what: str) -> tuple[int, int]:
    got, start, stop = _tlv(data, pos, end)
    if got != tag:
        raise DerError(f"expected {what} but found tag {got:#04x}")
    return start, stop


def _dotted(oid: bytes) -> str:
    if not oid:
        return ""
    parts = [str(oid[0] // 40), str(oid[0] % 40)]
    value = 0
    for byte in oid[1:]:
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            parts.append(str(value))
            value = 0
    return ".".join(parts)


def ed25519_public_key(cert: bytes) -> bytes:
    """The raw 32-byte ed25519 key of a DER certificate."""
    outer, outer_end = _expect(cert, 0, len(cert), _SEQUENCE, "a certificate")
    if outer_end != len(cert):
        raise DerError("there are bytes after the certificate")
    tbs, tbs_end = _expect(cert, outer, outer_end, _SEQUENCE, "the tbsCertificate")
    pos = tbs
    if pos < tbs_end and cert[pos] == _VERSION:
        _, pos = _expect(cert, pos, tbs_end, _VERSION, "the version")
    for tag, what in (
        (_INTEGER, "the serial number"),
        (_SEQUENCE, "the signature algorithm"),
        (_SEQUENCE, "the issuer"),
        (_SEQUENCE, "the validity"),
        (_SEQUENCE, "the subject"),
    ):
        _, pos = _expect(cert, pos, tbs_end, tag, what)
    spki, spki_end = _expect(cert, pos, tbs_end, _SEQUENCE, "the subjectPublicKeyInfo")
    alg, alg_end = _expect(cert, spki, spki_end, _SEQUENCE, "the key algorithm")
    oid_start, oid_end = _expect(cert, alg, alg_end, _OID, "the key algorithm identifier")
    oid = cert[oid_start:oid_end]
    if oid != ED25519_OID:
        kind = _KEY_TYPES.get(oid) or f"unknown (OID {_dotted(oid)})"
        raise DerError(
            f"the certificate holds a {kind} key (OID {_dotted(oid)}), but hoardDB servers "
            "present ed25519 keys: is this a hoardDB server?"
        )
    bits, bits_end = _expect(cert, alg_end, spki_end, _BIT_STRING, "the public key")
    if bits_end != spki_end:
        raise DerError("there are bytes after the public key")
    if bits == bits_end or cert[bits] != 0:
        raise DerError("the public key has unused bits")
    key = cert[bits + 1 : bits_end]
    if len(key) != 32:
        raise DerError(f"the ed25519 public key is {len(key)} bytes; it must be 32")
    return bytes(key)


def pem_to_der(pem: str) -> bytes:
    begin, end = "-----BEGIN CERTIFICATE-----", "-----END CERTIFICATE-----"
    start = pem.find(begin)
    stop = pem.find(end)
    if start == -1 or stop == -1 or stop < start:
        raise DerError("the text is not a PEM certificate (no BEGIN/END CERTIFICATE lines)")
    body = "".join(pem[start + len(begin) : stop].split())
    try:
        return base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError):  # ValueError: non-ASCII text
        raise DerError(
            "the PEM certificate body is not valid base64 (it must be plain ASCII letters, "
            "digits, '+', '/' and '=')"
        ) from None
