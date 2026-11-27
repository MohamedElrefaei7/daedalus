"""Fixtures for daedalus-mcp: the core test world plus a state file and the server.

The core fixtures live in tests/core/conftest.py. Both folders' conftests are
plain modules named "conftest", so the core one is loaded here under its own
name (core_conftest) and its fixtures re-exported, instead of copied.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

if "core_conftest" not in sys.modules:  # tests/terminal loads it the same way
    _spec = importlib.util.spec_from_file_location("core_conftest", Path(__file__).parents[1] / "core" / "conftest.py")
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["core_conftest"] = _mod
    _spec.loader.exec_module(_mod)
core_conftest = sys.modules["core_conftest"]

# Fixtures (autouse ones included) apply here once they're names in this conftest.
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

from mcp_helpers import Mcp, write_state  # noqa: E402

from daedalus.mcp_server import build_server  # noqa: E402


@pytest.fixture
def mworld(gworld):
    """gworld with a state file naming alpha active and beta pulled, and a server pointed at both."""
    gworld.state_path = write_state(gworld.root / "state.json", "alpha", ["beta"])
    gworld.config_path = gworld.root / "config.toml"  # absent: the default caps ($1.00 a session)
    gworld.server = build_server(gworld.registry_path, gworld.state_path, gworld.config_path)
    return gworld


@pytest.fixture
def mcp(mworld):
    return Mcp(mworld.server, mworld.state_path)
