# SPDX-License-Identifier: Apache-2.0
"""P4: decimal128 (BID) in the codec (spec 7.2, OQ-2)."""

from __future__ import annotations

import random
from decimal import Decimal

import pytest

import bsonvec
from hoarddb import errors
from hoarddb._bson import decimal128 as d128
from hoarddb._bson import deserialize, serialize

# 16 bytes little-endian: the high 64 bits hold sign, 14-bit biased exponent (bias 6176), and the
# top 49 bits of a 113-bit coefficient. Computed by hand:
HAND = {
    "1": ("01000000000000000000000000004030", Decimal("1")),  # biased exponent 6176 = 0x1820
    "-1": ("010000000000000000000000000040b0", Decimal("-1")),
    "0": ("00000000000000000000000000004030", Decimal("0")),
    "-0": ("000000000000000000000000000040b0", Decimal("-0")),
    "1.50": ("96000000000000000000000000003c30", Decimal("1.50")),  # 150 at exponent -2
    "0.1": ("01000000000000000000000000003e30", Decimal("0.1")),
    "0E+3": ("00000000000000000000000000004630", Decimal("0E+3")),
    "Infinity": ("00000000000000000000000000000078", Decimal("Infinity")),
    "-Infinity": ("000000000000000000000000000000f8", Decimal("-Infinity")),
    "NaN": ("0000000000000000000000000000007c", Decimal("NaN")),
}


@pytest.mark.parametrize(("name", "case"), list(HAND.items()), ids=list(HAND))
def test_hand_computed_decimal128(name: str, case: tuple[str, Decimal]) -> None:
    hexed, value = case
    assert d128.encode(value).hex() == hexed
    assert bsonvec.canon(d128.decode(bytes.fromhex(hexed))) == bsonvec.canon(value)


def test_trailing_zeros_and_exponent_are_part_of_the_value() -> None:
    assert d128.encode(Decimal("1.50")) != d128.encode(Decimal("1.5"))
    back = d128.decode(d128.encode(Decimal("1.50")))
    assert back.as_tuple() == Decimal("1.50").as_tuple()


def test_negative_nan_keeps_its_sign_and_payload_is_dropped() -> None:
    assert d128.decode(d128.encode(Decimal("-NaN"))).is_signed()
    assert d128.encode(Decimal("NaN123")) == d128.encode(Decimal("NaN"))


def test_the_limits_of_the_format_are_exact() -> None:
    top = Decimal("9" * 34)
    assert d128.decode(d128.encode(top)) == top
    assert d128.decode(d128.encode(Decimal("1E+6111"))) == Decimal("1E+6111")
    assert d128.decode(d128.encode(Decimal("1E-6176"))) == Decimal("1E-6176")
    biggest = Decimal("9." + "9" * 33 + "E+6144")
    assert d128.decode(d128.encode(biggest)).as_tuple() == biggest.as_tuple()


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("1" * 35, "34 significant digits"),
        ("1E+6112", "exponent"),
        ("1E-6177", "exponent"),
        ("0E+6112", "exponent"),
        ("sNaN", "signalling"),
    ],
)
def test_what_does_not_fit_is_refused_never_rounded(text: str, needle: str) -> None:
    with pytest.raises(errors.InvalidArgumentError) as info:
        serialize({"price": Decimal(text)})
    assert "price" in info.value.message
    assert needle in info.value.message


def test_a_non_canonical_large_coefficient_reads_as_zero() -> None:
    # coefficient field above 10^34 - 1: IEEE says it is zero
    raw = bytes.fromhex("ffffffffffffffffffffffffffff4130")
    assert d128.decode(raw) == Decimal(0)


def test_the_codec_maps_decimal_to_type_13() -> None:
    raw = serialize({"a": Decimal("1.5")})
    assert raw[4] == 0x13
    assert deserialize(raw)["a"] == Decimal("1.5")


def test_random_round_trips() -> None:
    rng = random.Random(1234)
    for _ in range(2000):
        digits = "".join(rng.choice("0123456789") for _ in range(rng.randint(1, 34)))
        exponent = rng.randint(-6176, 6111)
        value = Decimal(f"{rng.choice(['', '-'])}{digits}E{exponent:+d}")
        back = d128.decode(d128.encode(value))
        assert back.as_tuple() == value.as_tuple()
