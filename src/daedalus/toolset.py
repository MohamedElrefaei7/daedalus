"""The eight agent tools, shared by daedalus-mcp (Claude) and the terminal agent (DeepSeek).

Each function takes the Session first and returns a JSON-ready dict. The
front end decides where the session comes from and what ``source`` a
proposal carries; the model never supplies either.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from daedalus.core import queue, tools
from daedalus.core.notes import NoteSchemaError
from daedalus.core.policy import PolicyError
from daedalus.core.registry import SHARED_NAME, RegistryError
from daedalus.core.session import Session, SessionError
from daedalus.core.state import StateError

# The exact tool set, in the order the agent declares them.
NAMES = ("get_scope", "list_notes", "read_note", "search", "backlinks", "list_files", "read_code", "propose_note")

MAX_NOTES = 2000
MAX_SEARCH = 50

# Refusals whose message already says what went wrong and how to fix it.
PASS_THROUGH = (
    NoteSchemaError, StateError, RegistryError, SessionError, queue.ProposalError, tools.RefError, tools.ReadError,
)


def refusal(e: PolicyError) -> str:
    return f"Refused by rule {e.rule}: {e.reason}. (path: {e.path})"


def error_text(e: Exception) -> str | None:
    """The message a refusal goes back with, or None if ``e`` isn't a refusal (a real fault)."""
    if isinstance(e, PolicyError):
        return refusal(e)
    if isinstance(e, PASS_THROUGH):
        return str(e)
    return None


def json_safe(value: Any) -> Any:
    """YAML frontmatter can hold dates; tool results are JSON."""
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, (dt.date, dt.datetime, dt.time)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def get_scope(s: Session) -> dict[str, Any]:
    return {
        "active": s.active,
        "pulled": list(s.pulled),
        "shared": SHARED_NAME,
        "repo": str(s.active_repo),
        "vaults": {name: str(root) for name, root in s.scope},
    }


def list_notes(s: Session, folder: str | None = None, type: str | None = None,
               status: str | None = None) -> dict[str, Any]:
    entries = tools.list_notes(s, folder=folder, type=type, status=status)
    return {"notes": [e._asdict() for e in entries[:MAX_NOTES]], "truncated": len(entries) > MAX_NOTES}


def read_note(s: Session, ref: str) -> dict[str, Any]:
    match = tools.resolve_ref(s, ref)
    note, errors = tools.read_note(s, f"{match.vault}:{match.rel_path}", strict=False)
    return {
        "vault": match.vault,
        "rel_path": match.rel_path,
        "frontmatter": json_safe(note.frontmatter),
        "body": note.body,
        "schema_errors": errors,
    }


def search(s: Session, query: str, limit: int = 20) -> dict[str, Any]:
    hits = tools.search(s, query, max(1, min(limit, MAX_SEARCH)))
    return {"hits": [h._asdict() for h in hits]}


def backlinks(s: Session, ref: str) -> dict[str, Any]:
    return {"backlinks": [{**b._asdict(), "via": list(b.via)} for b in tools.backlinks(s, ref)]}


def list_files(s: Session, subdir: str = "") -> dict[str, Any]:
    result = tools.list_files(s, subdir)
    return {"files": result.files, "truncated": result.truncated}


def read_code(s: Session, rel_path: str, start_line: int | None = None, end_line: int | None = None) -> dict[str, Any]:
    return tools.read_code(s, rel_path, start_line, end_line)._asdict()


def propose_note(s: Session, source: str, rel_path: str, content: str, summary: str,
                 vault: str | None = None) -> dict[str, Any]:
    p = tools.propose_note(s, vault or s.active, rel_path, content, source, summary)
    return {
        "id": p.id,
        "status": p.status,
        "vault": p.vault,
        "rel_path": p.rel_path,
        "source": p.source,
        "message": "Queued. Nothing is written until Mohamed approves it in the terminal.",
    }


# Descriptions, written for the model. Both front ends use these words.
DESCRIPTIONS = {
    "get_scope": (
        "Read-only. Shows what this session can see: the active project, any projects pulled in for "
        "reading, the shared vault's name, and the active project's repo path. Call it first."
    ),
    "list_notes": (
        "Read-only. Lists notes in the active vault, pulled vaults, and the shared vault, with each note's "
        "vault, path, type, and status (status INVALID means its frontmatter breaks the schema). Filter by "
        "folder (e.g. 'decisions'), type, or status. Templates are not notes and are left out. Returns at "
        f"most {MAX_NOTES} entries."
    ),
    "read_note": (
        "Read-only. Reads one note by 'vault:path/to/note.md' (e.g. 'shared:practices/red-test-first.md') or "
        "by bare wikilink target (e.g. 'red-test-first'). Returns its frontmatter, body, and any schema "
        "errors. An ambiguous target is refused with the matching paths, so retry with vault:path. Only "
        "vaults in get_scope can be read."
    ),
    "search": (
        "Read-only. Full-text search over every note in scope (active, pulled, and shared vaults), best "
        f"matches first, with a snippet each. limit defaults to 20 and is capped at {MAX_SEARCH}."
    ),
    "backlinks": (
        "Read-only. Lists every note in scope that links to the given note, from its body or a frontmatter "
        "key such as related or shared_refs. The note is named like read_note's ref."
    ),
    "list_files": (
        "Read-only. Lists the active project's repo files as git sees them (tracked and untracked, minus "
        ".gitignore'd files), optionally under subdir. The vault/ folder is left out: use the note tools for "
        f"notes. Returns at most {tools.MAX_FILES} paths and says when the list was cut short, so narrow it "
        "with subdir."
    ),
    "read_code": (
        f"Read-only. Reads a text file in the active project's repo by repo-relative path. Returns up to "
        f"{tools.MAX_LINES} lines (and at most {tools.MAX_CHARS:,} characters) per call, plus the file's "
        "total line count; page with start_line (next_start_line says where to continue). Binary files, "
        "vault/ notes, and paths outside the repo are refused."
    ),
    "propose_note": (
        "Creates a pending proposal for a new note in the active project's inbox/ (or questions/ for a "
        "question note); it changes nothing until Mohamed approves it in the terminal. Pass vault='shared' "
        "to propose into the shared vault's inbox/. rel_path is relative to the vault root, e.g. "
        "'inbox/idea.md'. content is the whole note; if it has frontmatter, it must follow the schema in the "
        "vault's AGENTS.md, and its source must be '{source}' (proposals from this tool are always recorded "
        "as source {source}). A typed note must link at least one other note, in related, shared_refs, or "
        "project_refs, or in the body; AGENTS.md and CONTEXT.md are never link targets. summary is one line, at most 200 characters, and becomes the commit subject. "
        "Refused: any other folder (practices/, decisions/, ...), editing an existing note, paths outside "
        "the vault, and drafts that break the schema; the error says which rule and why."
    ),
}


def description(name: str, source: str) -> str:
    return DESCRIPTIONS[name].replace("{source}", source)
