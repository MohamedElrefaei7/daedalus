"""daedalus-mcp: the MCP server the Claude desktop app talks to, over stdio.

A thin layer over daedalus.core. Claude can read the session's vaults and the
active project's code, and propose new notes. It can't approve, apply,
reject, edit, or write logs: those tools are never registered here.

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
import datetime as dt
import functools
import logging
import os
from pathlib import Path
from typing import Any, Callable, TypeVar

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from daedalus.core import queue, tools
from daedalus.core.notes import NoteSchemaError
from daedalus.core.policy import PolicyError
from daedalus.core.registry import SHARED_NAME, RegistryError
from daedalus.core.session import SessionError
from daedalus.core.state import StateError, load_session

SOURCE = "claude"
MAX_NOTES = 2000
MAX_SEARCH = 50

log = logging.getLogger("daedalus.mcp")

F = TypeVar("F", bound=Callable[..., Any])

# Refusals whose message already says what went wrong and how to fix it.
_PASS_THROUGH = (
    NoteSchemaError, StateError, RegistryError, SessionError, queue.ProposalError, tools.RefError, tools.ReadError,
)

INSTRUCTIONS = """\
Daedalus gives you read access to the user's Obsidian project vaults, the shared vault of practices and \
lessons, and the active project's code. You can't change anything directly: propose_note queues a new note \
for the user to approve in their terminal. Call get_scope first to see which project is active. Notes are \
addressed as vault:path/to/note.md (for example shared:practices/red-test-first.md) or by bare wikilink \
target (red-test-first)."""


def refusal(e: PolicyError) -> str:
    return f"Refused by rule {e.rule}: {e.reason}. (path: {e.path})"


def _tool_errors(name: str) -> Callable[[F], F]:
    """Turn every refusal into a tool error; the server never crashes on one."""

    def wrap(fn: F) -> F:
        @functools.wraps(fn)
        def inner(*args: Any, **kwargs: Any) -> Any:
            log.info("call %s %s", name, _brief(kwargs))
            try:
                return fn(*args, **kwargs)
            # A refusal is the server working as designed: it's logged once
            # here at INFO, and FastMCP's own "Error calling tool" line for it
            # is demoted to DEBUG so the log doesn't read like a fault.
            except PolicyError as e:
                log.info("refused %s: rule %s: %s", name, e.rule, e.reason)
                raise ToolError(refusal(e), log_level=logging.DEBUG) from None
            except _PASS_THROUGH as e:
                log.info("refused %s: %s", name, e)
                raise ToolError(str(e), log_level=logging.DEBUG) from None
            except ToolError:
                raise
            except Exception as e:
                log.exception("internal error in %s", name)
                raise ToolError(f"daedalus-mcp internal error in {name} ({type(e).__name__}: {e})") from None

        return inner  # type: ignore[return-value]

    return wrap


def _brief(kwargs: dict[str, Any]) -> str:
    """Arguments for the log, with long strings (note content) shown by size only."""
    return str({k: (f"<{len(v)} chars>" if isinstance(v, str) and len(v) > 120 else v) for k, v in kwargs.items()})


