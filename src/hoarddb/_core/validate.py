# SPDX-License-Identifier: Apache-2.0
"""Local validation shared by connect options and per-call arguments (spec O-8).

Every refusal here happens before anything is sent, says so ("Nothing was sent."), and names the
fix. The server still refuses what it refuses; this only catches what is certainly wrong.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from hoarddb import errors

WRITE_CONCERNS: Final = ("one", "majority", "all")
MAX_TIMEOUT_SECONDS: Final = 3600.0  # an hour, as for dial_timeout; more is a typo
STORE_TYPES: Final = ("hash", "btree", "fifo", "lifo", "heap", "blob")


def check_write_concern(value: str, *, operation: str = "connect") -> None:
    if value not in WRITE_CONCERNS:
        raise errors.InvalidArgumentError(
            f'write_concern "{value}" is not one of: {", ".join(WRITE_CONCERNS)}. '
            "Nothing was sent.",
            operation=operation,
        )


def check_timeout(value: object, *, operation: str) -> None:
    """A per-call ``timeout=``: a positive, finite number of seconds, at most an hour (or None).

    ``inf``, ``nan``, zero, a negative and an absurdly large number are refused here, before a
    deadline is built from them (``socket.settimeout`` would raise ``OverflowError`` for 1e300).
    """
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not value > 0
        or value > MAX_TIMEOUT_SECONDS
    ):
        raise errors.InvalidArgumentError(
            f"timeout= must be a positive number of seconds, at most {MAX_TIMEOUT_SECONDS:g} "
            f"(one hour), got {value!r}. Pass for example timeout=5.0, or leave it out to use "
            "the client's request_timeout. Nothing was sent.",
            operation=operation,
        )


def check_name(kind: str, value: object, *, operation: str) -> str:
    """A database, bucket or key name: a non-empty string."""
    if not isinstance(value, str) or not value:
        shown = "an empty string" if value == "" else f"a {type(value).__name__}"
        raise errors.InvalidArgumentError(
            f"The {kind} name must be a non-empty string, not {shown}. Nothing was sent.",
            operation=operation,
        )
    return value


def store_type_word(value: object, *, operation: str) -> str:
    """The wire word for a store type given as a ``StoreType`` or a string (``StoreType`` is a
    ``StrEnum``, so its members are strings). Any other spelling (``"fifoStore"``) names the six."""
    if isinstance(value, str) and str.__str__(value) in STORE_TYPES:
        return str.__str__(value)
    raise errors.InvalidArgumentError(
        f"{value!r} is not a store type; the six are {', '.join(STORE_TYPES)} (spelled without a "
        '"Store" suffix). Nothing was sent.',
        operation=operation,
    )


def check_filter(value: object, *, operation: str) -> None:
    """A filter is a mapping (or None). Its operators are hoardDB's own; an unknown one is passed
    through so the server's refusal, which already names it, reaches the caller."""
    if value is not None and not isinstance(value, dict | Mapping):
        raise errors.InvalidArgumentError(
            "A filter is a mapping such as {'Field': 'computing'}, not a "
            f"{type(value).__name__}. Nothing was sent.",
            operation=operation,
        )


def check_sort(value: object, *, operation: str) -> None:
    """``sort`` has exactly one field, 1 (ascending) or -1 (descending)."""
    if value is None:
        return
    if not isinstance(value, Mapping) or len(value) != 1:  # pyright: ignore[reportUnknownArgumentType]
        raise errors.InvalidArgumentError(
            "sort takes exactly one field, for example sort={'Born': -1} (1 ascending, -1 "
            "descending); the server sorts by one field. Nothing was sent.",
            operation=operation,
        )
    ((field, direction),) = value.items()  # pyright: ignore[reportUnknownVariableType]
    if not isinstance(field, str) or not field:
        raise errors.InvalidArgumentError(
            "The sort field must be a non-empty string. Nothing was sent.", operation=operation
        )
    if isinstance(direction, bool) or direction not in (1, -1):
        raise errors.InvalidArgumentError(
            f"The sort direction for {field!r} must be 1 (ascending) or -1 (descending), not "
            f"{direction!r}. Nothing was sent.",
            operation=operation,
        )


def check_projection(value: object, *, operation: str) -> None:
    """``projection`` maps field names to 1 (keep) or 0 (drop)."""
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise errors.InvalidArgumentError(
            f"A projection is a mapping such as {{'Name': 1}}, not a {type(value).__name__}. "
            "Nothing was sent.",
            operation=operation,
        )
    for field, flag in value.items():  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(field, str) or isinstance(flag, bool) or flag not in (0, 1):
            raise errors.InvalidArgumentError(
                f"A projection maps a field name to 1 (keep) or 0 (drop); {field!r}: {flag!r} is "
                "neither. Nothing was sent.",
                operation=operation,
            )


def check_count(name: str, value: object, *, minimum: int, operation: str) -> int:
    """A whole number of at least ``minimum`` (``skip``, ``limit``, ``page_size``)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise errors.InvalidArgumentError(
            f"{name} must be a whole number of at least {minimum}, not {value!r}. "
            "Nothing was sent.",
            operation=operation,
        )
    return value
