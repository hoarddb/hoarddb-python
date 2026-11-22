# SPDX-License-Identifier: Apache-2.0
"""Smoke test of the built distributions (spec G-1, section 14 P19).

    python -m build            # needs hatchling and build (not installed everywhere)
    python scripts/smoke_dist.py dist/*.whl dist/*.tar.gz

checks that the wheel is pure Python (``py3-none-any``), declares no dependency, carries
``py.typed`` and the licence, still says ``Private :: Do Not Upload``, and that its Python files
carry the SPDX line; that the sdist can rebuild such a wheel; and, in a CLEAN virtual environment
with no package index, that the wheel installs, imports, and answers ``python -m hoarddb --help``.

With ``--build`` it runs ``python -m build`` first, into a temporary directory. Exit status 0 means
every check passed; each failure is printed with the fix.
"""

from __future__ import annotations

import subprocess
import sys
import tarfile
import tempfile
import zipfile
from email.parser import Parser
from pathlib import Path

SPDX = "# SPDX-License-Identifier: Apache-2.0"
NOT_SHIPPED = ("tests/", "handbook/", "testdata/", "docs/", "examples/", ".cache/")


def _metadata(text: str) -> tuple[dict[str, list[str]], str]:
    message = Parser().parsestr(text)
    fields: dict[str, list[str]] = {}
    for key, value in message.items():
        fields.setdefault(key, []).append(str(value))
    return fields, str(message.get_payload())


def check_metadata(text: str, where: str) -> list[str]:
    fields, _ = _metadata(text)
    problems: list[str] = []
    if "Requires-Dist" in fields:
        problems.append(
            f"{where}: Requires-Dist {fields['Requires-Dist']}: the driver has no runtime "
            "dependency; remove it from [project].dependencies"
        )
    if "Private :: Do Not Upload" not in fields.get("Classifier", []):
        problems.append(f"{where}: the classifier 'Private :: Do Not Upload' is missing")
    licence = fields.get("License-Expression", []) + fields.get("License", [])
    if not any("Apache-2.0" in item for item in licence):
        problems.append(f"{where}: the licence is not Apache-2.0 ({licence})")
    if not any(item.startswith(">=3.11") for item in fields.get("Requires-Python", [])):
        problems.append(f"{where}: Requires-Python must be >=3.11")
    return problems


def check_wheel(path: Path) -> list[str]:
    """Everything wrong with a built wheel, as sentences that say what to do."""
    problems: list[str] = []
    if not path.name.endswith("-py3-none-any.whl"):
        problems.append(f"{path.name}: the wheel tag must be py3-none-any (a pure-Python wheel)")
    with zipfile.ZipFile(path) as wheel:
        names = wheel.namelist()
        metadata = [n for n in names if n.endswith(".dist-info/METADATA")]
        if len(metadata) != 1:
            return [*problems, f"{path.name}: expected one METADATA file, found {metadata}"]
        problems += check_metadata(wheel.read(metadata[0]).decode("utf-8"), path.name)
        for required in ("hoarddb/__init__.py", "hoarddb/__main__.py", "hoarddb/py.typed"):
            if required not in names:
                problems.append(f"{path.name}: {required} is missing from the wheel")
        if not any(n.endswith(".dist-info/licenses/LICENSE") for n in names):
            problems.append(f"{path.name}: the LICENSE file is not inside .dist-info/licenses/")
        for name in names:
            if name.startswith(NOT_SHIPPED):
                problems.append(f"{path.name}: {name} must not ship in the wheel")
            if name.endswith(".py"):
                first = wheel.read(name).decode("utf-8").split("\n", 1)[0]
                if first != SPDX:
                    problems.append(f"{path.name}: {name} does not start with the SPDX line")
    return problems


