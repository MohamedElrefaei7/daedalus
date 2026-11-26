"""Test 7: the system prompt is byte-identical for identical vault state, across processes too."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys

from core_conftest import write_raw

from daedalus.terminal.prompts import PREAMBLE, system_prompt

SCRIPT = """
import hashlib, sys
from daedalus.core.registry import load_registry
from daedalus.core.session import Session
from daedalus.terminal.prompts import system_prompt
s = Session(load_registry(sys.argv[1]), active="alpha", pulled=("beta",))
print(hashlib.sha256(system_prompt(s).encode()).hexdigest())
"""


def test_two_assemblies_are_byte_identical(gworld) -> None:
    a, b = system_prompt(gworld.session), system_prompt(gworld.session)
    assert a.encode() == b.encode()


def test_order_is_preamble_shared_then_project(gworld) -> None:
    p = system_prompt(gworld.session)
    shared = (gworld.shared / "AGENTS.md").read_text().strip()
    project = (gworld.alpha / "AGENTS.md").read_text().strip()
    assert p.startswith(PREAMBLE)
    assert p.index(PREAMBLE) < p.index(shared) < p.index(project)
    assert "# Shared vault (shared): AGENTS.md" in p and "# Active project (alpha): AGENTS.md" in p


def test_changing_project_agents_md_changes_the_prompt(gworld) -> None:
    before = system_prompt(gworld.session)
    write_raw(gworld.alpha, "AGENTS.md", (gworld.alpha / "AGENTS.md").read_text() + "\nOne more rule.\n")
    after = system_prompt(gworld.session)
    assert after != before and "One more rule." in after


def test_identical_across_processes_with_different_hash_seeds(gworld) -> None:
    """Set or dict-of-set ordering would differ between these processes; the prompt must not."""
    digests = set()
    for seed in range(8):
        out = subprocess.run([sys.executable, "-c", SCRIPT, str(gworld.registry_path)], check=True,
                             capture_output=True, text=True, env={**os.environ, "PYTHONHASHSEED": str(seed)})
        digests.add(out.stdout.strip())
    assert digests == {hashlib.sha256(system_prompt(gworld.session).encode()).hexdigest()}
