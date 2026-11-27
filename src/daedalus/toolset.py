"""The agent tools, shared by daedalus-mcp (Claude, nine) and the terminal agent (DeepSeek: twelve in chat,
fourteen in coder mode).

Each function takes the Session first and returns a JSON-ready dict. The
front end decides where the session comes from and what ``source`` a
proposal carries; the model never supplies either.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from daedalus.core import accept, gate, queue, tools
from daedalus.core.schema import DESIGN_CATEGORIES
from daedalus.core.notes import NoteSchemaError
from daedalus.core.policy import PolicyError
from daedalus.core.registry import SHARED_NAME, RegistryError
from daedalus.core.session import Session, SessionError
from daedalus.core.state import StateError
from daedalus.core.worktree import WorktreeError
from daedalus.sandbox import runner
from daedalus.sandbox.errors import SandboxError

# daedalus-mcp's exact tool set (Claude), in order.
NAMES = ("get_scope", "list_notes", "read_note", "search", "backlinks", "list_files", "read_code", "propose_note",
         "find_precedent")
# The terminal agent's (DeepSeek) adds the two /end needs (CONTEXT.md and inbox edits, and lessons), and
# ask_user, which ends its turn. Claude asks in its own chat, so daedalus-mcp has no ask_user.
AGENT_NAMES = NAMES + ("propose_edit", "propose_lesson", "ask_user")
# Coder mode (/code <slug>): the eight read and propose tools, the code tools, and the gate's two. Claude
# never writes code, so daedalus-mcp has none of these.
CODER_NAMES = NAMES[:8] + ("edit_code", "write_code", "delete_code", "run", "find_precedent", "ask_user")
CODE_TOOLS = ("edit_code", "write_code", "delete_code", "run")

RUN_STDOUT_CHARS = 20_000  # what the model sees of a run: the end, where pytest puts its summary
RUN_STDERR_CHARS = 8_000

log = logging.getLogger("daedalus.code")

MAX_NOTES = 2000
MAX_SEARCH = 50

# Refusals whose message already says what went wrong and how to fix it.
PASS_THROUGH = (
    NoteSchemaError, StateError, RegistryError, SessionError, queue.ProposalError, tools.RefError, tools.ReadError,
    tools.CodeEditError, SandboxError, WorktreeError,
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
    scope = {
        "active": s.active,
        "pulled": list(s.pulled),
        "shared": SHARED_NAME,
        "repo": str(s.active_repo),
        "vaults": {name: str(root) for name, root in s.scope},
    }
    if s.worktree is not None:  # coder mode only; daedalus-mcp's sessions never have one
        scope["worktree"] = str(s.worktree)
        scope["note"] = "read_code, list_files, and the edit tools work in the worktree; run runs checks there."
    return scope


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


def propose_edit(s: Session, source: str, rel_path: str, content: str, summary: str,
                 vault: str | None = None) -> dict[str, Any]:
    p = tools.propose_edit(s, vault or s.active, rel_path, content, source, summary)
    return {"id": p.id, "status": p.status, "vault": p.vault, "rel_path": p.rel_path, "source": p.source,
            "base_sha256": p.base_sha256,
            "message": "Queued. Nothing is written until Mohamed approves it in the terminal."}


def propose_lesson(s: Session, source: str, rel_path: str, content: str, summary: str) -> dict[str, Any]:
    p = tools.propose_lesson(s, rel_path, content, source, summary)
    return {"id": p.id, "status": p.status, "vault": p.vault, "rel_path": p.rel_path, "source": p.source,
            "message": "Queued in the shared inbox/. Nothing is written until Mohamed approves it in the terminal."}


def find_precedent(s: Session, topic: str, category: str | None = None) -> dict[str, Any]:
    found = gate.find_precedent(s, topic, category)
    return {
        "precedents": [p._asdict() for p in found],
        "note": ("Each is accepted precedent: cite one in related (a decision) or shared_refs (a practice)."
                 if found else "No precedent. For a design choice, ask with ask_user and stop; don't decide it."),
    }


def ask_user(s: Session, source: str, question: str, category: str, options: list[str],
             context_refs: list[str]) -> dict[str, Any]:
    asked = accept.ask_user(s, source, question, category, options, context_refs)
    p = asked.proposal
    return {
        "id": p.id, "status": p.status, "vault": p.vault, "rel_path": p.rel_path, "source": p.source,
        "question": asked.question, "category": asked.category, "options": list(asked.options),
        "message": (f"Question queued as {p.id}. Your turn ends now: make no more tool calls and don't decide this "
                    f"yourself. Mohamed answers with /answer {p.id} <option>; his answer becomes an accepted "
                    "decision you can then find with find_precedent."),
    }


def _edit(e: tools.Edit) -> dict[str, Any]:
    return {"op": e.op, "rel_path": e.rel_path, "bytes_before": e.bytes_before, "bytes_after": e.bytes_after,
            "message": f"{e.op} {e.rel_path}: done, in the worktree (committed later, not by you)."}


def edit_code(s: Session, rel_path: str, old: str, new: str, edit_log: tools.EditLog | None = None) -> dict[str, Any]:
    return _edit(tools.edit_code(s, rel_path, old, new, edit_log))


def write_code(s: Session, rel_path: str, content: str, edit_log: tools.EditLog | None = None) -> dict[str, Any]:
    return _edit(tools.write_code(s, rel_path, content, edit_log))


def delete_code(s: Session, rel_path: str, edit_log: tools.EditLog | None = None) -> dict[str, Any]:
    return _edit(tools.delete_code(s, rel_path, edit_log))


def _tail(text: str, limit: int) -> tuple[str, bool]:
    return (text, False) if len(text) <= limit else (text[-limit:], True)


def run(s: Session, argv: list[str]) -> dict[str, Any]:
    """10a's sandboxed runner, in the session's worktree. repl.log gets metadata only, never output."""
    if s.worktree is None:
        raise WorktreeError("no worktree is active, and commands run only in a worktree")
    try:
        result = runner.run(s, argv, s.worktree)
    except Exception as e:
        log.info("run %s (%d args) refused: %s", argv[0] if argv and isinstance(argv[0], str) else "?",
                 max(0, len(argv) - 1) if isinstance(argv, list) else 0, type(e).__name__)
        raise
    log.info("run %s (%d args): exit %d, %.2f s, timed out %s, stdout %d chars, stderr %d chars", argv[0],
             len(argv) - 1, result.exit_code, result.duration_s, result.timed_out, len(result.stdout_tail),
             len(result.stderr_tail))
    out, out_cut = _tail(result.stdout_tail, RUN_STDOUT_CHARS)
    err, err_cut = _tail(result.stderr_tail, RUN_STDERR_CHARS)
    return {"exit_code": result.exit_code, "duration_s": result.duration_s, "timed_out": result.timed_out,
            "stdout_tail": out, "stderr_tail": err,
            **({"note": "output was cut to its last part"} if out_cut or err_cut else {})}


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
    "find_precedent": (
        "Read-only. Finds precedent for a design choice: accepted notes in the active project's decisions/ and the "
        "shared vault's practices/ that mention any word of topic, best matches first, at most 10, each with its "
        "ref, title, status, and first 300 characters. Only these are precedent: drafts, proposed notes, other "
        "folders, and pulled projects' decisions never are. category (one of " + ", ".join(DESIGN_CATEGORIES) +
        ") puts notes that mention it first. Call it before any design choice: a schema, a new dependency, an "
        "interface between components, or a data source."
    ),
    "ask_user": (
        "Asks Mohamed a design question when find_precedent found no precedent for a design choice (category one "
        "of " + ", ".join(DESIGN_CATEGORIES) + "). Queues a question note in questions/ with your options "
        "(at least two) and links to the notes that give context (context_refs, at least one; not AGENTS or "
        "CONTEXT). It ENDS YOUR TURN: no further tool calls run, and you must not make the choice yourself. His "
        "answer becomes an accepted decision."
    ),
    "propose_edit": (
        "Creates a pending proposal to replace an existing note's whole content; it changes nothing until "
        "Mohamed approves it in the terminal. The proposal records the note's current hash, so if the note "
        "changes before approval, the approval is refused as stale. Allowed only for the active project's "
        "CONTEXT.md and inbox/ notes (questions/ too), and the shared vault's inbox/ with vault='shared'. "
        "content is the complete new file, not a diff. An agent can't change a note's source. summary is "
        "one line, at most 200 characters."
    ),
    "edit_code": (
        "Edits a file in the worktree (your own git worktree of the project, on its agent/ branch): replaces the "
        "one occurrence of old with new. old must occur EXACTLY ONCE in the file, copied exactly, whitespace "
        "included; zero or several matches are refused with the count, so add surrounding lines to make it unique. "
        "This is the default way to change code: it costs far fewer tokens than rewriting a file. Paths are "
        "relative to the worktree root. vault/ and .git are off limits. The file's mode is kept."
    ),
    "write_code": (
        f"Creates a file in the worktree, or replaces a small one whole (at most {tools.MAX_WRITE_BYTES // 1024} KB). "
        "Use it for new files; change existing files with edit_code. UTF-8 text only. Missing folders are made."
    ),
    "delete_code": "Deletes a file in the worktree.",
    "run": (
        "Runs one command in the worktree, sandboxed: no network, no writes (the worktree is read-only to the "
        "run), a clean environment, and a timeout. argv is a list, e.g. [\"pytest\", \"-q\"]; argv[0] must be "
        "a program the project's daedalus.toml allows, and it runs from the project's own venv. Returns exit_code, "
        "duration_s, timed_out, and the end of stdout and stderr. Run the tests after every change."
    ),
    "propose_lesson": (
        "Creates a pending lesson in the shared vault's inbox/ (rel_path is relative to inbox/, e.g. "
        "'git-hooks-path.md'); it changes nothing until Mohamed approves it. A lesson records something that "
        "failed, why it failed, and the fix; nothing else qualifies. Frontmatter type lesson, with applies_to "
        "tags, source '{source}', and at least one link (usually a related practice)."
    ),
}


# In coder mode the code tools read the worktree, so two descriptions change there (daedalus-mcp's don't).
CODER_DESCRIPTIONS = {
    "list_files": (
        "Read-only. Lists the worktree's files as git sees them (tracked and untracked, minus .gitignore'd files), "
        f"optionally under subdir. vault/ is left out. Returns at most {tools.MAX_FILES} paths."
    ),
    "read_code": (
        f"Read-only. Reads a text file in the worktree by worktree-relative path, including your own edits. Returns "
        f"up to {tools.MAX_LINES} lines (and at most {tools.MAX_CHARS:,} characters) per call, plus the total line "
        "count; page with start_line. Binary files and vault/ are refused."
    ),
}


def description(name: str, source: str, profile: str = "chat") -> str:
    text = CODER_DESCRIPTIONS.get(name) if profile == "coder" else None
    return (text or DESCRIPTIONS[name]).replace("{source}", source)
