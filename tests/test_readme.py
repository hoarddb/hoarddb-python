# SPDX-License-Identifier: Apache-2.0
"""The README cannot drift from the spec or from the files under ``examples/`` (spec G-2, G-6):
its code is the spec's text, the quick-starts are the example files, and what the spec requires it
to say is there."""

from __future__ import annotations

import ast
import textwrap

import pytest

import readmekit

TEXT = " ".join(readmekit.README.read_text(encoding="utf-8").split())  # one line: wrapping is free
EXAMPLES = readmekit.ROOT / "examples"
SPDX = "# SPDX-License-Identifier: Apache-2.0\n"


def test_the_blocks_are_all_marked_and_found() -> None:
    assert set(readmekit.readme_blocks()) == {"quickstart", "quickstart-async", "typed", "shield"}


needs_spec = pytest.mark.skipif(
    not readmekit.SPEC.exists(), reason="the specification is not part of this checkout"
)


@needs_spec
@pytest.mark.parametrize(
    ("name", "index"), [("quickstart", 0), ("quickstart-async", 1), ("typed", 2)]
)
def test_a_readme_quickstart_block_is_the_specs_block(name: str, index: int) -> None:
    assert (
        readmekit.readme_blocks()[name] == readmekit.spec_quickstart_blocks()[index].rstrip() + "\n"
    )


@pytest.mark.parametrize(
    ("name", "file"), [("quickstart", "quickstart.py"), ("quickstart-async", "quickstart_async.py")]
)
def test_the_quickstart_files_are_the_readme_blocks_plus_the_licence_line(
    name: str, file: str
) -> None:
    on_disk = (EXAMPLES / file).read_text(encoding="utf-8")
    assert on_disk.startswith(SPDX), "every source file starts with the SPDX line"
    assert on_disk[len(SPDX) :] == readmekit.readme_blocks()[name]


@needs_spec
def test_the_shield_example_is_the_specs_8_4_text_word_for_word() -> None:
    assert readmekit.readme_blocks()["shield"].rstrip() == readmekit.spec_shield_block().rstrip()


def test_every_readme_block_is_valid_python() -> None:
    for name, code in readmekit.readme_blocks().items():
        if name == "shield":  # a fragment: it lives inside a coroutine
            code = "async def f(users, doc):\n" + textwrap.indent(code, "    ")
        ast.parse(code, filename=f"README block {name}")


def test_the_five_minute_path_says_how_to_trust_on_its_first_screen() -> None:
    first_block = TEXT.index("<!-- block: quickstart -->")
    assert "python -m hoarddb trust localhost:7433" in TEXT[:first_block]
    assert "?fingerprint=" in TEXT[:first_block]
    assert "pip install ./hoarddb-python" in TEXT[:first_block]


def test_the_trust_section_orders_pin_trust_accept_and_owns_the_weakness() -> None:
    section = TEXT[TEXT.index("## Trusting a server") : TEXT.index("## The six store types")]
    assert (
        section.index("Pin it")
        < section.index("python -m hoarddb trust")
        < section.index("accept_first_sight")
    )
    assert "trust on first use" in section and "weaker than a pin" in section


def test_the_cancellation_paragraph_says_a_cancelled_write_is_not_known_and_shields() -> None:
    section = TEXT[TEXT.index("## Cancellation") : TEXT.index("## Retries")]
    assert "a cancelled write is not known to have happened" in section
    assert "asyncio.shield" in section and "CancelledError" in section


def test_the_retries_paragraph_says_what_retryable_means_and_what_is_never_retried() -> None:
    section = TEXT[TEXT.index("## Retries") : TEXT.index("## Compatibility")]
    for needle in ("retryable", "never retried", "write_outcome", "idempotency", "RATE_LIMITED"):
        assert needle in section, needle


def test_every_store_type_has_an_example_file_named_in_the_readme() -> None:
    for kind in ("hash", "btree", "fifo", "lifo", "heap", "blob", "store_fields", "typed_bucket"):
        assert (EXAMPLES / f"{kind}.py").is_file(), kind
        assert f"examples/{kind}.py" in TEXT, kind


def test_the_readme_never_calls_the_command_hoarddb() -> None:
    assert "console script" not in TEXT.lower()
    assert "python -m hoarddb" in TEXT
