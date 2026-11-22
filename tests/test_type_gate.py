# SPDX-License-Identifier: Apache-2.0
"""``tests/types/`` is only worth anything if both type checkers read it strictly, and a wrong call
is only pinned if an ``ignore`` that stops hiding an error turns the run red. So the checkers are
run over ``tests/types/`` from inside the suite, and a loosened sample proves they bite.

``tests/types/pyrightconfig.json`` differs from the repository's pyright settings on purpose:
``# type: ignore`` belongs to mypy there (with it honoured, a loosened signature would still pass
pyright) and an unnecessary ignore is an error."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TYPES = ROOT / "tests" / "types"

MYPY = shutil.which("mypy")
PYRIGHT = shutil.which("pyright")
needs_checkers = pytest.mark.skipif(
    MYPY is None or PYRIGHT is None, reason="mypy and pyright are not installed here"
)


def mypy(*paths: Path, mypypath: str = "") -> subprocess.CompletedProcess[str]:
    assert MYPY is not None
    return subprocess.run(
        [MYPY, "--strict", "--python-executable", sys.executable, *map(str, paths)],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={**os.environ, "MYPYPATH": mypypath},
        timeout=300,
    )


def pyright(project: Path) -> subprocess.CompletedProcess[str]:
    assert PYRIGHT is not None
    return subprocess.run(
        [PYRIGHT, "-p", str(project)],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=300,
    )


@needs_checkers
def test_the_type_tests_pass_mypy_strict() -> None:
    done = mypy(TYPES, mypypath=str(ROOT / "src"))
    assert done.returncode == 0, done.stdout


@needs_checkers
def test_the_type_tests_pass_pyright_strict_with_unused_ignores_an_error() -> None:
    config = json.loads((TYPES / "pyrightconfig.json").read_text())
    assert config["typeCheckingMode"] == "strict"
    assert config["enableTypeIgnoreComments"] is False
    assert config["reportUnnecessaryTypeIgnoreComment"] is True
    done = pyright(TYPES)
    assert done.returncode == 0, done.stdout


SAMPLE = '''\
"""A sample whose "expected errors" no longer are errors: both checkers must refuse it."""

from typing import assert_type


class Bucket:
    def get(self, key: str) -> dict[str, int] | None:  # was meant to be absent
        return None

    def put(self, key: int) -> None:  # the argument type was loosened from str to int
        return None


def check(bucket: Bucket) -> None:
    bucket.get("k")  # type: ignore[attr-defined]  # pyright: ignore[@ATTR@]
    bucket.put(1)  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]
    assert_type(bucket.put(2), None)
'''


@needs_checkers
def test_an_ignore_that_no_longer_hides_an_error_turns_both_checkers_red(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text(LOOSENED)
    shutil.copy(TYPES / "pyrightconfig.json", tmp_path / "pyrightconfig.json")
    (tmp_path / "pyrightconfig.json").write_text(
        json.dumps({**json.loads((TYPES / "pyrightconfig.json").read_text()), "extraPaths": []})
    )
    seen_by_mypy = mypy(tmp_path / "sample.py")
    assert seen_by_mypy.returncode != 0
    assert seen_by_mypy.stdout.count('Unused "type: ignore"') == 2, seen_by_mypy.stdout
    seen_by_pyright = pyright(tmp_path)
    assert seen_by_pyright.returncode != 0
    assert seen_by_pyright.stdout.count("Unnecessary") >= 2, seen_by_pyright.stdout


LOOSENED = SAMPLE.replace("@ATTR@", "reportAttributeAccessIssue, reportUnknownMemberType")
RESTORED = (
    LOOSENED.replace(
        "    def get(self, key: str) -> dict[str, int] | None:  # was meant to be absent\n"
        "        return None\n\n",
        "",
    )
    .replace("def put(self, key: int)", "def put(self, key: str)")
    .replace("put(2)", 'put("k")')
)


@needs_checkers
def test_the_same_sample_with_the_errors_restored_is_clean(tmp_path: Path) -> None:
    """The verb is gone and the argument is a str again: now the ignores hide real errors."""
    (tmp_path / "sample.py").write_text(RESTORED)
    config = json.loads((TYPES / "pyrightconfig.json").read_text())
    (tmp_path / "pyrightconfig.json").write_text(json.dumps({**config, "extraPaths": []}))
    done = mypy(tmp_path / "sample.py")
    assert done.returncode == 0, done.stdout
    seen = pyright(tmp_path)
    assert seen.returncode == 0, seen.stdout
