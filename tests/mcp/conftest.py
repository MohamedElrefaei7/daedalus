"""Fixtures for daedalus-mcp: the core test world plus a state file and the server.

The core fixtures live in tests/core/conftest.py. Both folders' conftests are
plain modules named "conftest", so the core one is loaded here under its own
name (core_conftest) and its fixtures re-exported, instead of copied.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

_spec = importlib.util.spec_from_file_location("core_conftest", Path(__file__).parents[1] / "core" / "conftest.py")
core_conftest = importlib.util.module_from_spec(_spec)
sys.modules["core_conftest"] = core_conftest
_spec.loader.exec_module(core_conftest)

# Fixtures (autouse ones included) apply here once they're names in this conftest.
_index_cache = core_conftest._index_cache
_hermetic_git = core_conftest._hermetic_git
state_root = core_conftest.state_root
_golden_git_world = core_conftest._golden_git_world
gworld = core_conftest.gworld

from daedalus.mcp_server import build_server  # noqa: E402

TOOLS = {"get_scope", "list_notes", "read_note", "search", "backlinks", "list_files", "read_code", "propose_note"}


def write_state(path: Path, active: str, pulled: list[str] | tuple[str, ...] = ()) -> Path:
    path.write_text(json.dumps({"active": active, "pulled": list(pulled)}))
    return path


class Mcp:
    """One server and one client session, for tests that make several calls in a row."""

    def __init__(self, server, state_path: Path) -> None:
        self.server = server
        self.state_path = state_path
        self.client = Client(server)

    async def __aenter__(self) -> "Mcp":
        await self.client.__aenter__()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.client.__aexit__(*exc)

    async def call(self, name: str, args: dict[str, Any] | None = None) -> tuple[bool, Any]:
        """(is_error, result): the structured result on success, the error text on failure."""
        r = await self.client.call_tool(name, args or {}, raise_on_error=False)
        if r.is_error:
            return True, "\n".join(getattr(c, "text", "") for c in r.content)
        return False, r.structured_content

    async def ok(self, name: str, args: dict[str, Any] | None = None) -> Any:
        is_error, result = await self.call(name, args)
        assert not is_error, f"{name} failed: {result}"
        return result

    async def refused(self, name: str, args: dict[str, Any] | None = None) -> str:
        is_error, result = await self.call(name, args)
        assert is_error, f"{name} should have been refused, got {result}"
        return result


@pytest.fixture
def mworld(gworld):
    """gworld with a state file naming alpha active and beta pulled, and a server pointed at both."""
    gworld.state_path = write_state(gworld.root / "state.json", "alpha", ["beta"])
    gworld.server = build_server(gworld.registry_path, gworld.state_path)
    return gworld


@pytest.fixture
def mcp(mworld):
    return Mcp(mworld.server, mworld.state_path)


def run(coro):
    return asyncio.run(coro)
