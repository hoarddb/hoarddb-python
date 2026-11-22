# SPDX-License-Identifier: Apache-2.0
"""P2: Secret prints *** everywhere and carries nothing through pickle or copy."""

from __future__ import annotations

import copy
import pickle

from hoarddb._secret import Secret

PW = "hunter2-correct-horse"


def test_every_rendering_is_redacted() -> None:
    s = Secret(PW)
    for rendered in (
        repr(s),
        str(s),
        f"{s}",
        f"{s!r}",
        "%s" % s,  # noqa: UP031 - the percent form is one of the renderings under test
        format(s, ">20"),
        repr([s]),
    ):
        assert PW not in rendered
        assert "***" in rendered


def test_reveal_is_the_one_way_out() -> None:
    assert Secret(PW).reveal() == PW


def test_a_pickle_does_not_carry_the_value() -> None:
    blob = pickle.dumps(Secret(PW))
    assert PW.encode() not in blob
    assert "***" in pickle.loads(blob).reveal()


def test_copying_keeps_the_value_and_the_object_stays_inert() -> None:
    s = Secret(PW)
    assert copy.copy(s) is s
    assert copy.deepcopy({"k": s})["k"] is s
    assert not hasattr(s, "__dict__")


def test_truthiness_follows_the_value_but_length_is_not_exposed() -> None:
    assert Secret("x")
    assert not Secret("")
    assert not hasattr(Secret("x"), "__len__")
