# SPDX-License-Identifier: Apache-2.0
"""Pull the marked code blocks out of ``README.md`` (and the spec) so tests run the text itself."""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
SPEC = ROOT / "docs" / "driver-python-spec.md"

_MARKED = re.compile(r"<!-- block: (?P<name>[a-z-]+) -->\n```python\n(?P<code>.*?)```", re.S)


def readme_blocks() -> dict[str, str]:
    """Every ``<!-- block: name -->`` fenced Python block of the README, by name."""
    return {m["name"]: m["code"] for m in _MARKED.finditer(README.read_text(encoding="utf-8"))}


def spec_quickstart_blocks() -> list[str]:
    """The three Python blocks of spec section 2: blocking, asyncio, typed."""
    text = SPEC.read_text(encoding="utf-8")
    section = text[text.index("## 2. The five-minute quick-start") : text.index("## 3. Sync")]
    return re.findall(r"```python\n(.*?)```", section, re.S)


def spec_shield_block() -> str:
    """The ``asyncio.shield`` example of spec section 8.4, dedented."""
    text = SPEC.read_text(encoding="utf-8")
    section = text[text.index("### 8.4 Deadlines") : text.index("### 8.5")]
    return textwrap.dedent(re.findall(r"```python\n(.*?)```", section, re.S)[0])