def _json_safe(value: Any) -> Any:
    """YAML frontmatter can hold dates; MCP results are JSON."""
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (dt.date, dt.datetime, dt.time)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def build_server(registry_path: str | os.PathLike[str] | None = None,
                 state_path: str | os.PathLike[str] | None = None) -> FastMCP:
    """The server object. Paths default to ~/.config/daedalus/{vaults.toml,state.json}."""
    mcp = FastMCP("daedalus", instructions=INSTRUCTIONS, mask_error_details=False)

    def session():
        return load_session(registry_path, state_path)  # every call: switching projects needs no restart

    @mcp.tool(name="get_scope", description=(
        "Read-only. Shows what this session can see: the active project, any projects pulled in for "
        "reading, the shared vault's name, and the active project's repo path. Call it first. The scope "
        "follows the user's current project, so it can change between calls."
    ))
    @_tool_errors("get_scope")
    def get_scope() -> dict[str, Any]:
        s = session()
        return {
            "active": s.active,
            "pulled": list(s.pulled),
            "shared": SHARED_NAME,
            "repo": str(s.active_repo),
            "vaults": {name: str(root) for name, root in s.scope},
        }

    @mcp.tool(name="list_notes", description=(
        "Read-only. Lists notes in the active vault, pulled vaults, and the shared vault, with each note's "
        "vault, path, type, and status (status INVALID means its frontmatter breaks the schema). Filter by "
        "folder (e.g. 'decisions'), type, or status. Templates are not notes and are left out. Returns at "
        f"most {MAX_NOTES} entries."
    ))
    @_tool_errors("list_notes")
    def list_notes(folder: str | None = None, type: str | None = None, status: str | None = None) -> dict[str, Any]:
        entries = tools.list_notes(session(), folder=folder, type=type, status=status)
        return {"notes": [e._asdict() for e in entries[:MAX_NOTES]], "truncated": len(entries) > MAX_NOTES}

    @mcp.tool(name="read_note", description=(
        "Read-only. Reads one note by 'vault:path/to/note.md' (e.g. 'shared:practices/red-test-first.md') or "
        "by bare wikilink target (e.g. 'red-test-first'). Returns its frontmatter, body, and any schema "
        "errors. An ambiguous target is refused with the matching paths, so retry with vault:path. Only "
        "vaults in get_scope can be read."
    ))
    @_tool_errors("read_note")
    def read_note(ref: str) -> dict[str, Any]:
        s = session()
        match = tools.resolve_ref(s, ref)
        note, errors = tools.read_note(s, f"{match.vault}:{match.rel_path}", strict=False)
        return {
            "vault": match.vault,
            "rel_path": match.rel_path,
            "frontmatter": _json_safe(note.frontmatter),
            "body": note.body,
            "schema_errors": errors,
        }

    @mcp.tool(name="search", description=(
        "Read-only. Full-text search over every note in scope (active, pulled, and shared vaults), best "
        f"matches first, with a snippet each. limit defaults to 20 and is capped at {MAX_SEARCH}."
    ))
    @_tool_errors("search")
    def search(query: str, limit: int = 20) -> dict[str, Any]:
        hits = tools.search(session(), query, max(1, min(limit, MAX_SEARCH)))
        return {"hits": [h._asdict() for h in hits]}

    @mcp.tool(name="backlinks", description=(
        "Read-only. Lists every note in scope that links to the given note, from its body or a frontmatter "
        "key such as related or shared_refs. The note is named like read_note's ref."
    ))
    @_tool_errors("backlinks")
    def backlinks(ref: str) -> dict[str, Any]:
        return {"backlinks": [{**b._asdict(), "via": list(b.via)} for b in tools.backlinks(session(), ref)]}

    @mcp.tool(name="list_files", description=(
        "Read-only. Lists the active project's repo files as git sees them (tracked and untracked, minus "
        ".gitignore'd files), optionally under subdir. The vault/ folder is left out: use the note tools for "
        f"notes. Returns at most {tools.MAX_FILES} paths and says when the list was cut short, so narrow it "
        "with subdir."
    ))
    @_tool_errors("list_files")
    def list_files(subdir: str = "") -> dict[str, Any]:
        result = tools.list_files(session(), subdir)
        return {"files": result.files, "truncated": result.truncated}

    @mcp.tool(name="read_code", description=(
        f"Read-only. Reads a text file in the active project's repo by repo-relative path. Returns up to "
        f"{tools.MAX_LINES} lines (and at most {tools.MAX_CHARS:,} characters) per call, plus the file's "
        "total line count; page with start_line (next_start_line says where to continue). Binary files, "
        "vault/ notes, and paths outside the repo are refused."
    ))
    @_tool_errors("read_code")
    def read_code(rel_path: str, start_line: int | None = None, end_line: int | None = None) -> dict[str, Any]:
        return tools.read_code(session(), rel_path, start_line, end_line)._asdict()

    @mcp.tool(name="propose_note", description=(
        "Creates a pending proposal for a new note in the active project's inbox/ (or questions/ for a "
        "question note); it changes nothing until Mohamed approves it in the terminal. Pass vault='shared' "
        "to propose into the shared vault's inbox/. rel_path is relative to the vault root, e.g. "
        "'inbox/idea.md'. content is the whole note; if it has frontmatter, it must follow the schema in the "
        "vault's AGENTS.md, and its source must be 'claude' (proposals from this tool are always recorded as "
        "source claude). summary is one line, at most 200 characters, and becomes the commit subject. "
        "Refused: any other folder (practices/, decisions/, ...), editing an existing note, paths outside "
        "the vault, and drafts that break the schema; the error says which rule and why."
    ))
    @_tool_errors("propose_note")
    def propose_note(rel_path: str, content: str, summary: str, vault: str | None = None) -> dict[str, Any]:
        s = session()
        p = tools.propose_note(s, vault or s.active, rel_path, content, SOURCE, summary)
        log.info("proposed %s -> %s:%s", p.id, p.vault, p.rel_path)
        return {
            "id": p.id,
            "status": p.status,
            "vault": p.vault,
            "rel_path": p.rel_path,
            "source": p.source,
            "message": "Queued. Nothing is written until Mohamed approves it in the terminal.",
        }

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
