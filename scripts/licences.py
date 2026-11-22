# SPDX-License-Identifier: Apache-2.0
"""Report the licence of every installed distribution, from its metadata, against the allow-list.

``python scripts/licences.py SITE_PACKAGES...`` prints one line per distribution and exits 1 when
any licence is outside the allow-list (MIT, BSD, Apache-2.0, ISC, MPL-2.0, PSF, Unlicense). The
licence is never taken from this file's opinion of a tool: it is read from the installed
``METADATA``: ``License-Expression``, else the ``License ::`` classifier, else ``License``.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from email.parser import Parser
from pathlib import Path

_ALLOWED = (
    re.compile(r"^(0?bsd|bsd-\d-clause|bsd license|new bsd|simplified bsd)"),
    re.compile(r"^mit( license|-0)?$"),
    re.compile(r"^apache"),
    re.compile(r"^isc"),
    re.compile(r"^mpl-2\.0|^mozilla public license.*2\.0"),
    re.compile(r"^(psf|python software foundation|python-2\.0)"),
    re.compile(r"^unlicense|^the unlicense"),
)


@dataclass(frozen=True)
class Record:
    name: str
    version: str
    licence: str
    allowed: bool


def _term_allowed(term: str) -> bool:
    t = term.strip().strip("()").strip().lower()
    return any(p.search(t) for p in _ALLOWED)


def is_allowed(licence: str) -> bool:
    """An SPDX-ish expression: every AND term must pass; an OR term passes when any branch does."""
    if licence == "UNKNOWN":
        return False
    for conjunct in re.split(r"\s+AND\s+", licence):
        if not any(_term_allowed(b) for b in re.split(r"\s+OR\s+", conjunct)):
            return False
    return True


# The opening of the MIT licence's grant. A distribution whose METADATA names no licence (setuptools
# 79 ships only a LICENSE file) is read as MIT only when its own licence text carries this grant.
_MIT_GRANT = "permission is hereby granted, free of charge, to any person obtaining a copy"


def _licence_file_is_mit(info: Path) -> bool:
    for candidate in [*info.glob("LICEN[CS]E*"), *info.glob("licenses/**/LICEN[CS]E*")]:
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8", errors="replace")
            if _MIT_GRANT in " ".join(text.lower().split()):
                return True
    return False


def _licence_of(metadata: str) -> str:
    msg = Parser().parsestr(metadata)
    expression = msg.get("License-Expression")
    if expression:
        return expression.strip()
    for classifier in msg.get_all("Classifier") or []:
        if classifier.startswith("License :: OSI Approved :: "):
            return classifier.rsplit(" :: ", 1)[1].removesuffix(" License")
    free = (msg.get("License") or "").strip()
    if free and "\n" not in free and len(free) <= 100:
        return free
    return "UNKNOWN"


def scan(site_dirs: Iterable[Path]) -> list[Record]:
    out: list[Record] = []
    for site in site_dirs:
        for info in sorted(site.glob("*.dist-info")):
            meta = info / "METADATA"
            if not meta.is_file():
                continue
            text = meta.read_text(encoding="utf-8", errors="replace")
            msg = Parser().parsestr(text)
            licence = _licence_of(text)
            if licence == "UNKNOWN" and _licence_file_is_mit(info):
                licence = "MIT"
            out.append(
                Record(
                    msg.get("Name", info.name),
                    msg.get("Version", "?"),
                    licence,
                    is_allowed(licence),
                )
            )
    return out


def main(argv: list[str]) -> int:
    records = scan(Path(a) for a in argv)
    for r in records:
        print(f"{'ok ' if r.allowed else 'BAD'} {r.name} {r.version}: {r.licence}")
    return 1 if any(not r.allowed for r in records) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
