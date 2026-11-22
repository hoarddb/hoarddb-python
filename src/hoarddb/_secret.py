# SPDX-License-Identifier: Apache-2.0
"""``Secret``: a string that prints ``***`` however it is rendered (spec 4.2)."""

from __future__ import annotations

from typing import Any

_MASK = "***"


class Secret:
    """Wraps a password or token. The value leaves only through :meth:`reveal`, which the AUTH
    encoder alone calls. ``repr``, ``str``, ``format`` and pickle never carry it."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return _MASK

    def __str__(self) -> str:
        return _MASK

    def __format__(self, format_spec: str) -> str:
        return format(_MASK, format_spec)

    def __bool__(self) -> bool:
        return bool(self._value)

    def __reduce__(self) -> tuple[type[Secret], tuple[str]]:
        return (Secret, (_MASK,))

    def __copy__(self) -> Secret:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> Secret:
        return self
