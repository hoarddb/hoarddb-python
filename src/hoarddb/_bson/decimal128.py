# SPDX-License-Identifier: Apache-2.0
"""decimal128 in the BID encoding (IEEE 754-2008), as BSON writes it: 16 bytes little-endian.

The high 64 bits hold the sign (bit 63), a 14-bit exponent biased by 6176 (bits 62..49) and the top
49 bits of a coefficient of at most 113 bits; the low 64 bits hold the rest. A value is written with
the digits and exponent it has (``1.50`` and ``1.5`` are different bytes). Nothing is rounded: a
value that needs more than 34 significant digits, or an exponent outside -6176..6111, is refused.
"""

from __future__ import annotations

import struct
from decimal import Decimal
from typing import Final

BIAS: Final = 6176
MAX_EXPONENT: Final = 6111
MIN_EXPONENT: Final = -6176
MAX_DIGITS: Final = 34
_MAX_COEFFICIENT: Final = 10**MAX_DIGITS - 1
_MASK64: Final = (1 << 64) - 1
_PAIR = struct.Struct("<QQ")

_NAN_HI: Final = 0x7C00 << 48
_INF_HI: Final = 0x7800 << 48


class Decimal128Error(ValueError):
    """The value cannot be written as decimal128; the message says why."""


def encode(value: Decimal) -> bytes:
    sign = 1 if value.is_signed() else 0
    if value.is_snan():
        raise Decimal128Error("is a signalling NaN, which cannot be stored; use NaN")
    if value.is_nan():
        return _PAIR.pack(0, (sign << 63) | _NAN_HI)
    if value.is_infinite():
        return _PAIR.pack(0, (sign << 63) | _INF_HI)
    _, digits, exponent = value.as_tuple()
    assert isinstance(exponent, int)
    if len(digits) > MAX_DIGITS:
        raise Decimal128Error(
            f"needs {len(digits)} significant digits; decimal128 holds at most "
            f"{MAX_DIGITS} significant digits and the driver never rounds"
        )
    if not MIN_EXPONENT <= exponent <= MAX_EXPONENT:
        raise Decimal128Error(
            f"has exponent {exponent}, outside decimal128's range {MIN_EXPONENT} to {MAX_EXPONENT}"
        )
    coefficient = int("".join(map(str, digits)))
    hi = (sign << 63) | ((exponent + BIAS) << 49) | (coefficient >> 64)
    return _PAIR.pack(coefficient & _MASK64, hi)


def decode(data: bytes) -> Decimal:
    lo, hi = _PAIR.unpack(data)
    sign = hi >> 63
    top = (hi >> 58) & 0x1F
    if top == 0x1F:
        return Decimal("-NaN" if sign else "NaN")
    if top == 0x1E:
        return Decimal("-Infinity" if sign else "Infinity")
    if (hi >> 61) & 3 == 3:
        # The "large coefficient" form: every such coefficient is over 10^34 - 1, so IEEE reads 0.
        exponent = (hi >> 47) & 0x3FFF
        coefficient = 0
    else:
        exponent = (hi >> 49) & 0x3FFF
        coefficient = ((hi & 0x1FFFFFFFFFFFF) << 64) | lo
        if coefficient > _MAX_COEFFICIENT:
            coefficient = 0
    digits = tuple(int(c) for c in str(coefficient))
    return Decimal((sign, digits, exponent - BIAS))
