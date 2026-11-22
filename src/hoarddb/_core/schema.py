# SPDX-License-Identifier: Apache-2.0
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false
"""One encode/decode engine for every wire message, driven by the dataclass definitions.

A message is a frozen ``dataclass``. A field's wire name is its attribute name unless it says
otherwise with :func:`wire` (``write_concern`` is ``writeconcern``, ``blob_id`` is ``blobid``): the
names are written out once, next to the field, and pinned by the vectors. Decoding ignores unknown
fields, treats an absent or null field as its default, accepts any integer width, and refuses a
known field of the wrong type with a ``ProtocolError`` that names it.
"""

from __future__ import annotations

import dataclasses
import struct
import types
import typing
from collections.abc import Mapping
from typing import Any, TypeVar, dataclass_transform

from hoarddb import errors
from hoarddb._bson import Opaque
from hoarddb._secret import Secret

T = TypeVar("T")

_MISSING: Any = dataclasses.MISSING


def wire(
    name: str | None = None,
    *,
    default: Any = _MISSING,
    default_factory: Any = _MISSING,
    always: bool = False,
    conv: str | None = None,
) -> Any:
    """Field metadata. ``name``: the wire name. ``always``: send even when empty (an empty
    document, a zero ``priority``). ``conv``: ``"int64"`` writes an int as int64, ``"int64_values"``
    and ``"double_values"`` do the same for the values of a document (``$inc``, ``$mul``)."""
    meta = {"wire": name, "always": always, "conv": conv}
    if default_factory is not _MISSING:
        return dataclasses.field(default_factory=default_factory, metadata=meta)
    if default is not _MISSING:
        return dataclasses.field(default=default, metadata=meta)
    return dataclasses.field(metadata=meta)


@dataclass_transform(
    frozen_default=True, kw_only_default=True, field_specifiers=(wire, dataclasses.field)
)
def model(cls: type[T]) -> type[T]:
    """Make ``cls`` a frozen, slotted, keyword-only dataclass (how every message is declared)."""
    return dataclasses.dataclass(frozen=True, slots=True, kw_only=True)(cls)


def _wire_name(f: dataclasses.Field[Any]) -> str:
    return f.metadata.get("wire") or f.name


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return not value
    if isinstance(value, int | float):
        return value == 0
    if isinstance(value, str | bytes | list | tuple | dict):
        return len(value) == 0
    return False


def _encode(value: Any, conv: str | None) -> Any:
    if isinstance(value, Secret):
        return value.reveal()  # the one place a Secret leaves its wrapper
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return to_wire(value)
    if conv == "int64" and isinstance(value, int) and not isinstance(value, bool):
        return Opaque("int64", struct.pack("<q", value))
    if conv == "int64_values":
        document = typing.cast("Mapping[str, Any]", value)
        return {
            k: Opaque("int64", struct.pack("<q", v))
            if isinstance(v, int) and not isinstance(v, bool)
            else v
            for k, v in document.items()
        }
    if conv == "double_values":
        document = typing.cast("Mapping[str, Any]", value)
        return {
            k: float(v) if isinstance(v, int | float) and not isinstance(v, bool) else v
            for k, v in document.items()
        }
    if isinstance(value, list | tuple):
        return [_encode(v, None) for v in value]
    return value


def to_wire(message: Any) -> dict[str, Any]:
    """The message as a wire document: empty fields are omitted unless the field says ``always``."""
    out: dict[str, Any] = {}
    for f in dataclasses.fields(message):
        value = getattr(message, f.name)
        if _is_empty(value) and not f.metadata.get("always"):
            continue
        out[_wire_name(f)] = _encode(value, f.metadata.get("conv"))
    return out


# --- decoding ----------------------------------------------------------------------------------

_specs: dict[type, list[tuple[str, str, Any]]] = {}


def _spec(cls: type) -> list[tuple[str, str, Any]]:
    cached = _specs.get(cls)
    if cached is None:
        hints = typing.get_type_hints(cls)
        cached = [(f.name, _wire_name(f), hints[f.name]) for f in dataclasses.fields(cls)]
        _specs[cls] = cached
    return cached


def _bad(where: str, hint: Any, value: Any) -> errors.ProtocolError:
    wanted = getattr(hint, "__name__", str(hint))
    return errors.ProtocolError(
        f"The server sent {where} as {type(value).__name__}, expected {wanted}. "
        "The connection was discarded.",
        operation="receive",
    )


def _coerce(hint: Any, value: Any, where: str) -> Any:
    if hint is Any:
        return value
    origin = typing.get_origin(hint)
    if origin is typing.Union or origin is types.UnionType:
        options = [a for a in typing.get_args(hint) if a is not type(None)]
        if value is None:
            return None
        for option in options:
            try:
                return _coerce(option, value, where)
            except errors.ProtocolError:
                continue
        raise _bad(where, hint, value)
    if origin is list:
        if not isinstance(value, list):
            raise _bad(where, hint, value)
        (item,) = typing.get_args(hint)
        return [_coerce(item, v, where) for v in value]
    if origin is dict:
        if not isinstance(value, dict):
            raise _bad(where, hint, value)
        return value
    if hint is bool:
        if not isinstance(value, bool):
            raise _bad(where, hint, value)
        return value
    if hint is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise _bad(where, hint, value)
        return value
    if hint is float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise _bad(where, hint, value)
        return float(value)
    if hint is str or hint is bytes:
        if not isinstance(value, hint):
            raise _bad(where, hint, value)
        return value
    if hint is Secret:
        if not isinstance(value, str):
            raise _bad(where, hint, value)
        return Secret(value)
    if dataclasses.is_dataclass(hint):
        if not isinstance(value, dict):
            raise _bad(where, hint, value)
        return from_wire(typing.cast("type[Any]", hint), value)
    return value


def from_wire(cls: type[T], doc: Mapping[str, Any]) -> T:
    """Build a message from a decoded wire document. Unknown fields are ignored."""
    kwargs: dict[str, Any] = {}
    for attr, wire_name, hint in _spec(cls):
        value = doc.get(wire_name)
        if value is None:
            continue
        kwargs[attr] = _coerce(hint, value, f"{cls.__name__}.{wire_name}")
    return cls(**kwargs)


# --- introspection (tests): which wire names a message models ----------------------------------


def wire_paths(cls: type) -> tuple[set[str], set[str]]:
    """``(paths, raw)``: every modelled wire path (``entries[].key``), and the paths whose content
    is kept as a plain document (anything below them is covered)."""
    paths: set[str] = set()
    raw: set[str] = set()

    def visit(c: type, prefix: str) -> None:
        for _, wire_name, hint in _spec(c):
            path = prefix + wire_name
            paths.add(path)
            inner = hint
            origin = typing.get_origin(inner)
            if origin is typing.Union or origin is types.UnionType:
                args = [a for a in typing.get_args(inner) if a is not type(None)]
                inner = args[0] if len(args) == 1 else Any
            list_of = False
            if typing.get_origin(inner) is list:
                (inner,) = typing.get_args(inner)
                list_of = True
            if dataclasses.is_dataclass(inner) and isinstance(inner, type):
                visit(inner, path + ("[]." if list_of else "."))
            elif inner is Any or typing.get_origin(inner) is dict:
                raw.add(path)

    visit(cls, "")
    return paths, raw
