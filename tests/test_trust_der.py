# SPDX-License-Identifier: Apache-2.0
"""P6: the DER walker and the fingerprint (trust.md section 2, spec 5.1 items 3 and 4)."""

from __future__ import annotations

import base64
import hashlib
import random

import pytest

import faketls
import vectors
from hoarddb._trust import der, fingerprint

TRUST = [v for v in vectors.driver_vectors(kind="trust") if "public_key_hex" in v]


@pytest.fixture(scope="module")
def identity(tmp_path_factory: pytest.TempPathFactory) -> faketls.Identity:
    return faketls.make_identity(tmp_path_factory.mktemp("id"))


@pytest.mark.parametrize("vec", TRUST, ids=lambda v: v["name"])
def test_the_vectors_fingerprint_comes_from_the_key_and_from_the_certificate(
    vec: vectors.Vector,
) -> None:
    key = bytes.fromhex(vec["public_key_hex"])
    assert fingerprint.fingerprint_of_key(key) == vec["fingerprint"]
    cert = der.pem_to_der(vec["certificate_pem"])
    assert der.ed25519_public_key(cert) == key
    assert fingerprint.fingerprint_of_certificate(cert) == vec["fingerprint"]


def test_the_fingerprint_hashes_the_32_key_bytes_not_the_der_and_not_the_spki() -> None:
    vec = TRUST[0]
    key = bytes.fromhex(vec["public_key_hex"])
    cert = der.pem_to_der(vec["certificate_pem"])
    wrong_der = "SHA256:" + hashlib.sha256(cert).hexdigest()
    spki = bytes.fromhex("302a300506032b6570032100") + key
    wrong_spki = "SHA256:" + hashlib.sha256(spki).hexdigest()
    assert vec["fingerprint"] not in (wrong_der, wrong_spki)
    assert fingerprint.fingerprint_of_certificate(cert) == vec["fingerprint"]


def test_a_certificate_made_by_openssl_gives_the_key_openssl_reports(
    identity: faketls.Identity,
) -> None:
    assert der.ed25519_public_key(identity.cert_der) == identity.public_key
    pem = identity.cert_path.read_text()
    assert der.pem_to_der(pem) == identity.cert_der


def test_a_key_of_the_wrong_length_has_no_fingerprint() -> None:
    for bad in (b"", bytes(31), bytes(33)):
        with pytest.raises(ValueError, match="32"):
            fingerprint.fingerprint_of_key(bad)


def test_pins_compare_in_constant_time() -> None:
    fp = fingerprint.fingerprint_of_key(bytes(32))
    assert fingerprint.pins_equal(fp, fp)
    assert not fingerprint.pins_equal(fp, fingerprint.fingerprint_of_key(bytes(31) + b"\x01"))


# --- the walker refuses what is not an ed25519 certificate, by name -----------------------------


def tlv(tag: int, content: bytes) -> bytes:
    n = len(content)
    if n < 0x80:
        length = bytes([n])
    else:
        raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
        length = bytes([0x80 | len(raw)]) + raw
    return bytes([tag]) + length + content


def certificate(algorithm_oid: bytes, key: bytes, *, bits: bytes | None = None) -> bytes:
    alg = tlv(0x30, tlv(0x06, algorithm_oid))
    spki = tlv(0x30, alg + tlv(0x03, bits if bits is not None else b"\x00" + key))
    name = tlv(0x30, tlv(0x31, tlv(0x30, tlv(0x06, b"\x55\x04\x03") + tlv(0x0C, b"x"))))
    tbs = tlv(
        0x30,
        tlv(0xA0, tlv(0x02, b"\x02"))
        + tlv(0x02, b"\x01")
        + alg
        + name
        + tlv(0x30, tlv(0x17, b"260101000000Z") + tlv(0x17, b"360101000000Z"))
        + name
        + spki,
    )
    return tlv(0x30, tbs + alg + tlv(0x03, b"\x00" + bytes(64)))


ED25519 = bytes.fromhex("2b6570")
KEY = bytes(range(32))


def test_a_hand_built_certificate_reads_back() -> None:
    assert der.ed25519_public_key(certificate(ED25519, KEY)) == KEY


@pytest.mark.parametrize(
    ("oid", "name"),
    [
        (bytes.fromhex("2a864886f70d010101"), "RSA"),
        (bytes.fromhex("2a8648ce3d0201"), "EC"),
        (bytes.fromhex("2b656e"), "X25519"),
        (bytes.fromhex("2b6571"), "Ed448"),
        (bytes.fromhex("2a030405"), "1.2.3.4.5"),
    ],
)
def test_another_key_type_is_refused_by_name_not_hashed_into_a_fingerprint(
    oid: bytes, name: str
) -> None:
    with pytest.raises(der.DerError) as info:
        der.ed25519_public_key(certificate(oid, KEY))
    assert name in str(info.value)
    assert "ed25519" in str(info.value)


def test_an_ed25519_key_of_the_wrong_size_is_refused() -> None:
    with pytest.raises(der.DerError, match="32"):
        der.ed25519_public_key(certificate(ED25519, KEY[:31]))
    with pytest.raises(der.DerError, match="unused"):
        der.ed25519_public_key(certificate(ED25519, KEY, bits=b"\x01" + KEY))


@pytest.mark.parametrize(
    "bad",
    [
        b"",
        b"\x30",
        b"\x30\x00",
        b"\x30\x80\x00\x00",  # indefinite length is not DER
        b"\x02\x01\x00",  # not a SEQUENCE
        b"\x30\x84\xff\xff\xff\xff",  # a length that overruns
        certificate(ED25519, KEY) + b"\x00",  # trailing bytes
        certificate(ED25519, KEY)[:-1],  # truncated
    ],
)
def test_malformed_der_is_a_der_error_never_another_exception(bad: bytes) -> None:
    with pytest.raises(der.DerError):
        der.ed25519_public_key(bad)


def test_fuzz_mutated_certificates_only_ever_raise_der_error() -> None:
    rng = random.Random(0xDE12)
    seed = certificate(ED25519, KEY)
    for _ in range(4000):
        data = bytearray(seed)
        for _ in range(rng.randrange(1, 4)):
            if not data:
                break
            roll = rng.random()
            if roll < 0.6:
                data[rng.randrange(len(data))] = rng.getrandbits(8)
            elif roll < 0.8:
                del data[rng.randrange(len(data)) :]
            else:
                data.insert(rng.randrange(len(data)), rng.getrandbits(8))
        try:
            key = der.ed25519_public_key(bytes(data))
        except der.DerError:
            continue
        assert len(key) == 32


def test_pem_handling() -> None:
    body = base64.b64encode(certificate(ED25519, KEY)).decode()
    pem = f"-----BEGIN CERTIFICATE-----\n{body}\n-----END CERTIFICATE-----\n"
    assert der.ed25519_public_key(der.pem_to_der(pem)) == KEY
    for bad in (
        "",
        "no markers",
        pem.replace("BEGIN", "BEGAN"),
        "-----BEGIN CERTIFICATE-----\n!!\n-----END CERTIFICATE-----",
    ):
        with pytest.raises(der.DerError):
            der.pem_to_der(bad)


def test_a_pem_with_non_ascii_text_is_a_der_error_not_a_value_error() -> None:
    pem = "-----BEGIN CERTIFICATE-----\nMIIBé中\n-----END CERTIFICATE-----\n"
    with pytest.raises(der.DerError, match="base64"):
        der.pem_to_der(pem)
