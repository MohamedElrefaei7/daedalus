"""Fixtures for the terminal tests; the helpers they use live in fakes.py.

No test reaches the network: every DeepSeek client is built on
httpx2.MockTransport (openai 3.x uses httpx2), and FakeDeepSeek records every
request it receives. The core fixtures are reused as in tests/mcp.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import keyring
import pytest

if "core_conftest" not in sys.modules:
    _spec = importlib.util.spec_from_file_location("core_conftest", Path(__file__).parents[1] / "core" / "conftest.py")
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["core_conftest"] = _mod
    _spec.loader.exec_module(_mod)
core_conftest = sys.modules["core_conftest"]

_index_cache = core_conftest._index_cache
_hermetic_git = core_conftest._hermetic_git
state_root = core_conftest.state_root
_golden_git_world = core_conftest._golden_git_world
gworld = core_conftest.gworld


# Core helpers, re-exported: core test modules say `from conftest import git`, and pytest binds the
# name conftest to whichever folder's conftest it imported last, so every conftest offers them.
DROP = core_conftest.DROP
TEMPLATES = core_conftest.TEMPLATES
frontmatter = core_conftest.frontmatter
note_text = core_conftest.note_text
write_note = core_conftest.write_note
write_raw = core_conftest.write_raw
spawn = core_conftest.spawn
git = core_conftest.git
git_init = core_conftest.git_init
git_commit_all = core_conftest.git_commit_all
make_project = core_conftest.make_project
make_shared = core_conftest.make_shared
write_registry = core_conftest.write_registry
tree_hash = core_conftest.tree_hash
vault_digest = core_conftest.vault_digest
head = core_conftest.head
build_world = core_conftest.build_world
open_world = core_conftest.open_world

from fakes import FakeDeepSeek, MemoryKeyring  # noqa: E402

from daedalus.terminal import obsidian  # noqa: E402
from daedalus.terminal.config import Config  # noqa: E402


@pytest.fixture(autouse=True)
def memory_keyring(monkeypatch):
    """Never touch the real Keychain, and start with no key anywhere."""
    previous = keyring.get_keyring()
    ring = MemoryKeyring()
    keyring.set_keyring(ring)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    yield ring
    keyring.set_keyring(previous)


@pytest.fixture(autouse=True)
def no_real_obsidian(monkeypatch):
    """No test sees or launches the real Obsidian: it's "not running", and `open` refuses to run."""
    def refuse(argv):
        raise AssertionError(f"a test tried to run {argv!r}")

    monkeypatch.setattr(obsidian, "is_running", lambda: False)
    monkeypatch.setattr(obsidian, "run_open", refuse)


@pytest.fixture
def fake() -> FakeDeepSeek:
    return FakeDeepSeek()


@pytest.fixture
def cfg() -> Config:
    return Config()
