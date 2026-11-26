"""daedalus-mcp: the MCP server the Claude desktop app talks to, over stdio.

A thin layer over daedalus.core. Claude can read the session's vaults and the
active project's code, and propose new notes. It can't approve, apply,
reject, edit, or write logs: those tools are never registered here.

The tool bodies live in daedalus.toolset, shared with the terminal agent.

Three rules this file keeps:
  * The session is loaded from state.json on every tool call, never cached,
    so `python -m daedalus.state set-active <name>` takes effect on Claude's
    next call without restarting the app.
  * propose_note has no source parameter; the server always passes
    source="claude", so Claude can't file its work as yours.
  * Nothing but protocol messages goes to stdout. Logs go to stderr and to
    ~/.local/state/daedalus/mcp.log.
"""

from __future__ import annotations

import argparse
import functools
import logging
import os
from pathlib import Path
from typing import Any, Callable, TypeVar

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from daedalus import toolset
from daedalus.core import queue
from daedalus.core.state import load_session

SOURCE = "claude"

log = logging.getLogger("daedalus.mcp")

F = TypeVar("F", bound=Callable[..., Any])

INSTRUCTIONS = """\
Daedalus gives you read access to the user's Obsidian project vaults, the shared vault of practices and \
lessons, and the active project's code. You can't change anything directly: propose_note queues a new note \
for the user to approve in their terminal. Call get_scope first to see which project is active. Notes are \
addressed as vault:path/to/note.md (for example shared:practices/red-test-first.md) or by bare wikilink \
target (red-test-first)."""

refusal = toolset.refusal


def _tool_errors(name: str) -> Callable[[F], F]:
    """Turn every refusal into a tool error; the server never crashes on one."""

    def wrap(fn: F) -> F:
        @functools.wraps(fn)
        def inner(*args: Any, **kwargs: Any) -> Any:
            log.info("call %s %s", name, _brief(kwargs))
            try:
                return fn(*args, **kwargs)
            except ToolError:
                raise
            except Exception as e:
                text = toolset.error_text(e)
                if text is None:
                    log.exception("internal error in %s", name)
                    raise ToolError(f"daedalus-mcp internal error in {name} ({type(e).__name__}: {e})") from None
                # A refusal is the server working as designed: it's logged once
                # here at INFO, and FastMCP's own "Error calling tool" line for it
                # is demoted to DEBUG so the log doesn't read like a fault.
                log.info("refused %s: %s", name, text)
                raise ToolError(text, log_level=logging.DEBUG) from None

        return inner  # type: ignore[return-value]

    return wrap


def _brief(kwargs: dict[str, Any]) -> str:
    """Arguments for the log, with long strings (note content) shown by size only."""
    return str({k: (f"<{len(v)} chars>" if isinstance(v, str) and len(v) > 120 else v) for k, v in kwargs.items()})


def build_server(registry_path: str | os.PathLike[str] | None = None,
                 state_path: str | os.PathLike[str] | None = None) -> FastMCP:
    """The server object. Paths default to ~/.config/daedalus/{vaults.toml,state.json}."""
    mcp = FastMCP("daedalus", instructions=INSTRUCTIONS, mask_error_details=False)

    def session():
        return load_session(registry_path, state_path)  # every call: switching projects needs no restart

    def describe(name: str) -> str:
        return toolset.description(name, SOURCE)

    @mcp.tool(name="get_scope", description=describe("get_scope") + (
        " The scope follows the user's current project, so it can change between calls."))
    @_tool_errors("get_scope")
    def get_scope() -> dict[str, Any]:
        return toolset.get_scope(session())

    @mcp.tool(name="list_notes", description=describe("list_notes"))
    @_tool_errors("list_notes")
    def list_notes(folder: str | None = None, type: str | None = None, status: str | None = None) -> dict[str, Any]:
        return toolset.list_notes(session(), folder, type, status)

    @mcp.tool(name="read_note", description=describe("read_note"))
    @_tool_errors("read_note")
    def read_note(ref: str) -> dict[str, Any]:
        return toolset.read_note(session(), ref)

    @mcp.tool(name="search", description=describe("search"))
    @_tool_errors("search")
    def search(query: str, limit: int = 20) -> dict[str, Any]:
        return toolset.search(session(), query, limit)

    @mcp.tool(name="backlinks", description=describe("backlinks"))
    @_tool_errors("backlinks")
    def backlinks(ref: str) -> dict[str, Any]:
        return toolset.backlinks(session(), ref)

    @mcp.tool(name="list_files", description=describe("list_files"))
    @_tool_errors("list_files")
    def list_files(subdir: str = "") -> dict[str, Any]:
        return toolset.list_files(session(), subdir)

    @mcp.tool(name="read_code", description=describe("read_code"))
    @_tool_errors("read_code")
    def read_code(rel_path: str, start_line: int | None = None, end_line: int | None = None) -> dict[str, Any]:
        return toolset.read_code(session(), rel_path, start_line, end_line)

    @mcp.tool(name="propose_note", description=describe("propose_note"))
    @_tool_errors("propose_note")
    def propose_note(rel_path: str, content: str, summary: str, vault: str | None = None) -> dict[str, Any]:
        result = toolset.propose_note(session(), SOURCE, rel_path, content, summary, vault)
        log.info("proposed %s -> %s:%s", result["id"], result["vault"], result["rel_path"])
        return result

    return mcp


# Running over stdio --------------------------------------------------------------------

def default_log_path() -> Path:
    return queue.STATE_ROOT / "mcp.log"


def setup_logging(log_path: Path) -> None:
    """stderr and a log file; never stdout, which carries the protocol."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(fmt)
    for name in ("daedalus", "fastmcp", "mcp"):
        logger = logging.getLogger(name)
        logger.addHandler(file_handler)
        if name == "daedalus":
            err = logging.StreamHandler()  # stderr
            err.setFormatter(fmt)
            logger.addHandler(err)
            logger.setLevel(logging.INFO)
            logger.propagate = False


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="daedalus-mcp", description="Daedalus MCP server (stdio).")
    parser.add_argument("--registry", help="vault registry (default ~/.config/daedalus/vaults.toml)")
    parser.add_argument("--state", help="session state (default ~/.config/daedalus/state.json)")
    parser.add_argument("--log-file", help="log file (default ~/.local/state/daedalus/mcp.log)")
    args = parser.parse_args(argv)

    setup_logging(Path(os.path.expanduser(args.log_file)) if args.log_file else default_log_path())
    server = build_server(args.registry, args.state)
    log.info("daedalus-mcp starting (pid %d)", os.getpid())
    try:
        server.run(transport="stdio", show_banner=False)
    finally:
        log.info("daedalus-mcp stopped")


if __name__ == "__main__":
    main()
