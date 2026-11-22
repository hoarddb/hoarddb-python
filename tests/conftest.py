# SPDX-License-Identifier: Apache-2.0
"""Fixtures shared by the runner and cursor tests."""

from __future__ import annotations

import pytest

import faketls


@pytest.fixture(params=["sync", "async"])
def mode(request: pytest.FixtureRequest) -> str:
    """Run the test once with the blocking runner and once with the asyncio runner."""
    return str(request.param)


@pytest.fixture(scope="module")
def identity(tmp_path_factory: pytest.TempPathFactory) -> faketls.Identity:
    return faketls.make_identity(tmp_path_factory.mktemp("id"), "shared")
