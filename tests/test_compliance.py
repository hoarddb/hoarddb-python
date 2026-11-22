# SPDX-License-Identifier: Apache-2.0
"""P20, the compliance gate (spec 9.2, 9.3, 11.3): each rule is a test, and for each the guard is
shown to bite on a small tree made in the test. ``docs/compliance.md`` lists the evidence."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import licences
import lint

ROOT = Path(__file__).resolve().parent.parent
HEADER = "# SPDX-License-Identifier: Apache-2.0\n"
BANNED = "post" + "gres"  # assembled: this file must not trip the very grep it tests


def tree(tmp: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = tmp / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tmp


# -- the SPDX header: every source file, the examples, the scripts and __main__ -----------------


def test_the_repository_passes_the_spdx_check_and_the_check_covers_what_it_must() -> None:
    assert lint.check_spdx(ROOT) == []
    covered = {p.relative_to(ROOT).as_posix() for p in lint.source_files(ROOT)}
    for must in (
        "src/hoarddb/__main__.py",
        "examples/hash.py",
        "examples/quickstart.py",
        "scripts/lint.py",
        "scripts/smoke_dist.py",
        "tests/test_compliance.py",
    ):
        assert must in covered, must


def test_a_source_file_without_the_header_is_named(tmp_path: Path) -> None:
    root = tree(
        tmp_path,
        {
            "src/hoarddb/__main__.py": "print(1)\n",
            "examples/x.py": "import hoarddb\n",
            "scripts/s.py": "# a licence line that is not the line\n",
            "src/hoarddb/ok.py": HEADER,
        },
    )
    assert sorted(lint.check_spdx(root)) == [
        "examples/x.py: first line must be the SPDX header",
        "scripts/s.py: first line must be the SPDX header",
        "src/hoarddb/__main__.py: first line must be the SPDX header",
    ]


# -- no other database product is named anywhere we write ---------------------------------------


def test_the_repository_names_no_other_database_product() -> None:
    assert lint.check_product_names(ROOT) == []


def test_the_name_check_reads_the_readme_the_examples_and_our_documents(tmp_path: Path) -> None:
    root = tree(
        tmp_path,
        {
            "README.md": f"works like {BANNED}\n",
            "CHANGELOG.md": "fine\n",
            "examples/x.py": HEADER + f"# {BANNED}\n",
            "examples/notes.txt": f"{BANNED}\n",
            "docs/compliance.md": f"{BANNED}\n",
            "docs/five-minute-test.md": "fine\n",
            "scripts/run.sh": f"echo {BANNED}\n",
        },
    )
    found = lint.check_product_names(root)
    assert sorted(line.split(":")[0] for line in found) == [
        "README.md",
        "docs/compliance.md",
        "examples/notes.txt",
        "examples/x.py",
        "scripts/run.sh",
    ]


def test_the_one_allow_listed_file_is_the_specs_own_prohibition(tmp_path: Path) -> None:
    """The synced spec names a product once, in the sentence that forbids reading its driver. It is
    read-only, so it is the only allow-listed file; our own documents get no such pass."""
    assert lint.NAME_ALLOWED_FILES == ("docs/driver-python-spec.md",)
    root = tree(tmp_path, {"docs/driver-python-spec.md": f"never read the {BANNED} driver\n"})
    assert lint.check_product_names(root) == []


# Spellings a developer really writes: a name with its suffix, a client class, a driver module. Each
# is assembled so that this file does not trip the grep it tests.
SPELLINGS = [
    "Mon" + "goDB",
    "Mon" + "goClient",
    "py" + "mon" + "go",
    "Postgre" + "SQL",
    "psy" + "copg",
    "psy" + "copg2",
    "SQ" + "Lite",
    "sq" + "lite3",
    "My" + "SQL" + "Client",
    "Maria" + "DB",
    "Elastic" + "search",
    "Cass" + "andra",
    "Mem" + "cached",
    "Dynamo" + "DB",
]


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_the_name_check_names_a_product_with_its_suffix(tmp_path: Path, spelling: str) -> None:
    root = tree(tmp_path, {"README.md": f"Use {spelling} for storage.\n"})
    assert lint.check_product_names(root) == ["README.md:1: names another database product"]


def test_lint_exits_non_zero_for_a_readme_that_claims_compatibility_with_two_products(
    tmp_path: Path,
) -> None:
    """The script itself, run on a scratch tree, not only the function it calls."""
    shutil.copy(ROOT / "scripts" / "lint.py", tmp_path / "scripts_lint.py")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts_lint.py").rename(tmp_path / "scripts" / "lint.py")
    (tmp_path / "README.md").write_text(
        "Compatible with " + "Mon" + "goDB and " + "Postgre" + "SQL.\n"
    )
    run = subprocess.run(
        [sys.executable, str(tmp_path / "scripts" / "lint.py")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode != 0, run.stdout
    assert "README.md:1: names another database product" in run.stdout


def test_the_name_check_ignores_words_that_only_contain_a_name(tmp_path: Path) -> None:
    root = tree(
        tmp_path,
        {"README.md": "a redistribution licence; redistribute it; a credis card; a mongrel\n"},
    )
    assert lint.check_product_names(root) == []


# -- the boundary: the package, the examples and nothing else import only the standard library ---


def test_the_repository_passes_the_boundary_check() -> None:
    assert lint.check_boundary(ROOT) == []


def test_an_import_of_a_server_package_or_a_third_party_one_is_named(tmp_path: Path) -> None:
    root = tree(
        tmp_path,
        {
            "src/hoarddb/a.py": HEADER + "import hoardDB.transport\n",
            "src/hoarddb/b.py": HEADER + "from hoardDB.server import run\n",
            "src/hoarddb/ok.py": HEADER + "import os\nfrom hoarddb import errors\n",
            "examples/c.py": HEADER + "import requests\n",
            "examples/ok.py": HEADER + "import asyncio\nimport hoarddb.aio as hoarddb\n",
        },
    )
    found = lint.check_boundary(root)
    assert sorted(found) == [
        "examples/c.py:2: import of non-standard-library module 'requests'",
        "src/hoarddb/a.py:2: import of non-standard-library module 'hoardDB'",
        "src/hoarddb/b.py:2: import of non-standard-library module 'hoardDB'",
    ]


def test_the_server_module_path_is_absent_from_the_import_graph() -> None:
    """Import every module of the package in a clean interpreter and list what got loaded: only the
    standard library and ``hoarddb``, and nothing from a server tree (spec 9.3, `go list -deps` of
    the Go side)."""
    code = (
        "import importlib, json, pkgutil, sys\n"
        "before = {n.split('.')[0] for n in sys.modules}\n"
        f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
        "import hoarddb\n"
        "for m in pkgutil.walk_packages(hoarddb.__path__, 'hoarddb.'):\n"
        "    if m.name != 'hoarddb.__main__':\n"
        "        importlib.import_module(m.name)\n"
        "importlib.import_module('hoarddb.aio')\n"
        "loaded = {n.split('.')[0] for n in sys.modules} - before\n"
        "print(json.dumps(sorted(loaded - set(sys.stdlib_module_names) - {'hoarddb'})))\n"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "")},
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    leftovers = [n for n in eval(done.stdout) if not n.startswith("_")]
    assert leftovers == [], f"the package pulled in {leftovers}"


# -- no runtime dependency -----------------------------------------------------------------------


def test_the_project_declares_no_dependency_of_any_kind() -> None:
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["dependencies"] == []
    assert "optional-dependencies" not in project and "dependency-groups" not in project


# -- the licences of the dev tree ----------------------------------------------------------------


def tool_site_packages() -> list[Path]:
    """The site-packages of every dev tool this repository uses that is installed here."""
    found: list[Path] = []
    for tool in ("pytest", "mypy", "pyright", "ruff"):
        exe = shutil.which(tool)
        if exe is None:
            continue
        env = Path(exe).resolve().parent.parent
        found += sorted(env.glob("lib/python3*/site-packages"))
    found += [Path(p) for p in sys.path if p.endswith("site-packages") and Path(p).is_dir()]
    unique = {p.resolve(): p for p in found}
    return list(unique.values())


def test_every_installed_dev_tool_has_an_allowed_licence() -> None:
    sites = tool_site_packages()
    assert sites, "no dev-tool environment found to read licences from"
    records = licences.scan(sites)
    assert len(records) >= 10
    bad = [f"{r.name} {r.version}: {r.licence}" for r in records if not r.allowed]
    assert bad == [], f"licences outside the allow-list: {bad}"


def test_a_licence_outside_the_list_is_reported_by_the_script(tmp_path: Path) -> None:
    for name, licence in (("fine", "MIT"), ("copyleft", "GPL-3.0-only"), ("blank", "")):
        info = tmp_path / f"{name}-1.0.dist-info"
        info.mkdir()
        expression = f"License-Expression: {licence}\n" if licence else ""
        (info / "METADATA").write_text(
            f"Metadata-Version: 2.4\nName: {name}\nVersion: 1.0\n{expression}"
        )
    assert licences.main([str(tmp_path)]) == 1
    verdicts = {r.name: r.allowed for r in licences.scan([tmp_path])}
    assert verdicts == {"fine": True, "copyleft": False, "blank": False}


def test_a_distribution_without_a_metadata_licence_is_read_from_its_licence_file(
    tmp_path: Path,
) -> None:
    mit = (
        "MIT License\n\nPermission is hereby granted, free of charge, "
        "to any person obtaining a copy\nof this software and associated documentation files\n"
    )
    for name, text in (
        ("withmit", mit),
        ("withgpl", "GNU GENERAL PUBLIC LICENSE\nVersion 3\n"),
        ("bare", ""),
    ):
        info = tmp_path / f"{name}-1.0.dist-info"
        (info / "licenses").mkdir(parents=True)
        (info / "METADATA").write_text(f"Metadata-Version: 2.4\nName: {name}\nVersion: 1.0\n")
        if text:
            (info / "licenses" / "LICENSE").write_text(text)
    verdicts = {r.name: (r.licence, r.allowed) for r in licences.scan([tmp_path])}
    assert verdicts == {
        "withmit": ("MIT", True),
        "withgpl": ("UNKNOWN", False),
        "bare": ("UNKNOWN", False),
    }


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_no_vendored_third_party_code_is_tracked() -> None:
    tracked = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, cwd=ROOT, check=True
    ).stdout.split()
    vendored = [f for f in tracked if f.startswith(("vendor/", "third_party/", "site-packages/"))]
    assert vendored == []
    assert not any(f.endswith((".so", ".pyd", ".dll", ".dylib")) for f in tracked), "native code"
