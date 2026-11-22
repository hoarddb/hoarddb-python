# SPDX-License-Identifier: Apache-2.0
"""Loaders for the frozen vector files. Read-only: nothing here ever writes to testdata/."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DRIVER_VECTORS = ROOT / "testdata" / "driver-vectors" / "v1.json"
URI_VECTORS = ROOT / "testdata" / "uri-vectors" / "v1.json"
BSON_CORPUS = ROOT / "testdata" / "bson-vectors" / "v1.json"

Vector = dict[str, Any]


def driver_vectors(kind: str | None = None, group: str | None = None) -> list[Vector]:
    data = json.loads(DRIVER_VECTORS.read_text(encoding="utf-8"))
    out: list[Vector] = data["vectors"]
    if kind is not None:
        out = [v for v in out if v["kind"] == kind]
    if group is not None:
        out = [v for v in out if v["group"] == group]
    return out


def uri_vectors() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(URI_VECTORS.read_text(encoding="utf-8"))
    return data
