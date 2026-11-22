# SPDX-License-Identifier: Apache-2.0
"""The call kinds the retry table reads (spec 8.1)."""

from __future__ import annotations

from typing import Literal

Kind = Literal["READ", "WRITE", "DDL"]
READ: Kind = "READ"
WRITE: Kind = "WRITE"
DDL: Kind = "DDL"
