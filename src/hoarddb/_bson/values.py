# SPDX-License-Identifier: Apache-2.0
"""``Opaque``: a stored BSON value with no Python form in v1 (spec 7.2).

It holds the value's body bytes (everything after the type byte and the key), so writing it back
reproduces the element byte for byte. There are no constructors: a program gets one by reading.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# BSON element type bytes by the name an ``Opaque`` carries.
TYPE_BYTES: Final = {
    "double": 0x01,
    "string": 0x02,
    "document": 0x03,
    "array": 0x04,
    "binary": 0x05,
    "undefined": 0x06,
    "objectid": 0x07,
    "bool": 0x08,
    "datetime": 0x09,
    "null": 0x0A,
    "regex": 0x0B,
    "dbpointer": 0x0C,
    "javascript": 0x0D,
    "symbol": 0x0E,
    "javascript_with_scope": 0x0F,
    "int32": 0x10,
    "timestamp": 0x11,
    "int64": 0x12,
    "decimal128": 0x13,
    "maxkey": 0x7F,
    "minkey": 0xFF,
}


@dataclass(frozen=True, slots=True)
class Opaque:
    """A BSON value this driver stores and returns but has no Python type for: an object id, a
    timestamp, a regex, min/max key, JavaScript, a symbol, a DBPointer, undefined, a binary of
    any subtype but 0 and 4, or a datetime outside Python's range. Compared by value."""

    bson_type: str
    data: bytes
