# SPDX-License-Identifier: Apache-2.0
"""A typed response as the plain mapping the in-band checks (``failure.check_*``) read."""

from __future__ import annotations

import dataclasses
from typing import Any

_NAMES: dict[type[Any], tuple[str, ...]] = {}


def as_payload(response: Any) -> dict[str, Any]:
    """The response's fields by Python name (cheap: no copy of the nested values)."""
    cls: type[Any] = type(response)  # pyright: ignore[reportUnknownVariableType]
    names = _NAMES.get(cls)
    if names is None:
        names = _NAMES[cls] = tuple(f.name for f in dataclasses.fields(cls))
    return {name: getattr(response, name) for name in names}
