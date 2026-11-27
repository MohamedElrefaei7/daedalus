"""Helpers for the daedalus-mcp tests (a plain module, so the name is unambiguous)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastmcp import Client

TOOLS = {"get_scope", "list_notes", "read_note", "search", "backlinks", "list_files", "read_code", "propose_note",
         "find_precedent", "submit_plan", "revise_plan", "plan_status", "list_plans"}
PLAN_TOOLS = {"submit_plan", "revise_plan", "plan_status", "list_plans"}


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


def run(coro):
    return asyncio.run(coro)
