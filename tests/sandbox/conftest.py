"""Fixtures for the sandbox tests; the fake executor lives in sandbox_fakes.py.

Unit tests inject a fake executor and never sandbox anything. The live tests
(test_live.py, marked `sandbox`, run with `-m sandbox`) use the real
sandbox-exec and the real scratch2 venv; they fail, never skip, if it's missing.
The core fixtures are reused as in tests/mcp and tests/terminal.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

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
world = core_conftest.world

from daedalus.sandbox import runner  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_runner_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """The preflight verdict and `uv python dir` are cached per process; each test starts without them."""
    monkeypatch.setattr(runner, "_preflight", None)
    monkeypatch.setattr(runner, "_uv_python_dir", None)
