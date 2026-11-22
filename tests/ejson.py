# SPDX-License-Identifier: Apache-2.0
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false
"""A test-only Extended JSON (version 2) reader for the driver vectors' ``payload_json``.

It turns the canonical form (``$numberInt``, ``$numberLong``, ``$numberDouble``,
``$numberDecimal``, ``$date``, ``$oid``, ``$binary``) into the Python values the codec reads and
writes, so a vector's request can be compared field by field. Independent of the codec on purpose.
"""

from __future__ import annotations

import base64
import math
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from hoarddb._bson import Opaque

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def to_python(value: Any) -> Any:
    if isinstance(value, list):
        return [to_python(v) for v in value]
    if not isinstance(value, dict):
        return value
    if len(value) == 1:
        ((tag, inner),) = value.items()
        if tag == "$numberInt" or tag == "$numberLong":
            return int(inner)
        if tag == "$numberDouble":
            specials = {"NaN": math.nan, "Infinity": math.inf, "-Infinity": -math.inf}
            return specials[inner] if inner in specials else float(inner)
        if tag == "$numberDecimal":
            return Decimal(inner)
        if tag == "$date":
            millis = int(inner["$numberLong"])
            return EPOCH + timedelta(milliseconds=millis)
        if tag == "$oid":
            return Opaque("objectid", bytes.fromhex(inner))
        if tag == "$binary":
            data = base64.b64decode(inner["base64"])
            subtype = int(inner["subType"], 16)
            if subtype == 0:
                return data
            if subtype == 4 and len(data) == 16:
                return uuid.UUID(bytes=data)
            raise AssertionError(f"ejson: binary subtype {subtype:02x} is not used by the vectors")
        if tag.startswith("$"):
            raise AssertionError(f"ejson: unsupported form {tag}")
    return {k: to_python(v) for k, v in value.items()}
