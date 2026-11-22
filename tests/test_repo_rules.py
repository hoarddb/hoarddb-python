# SPDX-License-Identifier: Apache-2.0
"""Repository rules (spec 9.2, 9.3): SPDX headers, boundary, product names, metadata."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import lint

ROOT = Path(__file__).resolve().parent.parent


def test_every_python_file_carries_the_spdx_header() -> None:
    assert lint.check_spdx(ROOT) == []


def test_no_other_database_product_is_named() -> None:
    assert lint.check_product_names(ROOT) == []


def test_package_imports_only_the_standard_library_and_itself() -> None:
    assert lint.check_boundary(ROOT) == []


def test_the_core_has_no_sockets_clocks_threads_or_loops() -> None:
    assert lint.check_core_purity(ROOT) == []


def test_the_core_purity_check_catches_a_clock(tmp_path: Path) -> None:
    core = tmp_path / "src" / "hoarddb" / "_core"
    core.mkdir(parents=True)
    (core / "x.py").write_text(
        "# SPDX-License-Identifier: Apache-2.0\nimport time\nfrom socket import socket\n"
    )
    assert lint.check_core_purity(tmp_path) == [
        "src/hoarddb/_core/x.py:2: the sans-I/O core imports 'time'",
        "src/hoarddb/_core/x.py:3: the sans-I/O core imports 'socket'",
    ]


def test_the_lint_catches_what_it_claims_to_catch(tmp_path: Path) -> None:
    pkg = tmp_path / "src" / "hoarddb"
    pkg.mkdir(parents=True)
    header = "# SPDX-License-Identifier: Apache-2.0\n"
    (pkg / "a.py").write_text("import requests\nfrom hoarddb import x\nimport os\n")
    (pkg / "b.py").write_text(header + "x = 1\n")
    banned = "mon" + "go"
    (pkg / "c.py").write_text(header + f"# a {banned} note\n# redistribute is fine\n")
    assert lint.check_spdx(tmp_path) == ["src/hoarddb/a.py: first line must be the SPDX header"]
    assert lint.check_boundary(tmp_path) == [
        "src/hoarddb/a.py:1: import of non-standard-library module 'requests'"
    ]
    assert lint.check_product_names(tmp_path) == [
        "src/hoarddb/c.py:2: names another database product"
    ]


def test_pyproject_declares_no_runtime_dependency() -> None:
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text())
    project = meta["project"]
    assert project["dependencies"] == []
    assert project["license"] == "Apache-2.0"
    assert project["requires-python"] == ">=3.11"
    assert "Private :: Do Not Upload" in project["classifiers"]
    assert meta["build-system"]["requires"] == ["hatchling>=1.26.3"]


def test_the_package_ships_py_typed() -> None:
    assert (ROOT / "src" / "hoarddb" / "py.typed").exists()


@pytest.mark.skipif(
    importlib.util.find_spec("hatchling") is None or importlib.util.find_spec("build") is None,
    reason="hatchling and build are not installed here (no package index); the distributions "
    "were not built. Run `pip install 'hatchling>=1.26.3' build`, then "
    "`python scripts/smoke_dist.py --build`",
)
def test_the_built_distributions_pass_the_smoke(tmp_path: Path) -> None:
    """Build the wheel and the sdist, check both, rebuild a wheel from the sdist, and install the
    wheel into a clean virtual environment (scripts/smoke_dist.py)."""
    done = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "smoke_dist.py"), "--build"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=900,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_pyproject_metadata_for_the_distributions() -> None:
    """What needs no build backend: the metadata the wheel and sdist are built from."""
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text())
    project = meta["project"]
    assert project["dependencies"] == []
    assert "optional-dependencies" not in project
    assert project["license"] == "Apache-2.0" and project["license-files"] == ["LICENSE"]
    assert project["requires-python"] == ">=3.11"
    assert "Private :: Do Not Upload" in project["classifiers"]
    assert "Typing :: Typed" in project["classifiers"]
    assert meta["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["src/hoarddb"]
    assert meta["tool"]["hatch"]["version"]["path"] == "src/hoarddb/_version.py"
    assert (ROOT / "LICENSE").is_file() and (ROOT / "src" / "hoarddb" / "py.typed").is_file()


def test_the_changelog_records_the_timeout_refusal() -> None:
    changelog = ROOT / "CHANGELOG.md"
    assert changelog.exists(), "spec 9.1 wants a CHANGELOG.md; create it at the repository root"
    text = changelog.read_text(encoding="utf-8")
    assert "timeout=0" in text and "InvalidArgumentError" in text
    assert "request_timeout=0" in text and "ConfigError" in text
