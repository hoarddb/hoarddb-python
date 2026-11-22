# SPDX-License-Identifier: Apache-2.0
"""Repository rules that no general linter knows: SPDX headers, import boundary, product names.

Run ``python scripts/lint.py`` from the repository root. Exit status is the number of problems
(capped at 1). The synced, read-only trees (``handbook/``, ``testdata/``, ``docs/``) are not
scanned: they are not ours to edit.
"""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Iterator
from pathlib import Path

SPDX = "# SPDX-License-Identifier: Apache-2.0"

# Code that is ours. Everything else (handbook, testdata, docs) is synced or prose.
SOURCE_DIRS = ("src", "tests", "scripts", "examples")
NAME_SCAN_FILES = ("README.md", "pyproject.toml", "CHANGELOG.md")
# Our own prose and scripts are scanned as well. The one exception is the synced, read-only spec: it
# names a product once, in the sentence that forbids reading that product's driver.
NAME_SCAN_GLOBS = ("docs/**/*.md", "scripts/*.sh")
NAME_ALLOWED_FILES = ("docs/driver-python-spec.md",)
# Imports are checked in the package and in the examples (what a reader copies).
BOUNDARY_DIRS = ("src", "examples")

# The product names the clean-room rule forbids anywhere in our own text. They are assembled from
# pieces so that this file does not trip the very grep it implements.
_BANNED_PIECES = (
    ("mon", "go"),
    ("post", "gres"),
    ("my", "sql"),
    ("maria", "db"),
    ("red", "is"),
    ("cass", "andra"),
    ("couch", "base"),
    ("dynamo", "db"),
    ("cockroach", "db"),
    ("elastic", "search"),
    ("mem", "cached"),
    ("sq", "lite"),
    ("psy", "copg"),
)
BANNED = tuple("".join(p) for p in _BANNED_PIECES)
# A name that is also the start of ordinary words is only banned when no letter follows it
# ("redistribution" is a word of ours). The others are banned whatever follows, because a developer
# writes a name with its suffix, a client class or a driver module and a trailing-letter guard on
# those let every one of them through.
_ONLY_WHOLE_WORD = ("red" + "is",)
_ALTERNATION = "|".join(f"{name}(?![a-z])" if name in _ONLY_WHOLE_WORD else name for name in BANNED)
_BANNED_RE = re.compile(rf"(?<![a-z])(?:py)?(?:{_ALTERNATION})", re.IGNORECASE)


def _python_files(root: Path, dirs: tuple[str, ...] = SOURCE_DIRS) -> Iterator[Path]:
    for d in dirs:
        base = root / d
        if base.is_dir():
            yield from sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)


def source_files(root: Path) -> list[Path]:
    """Every Python file the SPDX check reads."""
    return list(_python_files(root))


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def check_spdx(root: Path) -> list[str]:
    problems: list[str] = []
    for path in _python_files(root):
        first = path.read_text(encoding="utf-8").split("\n", 1)[0]
        if first != SPDX:
            problems.append(f"{_rel(root, path)}: first line must be the SPDX header")
    return problems


def check_boundary(root: Path) -> list[str]:
    """Every import in ``src/`` and ``examples/`` is the standard library or ``hoarddb`` itself
    (a server package such as ``hoardDB.transport`` is neither)."""
    allowed = set(sys.stdlib_module_names) | {"hoarddb"}
    problems: list[str] = []
    for path in _python_files(root, BOUNDARY_DIRS):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            else:
                continue
            for name in names:
                if name not in allowed:
                    problems.append(
                        f"{_rel(root, path)}:{node.lineno}: "
                        f"import of non-standard-library module {name!r}"
                    )
    return problems


# The sans-I/O core (spec 3.3) holds no socket, no clock, no thread and no event loop.
_CORE_FORBIDDEN = frozenset(
    {"socket", "ssl", "threading", "asyncio", "time", "select", "selectors", "subprocess"}
)


def check_core_purity(root: Path) -> list[str]:
    problems: list[str] = []
    core = root / "src" / "hoarddb" / "_core"
    for path in sorted(core.rglob("*.py")) if core.is_dir() else []:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            else:
                continue
            problems += [
                f"{_rel(root, path)}:{node.lineno}: the sans-I/O core imports {name!r}"
                for name in names
                if name in _CORE_FORBIDDEN
            ]
    return problems


def check_product_names(root: Path) -> list[str]:
    problems: list[str] = []
    files = list(_python_files(root))
    files += [root / n for n in NAME_SCAN_FILES if (root / n).is_file()]
    examples = root / "examples"
    if examples.is_dir():
        files += sorted(p for p in examples.rglob("*") if p.is_file() and p.suffix != ".py")
    for pattern in NAME_SCAN_GLOBS:
        files += sorted(
            p for p in root.glob(pattern) if _rel(root, p) not in NAME_ALLOWED_FILES and p.is_file()
        )
    for path in files:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _BANNED_RE.search(line):
                problems.append(f"{_rel(root, path)}:{lineno}: names another database product")
    return problems


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    problems = (
        check_spdx(root)
        + check_boundary(root)
        + check_core_purity(root)
        + check_product_names(root)
    )
    for p in problems:
        print(p)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
