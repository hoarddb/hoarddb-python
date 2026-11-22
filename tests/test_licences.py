# SPDX-License-Identifier: Apache-2.0
"""The dev-tool licence check (spec 9.2): read from installed metadata, allow-list only."""

from __future__ import annotations

from pathlib import Path

import licences


def _dist(site: Path, name: str, metadata: str) -> None:
    d = site / f"{name}-1.0.dist-info"
    d.mkdir(parents=True)
    (d / "METADATA").write_text(f"Metadata-Version: 2.4\nName: {name}\nVersion: 1.0\n{metadata}")


def test_licence_comes_from_the_expression_then_the_classifier_then_the_free_text(
    tmp_path: Path,
) -> None:
    _dist(tmp_path, "a", "License-Expression: MIT\n")
    _dist(tmp_path, "b", "Classifier: License :: OSI Approved :: BSD License\n")
    _dist(tmp_path, "c", "License: Apache Software License 2.0\n")
    _dist(tmp_path, "d", "")
    found = {r.name: r.licence for r in licences.scan([tmp_path])}
    assert found == {
        "a": "MIT",
        "b": "BSD",
        "c": "Apache Software License 2.0",
        "d": "UNKNOWN",
    }


def test_only_the_allowed_licences_pass(tmp_path: Path) -> None:
    for i, lic in enumerate(
        ["MIT", "BSD-3-Clause", "Apache-2.0", "ISC", "MPL-2.0", "PSF-2.0", "Unlicense", "GPL-3.0"]
    ):
        _dist(tmp_path, f"p{i}", f"License-Expression: {lic}\n")
    _dist(
        tmp_path,
        "lgpl",
        "Classifier: License :: OSI Approved :: GNU Lesser General Public License v3\n",
    )
    _dist(tmp_path, "mixed", "License-Expression: MIT AND Python-2.0\n")
    bad = sorted(r.name for r in licences.scan([tmp_path]) if not r.allowed)
    assert bad == ["lgpl", "p7"]
