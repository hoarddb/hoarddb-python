# SPDX-License-Identifier: Apache-2.0
"""Security-review fixes in the BSON encoder: M4 (str subclasses), L1 (Opaque), L2 (cycles)."""

from __future__ import annotations

import enum
import inspect
import struct
import sys
from collections.abc import Iterator, Mapping
from typing import Any

import pytest

from hoarddb import Opaque, errors
from hoarddb._bson import deserialize, encode, serialize

# --- M4 ----------------------------------------------------------------------------------------


class Color(str, enum.Enum):  # noqa: UP042 - the (str, Enum) shape is the case under test
    RED = "red"


class SelfStr(str):
    """A str subclass whose ``__str__`` returns itself: ``str(value)`` recurses for ever."""

    __slots__ = ()

    def __str__(self) -> str:
        return self  # pyright: ignore[reportReturnType]


def test_a_str_enum_member_is_written_as_its_value_not_its_name() -> None:
    assert deserialize(serialize({"c": Color.RED})) == {"c": "red"}


def test_a_str_subclass_whose_str_returns_itself_is_written_as_its_text() -> None:
    assert deserialize(serialize({"s": SelfStr("hello")})) == {"s": "hello"}


# --- L1 ----------------------------------------------------------------------------------------

I32 = struct.Struct("<i").pack


@pytest.mark.parametrize(
    ("opaque", "needle"),
    [
        (Opaque("null", "zz"), "bytes"),  # type: ignore[arg-type]
        (Opaque("null", b"\x10a\x00\x01\x00\x00\x00"), "extra"),  # smuggles an element
        (Opaque("int32", b"\x01\x00\x00\x00\x00"), "extra"),
        (Opaque("int32", b"\x01"), "truncated"),
        (Opaque("objectid", bytes(11)), "truncated"),
        (Opaque("string", I32(99) + b"abc\x00"), "does not fit"),
        (Opaque("bool", b"\x02"), "only 0 and 1"),
        (Opaque("document", I32(5) + b"\x00" + b"\x10a\x00\x01\x00\x00\x00"), "extra"),
        (Opaque("datetime", bytearray(8)), "bytes"),  # type: ignore[arg-type]
    ],
)
def test_a_bad_opaque_is_an_invalid_argument_error_that_says_what_is_wrong(
    opaque: Opaque, needle: str
) -> None:
    with pytest.raises(errors.InvalidArgumentError) as caught:
        serialize({"x": opaque})
    assert "Opaque" in str(caught.value)
    assert needle in str(caught.value)
    assert 'field "x"' in str(caught.value)


def test_an_opaque_the_decoder_produced_still_round_trips() -> None:
    doc = {"k": Opaque("objectid", bytes(range(12))), "n": Opaque("minkey", b"")}
    assert deserialize(serialize(doc)) == doc


# --- L2 ----------------------------------------------------------------------------------------


class Box(Mapping[str, Any]):
    def __init__(self) -> None:
        self.inner: dict[str, Any] = {}

    def __getitem__(self, key: str) -> Any:
        return self.inner[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.inner)

    def __len__(self) -> int:
        return len(self.inner)


class Items(list[Any]):
    pass


def test_a_cyclic_dict_names_the_cycle_not_a_hundred_segment_path() -> None:
    doc: dict[str, Any] = {}
    doc["a"] = {"b": doc}
    with pytest.raises(errors.InvalidArgumentError) as caught:
        serialize(doc)
    message = str(caught.value)
    assert "cycle" in message
    assert 'field "a.b"' in message


def test_a_cyclic_mapping_subclass_is_named_not_a_recursion_error() -> None:
    box = Box()
    box.inner["me"] = box
    with pytest.raises(errors.InvalidArgumentError, match="cycle"):
        serialize({"top": box})


def test_a_cyclic_list_subclass_is_named_not_a_recursion_error() -> None:
    items = Items()
    items.append(items)
    with pytest.raises(errors.InvalidArgumentError, match=r"cycle"):
        serialize({"xs": items})


def test_a_cyclic_plain_list_is_named() -> None:
    items: list[Any] = [1]
    items.append(items)
    with pytest.raises(errors.InvalidArgumentError, match=r'field "xs\[1\]".*cycle|cycle'):
        serialize({"xs": items})


def test_the_same_container_twice_is_not_a_cycle() -> None:
    shared = {"v": 1}
    assert deserialize(serialize({"a": shared, "b": shared, "c": [shared, shared]})) == {
        "a": {"v": 1},
        "b": {"v": 1},
        "c": [{"v": 1}, {"v": 1}],
    }


def test_a_recursion_error_inside_serialize_is_an_invalid_argument_error() -> None:
    nested: dict[str, Any] = {}
    cursor = nested
    for _ in range(60):
        cursor["n"] = {}
        cursor = cursor["n"]

    def descend(room: int) -> None:
        if room:
            descend(room - 1)
            return
        with pytest.raises(errors.InvalidArgumentError, match=r"too deeply|nested"):
            serialize(nested)

    # leave only a few frames of room so the encoder itself runs out
    depth = len(inspect.stack(0))
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(depth + 60)
    try:
        descend(sys.getrecursionlimit() - depth - 45 - 12)
    finally:
        sys.setrecursionlimit(limit)


# --- L12: a length that does not fit the 32-bit prefix is the driver's error, not struct.error ---


@pytest.mark.parametrize(
    ("value", "word"),
    [
        ("x" * 100, "string"),
        (b"y" * 100, "binary"),
        (list(range(40)), "array"),
        (bytearray(100), "binary"),
        ({"k": "z" * 100}, "string"),
    ],
)
def test_a_value_over_the_32_bit_length_is_an_invalid_argument_error(
    monkeypatch: pytest.MonkeyPatch, value: object, word: str
) -> None:
    # 2 GiB would be needed for real; the limit is a module constant so the check is testable
    monkeypatch.setattr(encode, "_MAX_DOCUMENT", 64)
    with pytest.raises(errors.InvalidArgumentError, match=word) as caught:
        serialize({"v": value})
    assert "limit" in str(caught.value)
    assert 'field "v' in str(caught.value)


def test_a_value_exactly_at_the_limit_is_still_written(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(encode, "_MAX_DOCUMENT", 4 + 1 + 2 + 4 + 5 + 1)  # {"v": "abcd"}
    assert deserialize(serialize({"v": "abcd"})) == {"v": "abcd"}
