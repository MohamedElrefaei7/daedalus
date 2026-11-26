"""Test 7 and commit 6's prompt tests: six blocks, most stable first, byte-identical for identical files."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys

import pytest
from core_conftest import write_note, write_raw

from daedalus.terminal import prompts
from daedalus.terminal.prompts import PREAMBLE, SEPARATOR, blocks, system_prompt

SCRIPT = """
import hashlib, sys
from daedalus.core.registry import load_registry
from daedalus.core.session import Session
from daedalus.terminal.prompts import system_prompt
s = Session(load_registry(sys.argv[1]), active="alpha", pulled=("gamma", "beta"))
print(hashlib.sha256(system_prompt(s).encode()).hexdigest())
"""

PULLED_CONTEXT = """# CONTEXT

## Log

### 2026-11-20 — something private to beta's log

## Decisions index

- [[storage-engine]]: SQLite

## Where things live

- fetcher: src/fetch.py

## Up Next

beta's plans
"""


@pytest.fixture
def rich(gworld):
    """A world with every block populated: two pulled projects, several practices, a real CONTEXT.md."""
    from daedalus.core.session import Session

    write_note(gworld.shared, "practices/one-commit-one-change.md", "practice", "# One commit, one change\n",
               applies_to=["git"], related=["[[red-test-first]]"])
    write_note(gworld.shared, "practices/draft-idea.md", "practice", "# Not yet\n", status="draft",
               applies_to=["x"])
    write_raw(gworld.beta, "CONTEXT.md", PULLED_CONTEXT)
    write_raw(gworld.gamma, "CONTEXT.md", PULLED_CONTEXT.replace("SQLite", "Postgres"))
    write_raw(gworld.alpha, "CONTEXT.md", "# CONTEXT\n\n## Log\n\n### 2026-11-25 — first session\n\n## Up Next\n")
    gworld.session = Session(gworld.registry, active="alpha", pulled=("gamma", "beta"))
    return gworld


def test_two_assemblies_are_byte_identical(rich) -> None:
    assert system_prompt(rich.session).encode() == system_prompt(rich.session).encode()


def test_six_blocks_in_order(rich) -> None:
    b = blocks(rich.session)
    assert b[0] == PREAMBLE
    assert b[1].startswith("# Shared vault (shared): AGENTS.md")
    assert b[2].startswith("# Active project (alpha): AGENTS.md")
    assert b[3].startswith("# Accepted shared practices")
    assert b[4].startswith("# Pulled project (beta, read-only)")  # pulled sorted by name
    assert b[5].startswith("# Pulled project (gamma, read-only)")
    assert b[6].startswith("# Active project (alpha): CONTEXT.md")
    assert len(b) == 7
    assert system_prompt(rich.session) == SEPARATOR.join(b) + "\n"


def test_practices_block_sorted_and_accepted_only(rich) -> None:
    block = blocks(rich.session)[3]
    assert block.index("shared:practices/one-commit-one-change.md") < block.index("shared:practices/red-test-first.md")
    assert "draft-idea" not in block and "Not yet" not in block


def test_pulled_block_has_only_the_two_sections(rich) -> None:
    block = blocks(rich.session)[4]
    assert "## Decisions index" in block and "SQLite" in block
    assert "## Where things live" in block and "src/fetch.py" in block
    assert "private to beta's log" not in block and "beta's plans" not in block


def test_changing_active_context_keeps_the_prefix_through_block_5(rich) -> None:
    before = blocks(rich.session)
    prefix = SEPARATOR.join(before[:-1]) + SEPARATOR
    write_raw(rich.alpha, "CONTEXT.md", "# CONTEXT\n\n## Log\n\n### 2026-11-26 — a new entry\n")
    after = system_prompt(rich.session)
    assert after.startswith(prefix), "changing the active CONTEXT.md changed an earlier block"
    assert after != SEPARATOR.join(before) + "\n"


def test_changing_a_practice_changes_block_4_onward_only(rich) -> None:
    before = blocks(rich.session)
    write_note(rich.shared, "practices/red-test-first.md", "practice", "# Red test first, revised\n",
               applies_to=["testing"])
    after = blocks(rich.session)
    assert after[:3] == before[:3]
    assert after[3] != before[3] and "revised" in after[3]
    assert after[4:] == before[4:]


def test_practice_cap_truncates_with_a_note(rich, monkeypatch) -> None:
    for i in range(6):
        write_note(rich.shared, f"practices/p{i}.md", "practice", "# P\n\n" + "x" * 3000 + "\n", applies_to=["t"])
    block = blocks(rich.session)[3]
    body = block[len("# Accepted shared practices\n\n"):]
    shown = body.split("\n\n[truncated:")[0]
    assert len(shown) <= prompts.MAX_PRACTICES_CHARS
    assert "[truncated: the 12,000-character cap for practices left out" in block
    assert "read them with read_note: shared:practices/" in block


def test_pulled_cap_is_6000_each(rich) -> None:
    write_raw(rich.beta, "CONTEXT.md", "# CONTEXT\n\n## Decisions index\n\n" + "d" * 9000 + "\n")
    block = blocks(rich.session)[4]
    assert "[truncated:" in block and len(block) < prompts.MAX_PULLED_CHARS + 400


def test_identical_across_processes_with_different_hash_seeds(rich) -> None:
    """Set or dict ordering would differ between these processes; no block may."""
    digests = set()
    for seed in range(8):
        out = subprocess.run([sys.executable, "-c", SCRIPT, str(rich.registry_path)], check=True,
                             capture_output=True, text=True, env={**os.environ, "PYTHONHASHSEED": str(seed)})
        digests.add(out.stdout.strip())
    assert digests == {hashlib.sha256(system_prompt(rich.session).encode()).hexdigest()}


# Block 6 keeps only the newest Log entries -------------------------------------------------

def context_with(n: int) -> str:
    entries = "".join(f"### 2026-11-{i:02d} — session {i}\n\nWhat happened in session {i}.\n\n" for i in range(1, n + 1))
    return ("# CONTEXT\n\n## Log\n\n" + entries + "## Decisions index\n\n- [[storage-engine]]: SQLite\n\n"
            "## Where things live\n\n- fetcher: src/fetch.py\n\n## Up Next\n\nCommit 7.\n")


def test_block_6_keeps_the_newest_8_log_entries_plus_a_note(rich) -> None:
    write_raw(rich.alpha, "CONTEXT.md", context_with(20))
    block = blocks(rich.session)[-1]
    shown = [i for i in range(1, 21) if f"— session {i}\n" in block]
    assert shown == list(range(13, 21))
    notes = [line for line in block.splitlines() if "older Log entries" in line]
    assert notes == ["[12 older Log entries are in CONTEXT.md and not shown here; read_note alpha:CONTEXT.md "
                     "for the full log.]"]
    for section in ("## Decisions index", "- [[storage-engine]]: SQLite", "## Where things live",
                    "- fetcher: src/fetch.py", "## Up Next", "Commit 7."):
        assert section in block  # every non-Log section, whole


def test_short_log_is_whole_and_has_no_note(rich) -> None:
    write_raw(rich.alpha, "CONTEXT.md", context_with(8))
    block = blocks(rich.session)[-1]
    assert all(f"— session {i}\n" in block for i in range(1, 9))
    assert "older Log entries" not in block


def test_a_21st_entry_changes_only_block_6(rich) -> None:
    write_raw(rich.alpha, "CONTEXT.md", context_with(20))
    before = blocks(rich.session)
    write_raw(rich.alpha, "CONTEXT.md", context_with(21))
    after = blocks(rich.session)
    assert after[:-1] == before[:-1]
    assert after[-1] != before[-1]
    assert "— session 21\n" in after[-1] and "— session 13\n" not in after[-1]
    assert "[13 older Log entries" in after[-1]


def test_capped_context_is_identical_across_processes(rich) -> None:
    write_raw(rich.alpha, "CONTEXT.md", context_with(20))
    digests = set()
    for seed in (0, 1, 2, 3):
        out = subprocess.run([sys.executable, "-c", SCRIPT, str(rich.registry_path)], check=True,
                             capture_output=True, text=True, env={**os.environ, "PYTHONHASHSEED": str(seed)})
        digests.add(out.stdout.strip())
    assert digests == {hashlib.sha256(system_prompt(rich.session).encode()).hexdigest()}


def test_newest_by_date_even_when_the_log_runs_newest_first(rich) -> None:
    text = context_with(20)
    head, rest = text.split("## Log\n\n", 1)
    entries_part, tail = rest.split("## Decisions index", 1)
    entries = ["### " + e for e in entries_part.split("### ") if e]
    write_raw(rich.alpha, "CONTEXT.md", head + "## Log\n\n" + "".join(reversed(entries)) + "## Decisions index" + tail)
    block = blocks(rich.session)[-1]
    shown = [i for i in range(1, 21) if f"— session {i}\n" in block]
    assert shown == list(range(13, 21))
    assert block.index("— session 20\n") < block.index("— session 13\n")  # kept in file order