def check_sdist(path: Path) -> list[str]:
    """Everything wrong with a built sdist."""
    problems: list[str] = []
    with tarfile.open(path) as sdist:
        names = [n.split("/", 1)[1] for n in sdist.getnames() if "/" in n]
        for required in (
            "PKG-INFO",
            "pyproject.toml",
            "LICENSE",
            "README.md",
            "src/hoarddb/py.typed",
        ):
            if required not in names:
                problems.append(f"{path.name}: {required} is missing from the sdist")
        info = [n for n in sdist.getnames() if n.endswith("/PKG-INFO")]
        if info:
            member = sdist.extractfile(info[0])
            assert member is not None
            problems += check_metadata(member.read().decode("utf-8"), path.name)
    return problems


def run(command: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, cwd=cwd, timeout=600)


def smoke_in_clean_venv(wheel: Path, scratch: Path) -> list[str]:
    """Install ``wheel`` into a fresh virtual environment (no index, no other package), then import
    it from outside the source tree and run the module's help."""
    venv = scratch / "venv"
    made = run([sys.executable, "-m", "venv", str(venv)])
    if made.returncode != 0:
        return [f"could not create a virtual environment: {made.stderr.strip()}"]
    python = str(venv / "bin" / "python")
    problems: list[str] = []
    installed = run([python, "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)])
    if installed.returncode != 0:
        return [f"pip could not install {wheel.name}: {installed.stderr.strip()[-400:]}"]
    listing = run([python, "-m", "pip", "list", "--format=freeze"]).stdout.split()
    extra = [p for p in listing if not p.startswith(("hoarddb==", "pip==", "setuptools=="))]
    if extra:
        problems.append(f"the clean environment holds more than the driver: {extra}")
    imported = run(
        [
            python,
            "-I",
            "-c",
            "import hoarddb, hoarddb.aio; print(hoarddb.__version__, hoarddb.__file__)",
        ],
        cwd=scratch,
    )
    if imported.returncode != 0 or str(scratch) not in imported.stdout:
        problems.append(f"import hoarddb failed or did not come from the venv: {imported}")
    helped = run([python, "-I", "-m", "hoarddb", "--help"], cwd=scratch)
    if helped.returncode != 0 or "python -m hoarddb trust" not in helped.stdout:
        problems.append(f"python -m hoarddb --help failed: {helped.stderr.strip()[-300:]}")
    return problems


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix="hoarddb-smoke-") as tmp:
        scratch = Path(tmp)
        if argv[:1] == ["--build"]:
            built = run([sys.executable, "-m", "build", "--outdir", str(scratch / "dist")], root)
            if built.returncode != 0:
                print(
                    f"python -m build failed (is `pip install build hatchling` done?):\n"
                    f"{built.stderr}"
                )
                return 1
            paths = sorted((scratch / "dist").iterdir())
        else:
            paths = [Path(a) for a in argv]
        wheels = [p for p in paths if p.suffix == ".whl"]
        sdists = [p for p in paths if p.name.endswith(".tar.gz")]
        if len(wheels) != 1 or len(sdists) != 1:
            print("usage: smoke_dist.py --build | smoke_dist.py <one wheel> <one sdist>")
            return 2
        problems = check_wheel(wheels[0]) + check_sdist(sdists[0])
        # the sdist must rebuild a wheel that passes the same checks
        with tarfile.open(sdists[0]) as sdist:
            sdist.extractall(scratch / "unpacked", filter="data")
        (source,) = (scratch / "unpacked").iterdir()
        rebuilt = run(
            [sys.executable, "-m", "build", "--wheel", "--outdir", str(scratch / "again")], source
        )
        if rebuilt.returncode != 0:
            problems.append(f"the sdist does not build a wheel: {rebuilt.stderr.strip()[-400:]}")
        else:
            (again,) = (scratch / "again").glob("*.whl")
            problems += [f"(from the sdist) {p}" for p in check_wheel(again)]
        problems += smoke_in_clean_venv(wheels[0], scratch)
        for problem in problems:
            print(problem)
        print("smoke test:", "FAILED" if problems else "passed")
        return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
