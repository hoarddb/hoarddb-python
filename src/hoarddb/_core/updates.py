# SPDX-License-Identifier: Apache-2.0
"""``update``'s nine operators, validated before anything is sent (spec 4.7).

The keys are hoardDB's operator spellings. ``$unset`` is a *list* of names, not a mapping;
``$inc`` is int64 and ``$mul`` is double, always; an unknown key is refused naming the nine. The
server applies the operators in the order of the table and the whole update is all or nothing.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any, Final, cast

from hoarddb import errors

OPERATORS: Final = (
    "$set",
    "$unset",
    "$inc",
    "$push",
    "$addToSet",
    "$mul",
    "$min",
    "$max",
    "$rename",
)
_WIRE: Final = {
    "$set": "set",
    "$unset": "unset",
    "$inc": "inc",
    "$push": "push",
    "$addToSet": "add_to_set",
    "$mul": "mul",
    "$min": "min",
    "$max": "max",
    "$rename": "rename",
}
_INT64: Final = range(-(2**63), 2**63)


def _bad(message: str, operation: str) -> errors.InvalidArgumentError:
    return errors.InvalidArgumentError(f"{message} Nothing was sent.", operation=operation)


def _mapping(op: str, value: Any, operation: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _bad(
            f"{op} takes a mapping of field name to value, not a {type(value).__name__}.",
            operation,
        )
    for key in value:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(key, str) or not key:
            raise _bad(f"{op} needs non-empty string field names, not {key!r}.", operation)
    return value  # pyright: ignore[reportUnknownVariableType]


def update_fields(ops: object, *, operation: str = "update") -> dict[str, Any]:
    """The ``UpdateRequest`` keyword arguments for ``ops``; raises ``InvalidArgumentError``."""
    if not isinstance(ops, Mapping):
        raise _bad(
            f"update takes a mapping of operators, not a {type(ops).__name__}; for example "
            '{"$set": {"city": "London"}, "$inc": {"logins": 1}}.',
            operation,
        )
    if not ops:
        raise _bad(
            "update was given no operator. Give at least one of " + ", ".join(OPERATORS) + ".",
            operation,
        )
    out: dict[str, Any] = {}
    for op, value in ops.items():  # pyright: ignore[reportUnknownVariableType]
        if op not in _WIRE:
            raise _bad(
                f"{op!r} is not an update operator; the nine are " + ", ".join(OPERATORS) + ".",
                operation,
            )
        out[_WIRE[op]] = _checked(op, value, operation)  # pyright: ignore[reportUnknownArgumentType]
    return out


def _checked(op: str, value: Any, operation: str) -> Any:
    if op == "$unset":
        if isinstance(value, str | bytes | Mapping) or not isinstance(value, Sequence):
            raise _bad(
                '$unset takes a list of field names, for example {"$unset": ["old_field"]}; '
                f"a {type(cast(object, value)).__name__} is not one.",
                operation,
            )
        names: list[Any] = list(value)  # pyright: ignore[reportUnknownArgumentType]
        if not all(isinstance(n, str) and n for n in names):
            raise _bad("$unset needs non-empty string field names.", operation)
        return names
    fields = _mapping(op, value, operation)
    if op == "$inc":
        for name, amount in fields.items():
            if isinstance(amount, bool) or not isinstance(amount, int):
                raise _bad(
                    f"$inc adds a whole number (int64), but {name!r} is a "
                    f"{type(amount).__name__}; use $mul for a fraction.",
                    operation,
                )
            if amount not in _INT64:
                raise _bad(f"$inc of {name!r} is past the 64-bit range.", operation)
    elif op == "$mul":
        for name, factor in fields.items():
            if isinstance(factor, bool) or not isinstance(factor, int | float):
                raise _bad(
                    f"$mul multiplies by a number, but {name!r} is a {type(factor).__name__}.",
                    operation,
                )
            if not math.isfinite(factor):
                raise _bad(f"$mul by {factor!r} for {name!r} is not a finite number.", operation)
    elif op == "$rename":
        for old, new in fields.items():
            if not isinstance(new, str) or not new:
                raise _bad(
                    f"$rename maps a field to a new name (a non-empty string), but {old!r} maps "
                    f"to {new!r}.",
                    operation,
                )
    return dict(fields)
