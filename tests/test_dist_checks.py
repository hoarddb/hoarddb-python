# SPDX-License-Identifier: Apache-2.0
# pyright: reportUnknownLambdaType=false, reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false
"""The wheel and sdist checks of ``scripts/smoke_dist.py``, run on small distributions made in the
test (no build backend needed): a good one passes and each defect is named. The real thing, built
by ``python -m build``, is ``test_repo_rules.py::test_the_built_distributions_pass_the_smoke``."""

from __future__ import annotations

import io
import tarfile
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

import smoke_dist

GOOD_METADATA = """\
Metadata-Version: 2.4
Name: hoarddb
Version: 0.0.0
License-Expression: Apache-2.0
Classifier: Private :: Do Not Upload
Requires-Python: >=3.11
"""
SPDX = "# SPDX-License-Identifier: Apache-2.0\n"


def wheel(
    tmp: Path,
    *,
    name: str = "hoarddb-0.0.0-py3-none-any.whl",
    metadata: str = GOOD_METADATA,
    drop: tuple[str, ...] = (),
    extra: dict[str, str] | None = None,
) -> Path:
    files = {
        "hoarddb/__init__.py": SPDX + "x = 1\n",
        "hoarddb/__main__.py": SPDX,
        "hoarddb/py.typed": "",
        "hoarddb-0.0.0.dist-info/METADATA": metadata,
        "hoarddb-0.0.0.dist-info/licenses/LICENSE": "Apache License\n",
        **(extra or {}),
    }
    path = tmp / name
    with zipfile.ZipFile(path, "w") as archive:
        for member, content in files.items():
            if member not in drop:
                archive.writestr(member, content)
    return path


def sdist(tmp: Path, *, metadata: str = GOOD_METADATA, drop: tuple[str, ...] = ()) -> Path:
    files = {
        "PKG-INFO": metadata,
        "pyproject.toml": "[project]\n",
        "LICENSE": "Apache License\n",
        "README.md": "# hoarddb\n",
        "src/hoarddb/py.typed": "",
    }
    path = tmp / "hoarddb-0.0.0.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for member, content in files.items():
            if member in drop:
                continue
            data = content.encode()
            info = tarfile.TarInfo(f"hoarddb-0.0.0/{member}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return path


def test_a_good_wheel_and_a_good_sdist_pass(tmp_path: Path) -> None:
    assert smoke_dist.check_wheel(wheel(tmp_path)) == []
    assert smoke_dist.check_sdist(sdist(tmp_path)) == []


@pytest.mark.parametrize(
    ("what", "make", "needle"),
    [
        (
            "a platform tag",
            lambda t: wheel(t, name="hoarddb-0.0.0-cp313-cp313-linux_x86_64.whl"),
            "py3-none-any",
        ),
        (
            "a dependency",
            lambda t: wheel(t, metadata=GOOD_METADATA + "Requires-Dist: requests\n"),
            "Requires-Dist",
        ),
        ("no py.typed", lambda t: wheel(t, drop=("hoarddb/py.typed",)), "py.typed"),
        ("no __main__", lambda t: wheel(t, drop=("hoarddb/__main__.py",)), "__main__.py"),
        (
            "no licence",
            lambda t: wheel(t, drop=("hoarddb-0.0.0.dist-info/licenses/LICENSE",)),
            "LICENSE",
        ),
        (
            "the private classifier gone",
            lambda t: wheel(
                t, metadata=GOOD_METADATA.replace("Classifier: Private :: Do Not Upload\n", "")
            ),
            "Do Not Upload",
        ),
        (
            "another licence",
            lambda t: wheel(t, metadata=GOOD_METADATA.replace("Apache-2.0", "GPL-3.0")),
            "not Apache-2.0",
        ),
        (
            "an old python floor",
            lambda t: wheel(t, metadata=GOOD_METADATA.replace(">=3.11", ">=3.9")),
            "Requires-Python",
        ),
        ("a shipped test", lambda t: wheel(t, extra={"tests/test_x.py": SPDX}), "must not ship"),
        (
            "a file with no SPDX line",
            lambda t: wheel(t, extra={"hoarddb/bad.py": "x = 1\n"}),
            "SPDX",
        ),
    ],
)
def test_each_wheel_defect_is_named(
    what: str, make: Callable[[Path], Path], needle: str, tmp_path: Path
) -> None:
    problems = smoke_dist.check_wheel(make(tmp_path))
    assert any(needle in p for p in problems), (what, problems)


@pytest.mark.parametrize(
    ("what", "make", "needle"),
    [
        (
            "a dependency",
            lambda t: sdist(t, metadata=GOOD_METADATA + "Requires-Dist: requests\n"),
            "Requires-Dist",
        ),
        ("no licence", lambda t: sdist(t, drop=("LICENSE",)), "LICENSE"),
        ("no py.typed", lambda t: sdist(t, drop=("src/hoarddb/py.typed",)), "py.typed"),
        ("no pyproject", lambda t: sdist(t, drop=("pyproject.toml",)), "pyproject.toml"),
    ],
)
def test_each_sdist_defect_is_named(
    what: str, make: Callable[[Path], Path], needle: str, tmp_path: Path
) -> None:
    problems = smoke_dist.check_sdist(make(tmp_path))
    assert any(needle in p for p in problems), (what, problems)
