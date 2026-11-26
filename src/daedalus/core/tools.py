"""The tools agents call.

Read tools: list_notes, read_note, backlinks, search. Scope is the session's
active vault, its pulled vaults, and the shared vault; every file is read
through notes.read_note, so every read passes the policy.

Code read tools: list_files, read_code. Scope is the active repo minus its
vault/ (the note tools cover that), read through the same policy, with caps
on how much one call returns.

Write tools: propose_note, propose_edit, propose_lesson write only to the
queue, never to a vault or a repo. write_log creates one log file and commits
it at once; logs are records, not decisions, so they need no approval.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import NamedTuple

from . import index, notes, queue
from .apply import repo_for, vault_root
from .vaultio import (
    check_still_authorized, commit_path, git, open_dir_nofollow, restore_at, unstage, write_lock,
)
from .links import Catalog, Match, link_scope, parse_link, resolve
from .policy import Op, PolicyError, authorize
from .queue import CommitFailed, DraftInvalid, ProposalError, ProposalTooLarge, Proposal
from .registry import SHARED_NAME
from .schema import SCHEMA
from .session import Area, Session


class RefError(LookupError):
    """A note reference names nothing in scope."""


class AmbiguousRefError(RefError):
    def __init__(self, ref: str, matches: tuple[Match, ...]) -> None:
        self.ref = ref
        self.matches = matches
        names = ", ".join(f"{m.vault}:{m.rel_path}" for m in matches)
        super().__init__(f"[[{ref}]] is ambiguous; it matches {names}. Use vault:path to pick one.")


class NoteEntry(NamedTuple):
    vault: str
    rel_path: str
    type: str | None
    status: str | None  # "INVALID" if the note fails validation


class Backlink(NamedTuple):
    vault: str
    rel_path: str
    via: tuple[str, ...]  # "body" and/or the frontmatter keys holding the link
    ambiguous: bool  # the link also matches other notes


class SearchHit(NamedTuple):
    vault: str
    rel_path: str
    snippet: str


INVALID = "INVALID"


def _catalogs(session: Session) -> dict[str, Catalog]:
    return {
        vault: Catalog.build(vault, (rel for rel, _ in index.walk_markdown(root)))
        for vault, root in session.scope
    }


def list_notes(
    session: Session, folder: str | None = None, type: str | None = None, status: str | None = None
) -> list[NoteEntry]:
    """Notes in scope, optionally filtered. templates/ is skipped by index.walk_markdown."""
    prefix = folder.strip("/") + "/" if folder else None
    entries = []
    for vault, root in session.scope:
        for rel, _ in sorted(index.walk_markdown(root), key=lambda item: item[0]):
            if prefix and not rel.startswith(prefix):
                continue
            try:
                note, errors = notes.read_note(root / rel, session, strict=False)
            except UnicodeDecodeError:
                entries.append(NoteEntry(vault, rel, None, INVALID))
                continue
            fm = note.frontmatter
            note_type = fm.get("type") if isinstance(fm.get("type"), str) else None
            note_status = INVALID if errors else (fm.get("status") if isinstance(fm.get("status"), str) else None)
            entries.append(NoteEntry(vault, rel, note_type, note_status))
    return [
        e for e in entries if (type is None or e.type == type) and (status is None or e.status == status)
    ]


def resolve_ref(session: Session, ref: str, catalogs: dict[str, Catalog] | None = None) -> Match:
    """Turn ``vault:rel/path.md`` or a bare wikilink target into one note."""
    ref = ref.strip()
    if ref.startswith("[[") and ref.endswith("]]"):
        ref = ref[2:-2]
    vault, sep, rest = ref.partition(":")
    in_scope = [name for name, _ in session.scope]
    if sep:
        if vault not in in_scope:
            raise RefError(f"{ref!r}: vault {vault!r} is not in this session (in scope: {', '.join(in_scope)})")
        if not rest or rest.startswith("/"):
            raise RefError(f"{ref!r}: expected vault:rel/path.md")
        return Match(vault, rest)
    link = parse_link(ref)
    if link is None:
        raise RefError(f"{ref!r} names no note")
    catalogs = catalogs if catalogs is not None else _catalogs(session)
    res = resolve(link.target, catalogs.values())
    if not res.matches:
        raise RefError(f"[[{link.target}]] matches no note in {', '.join(in_scope)}")
    if res.ambiguous:
        raise AmbiguousRefError(link.target, res.matches)
    return res.matches[0]


def read_note(session: Session, ref: str, strict: bool = True):
    """Read one note by ``vault:rel/path.md`` or by bare wikilink target; refuses ambiguous targets."""
    m = resolve_ref(session, ref)
    return notes.read_note(session.vault_root(m.vault) / m.rel_path, session, strict=strict)


def backlinks(session: Session, ref: str) -> list[Backlink]:
    """Every note in scope that links to ``ref``, from its body or its frontmatter."""
    catalogs = _catalogs(session)
    target = resolve_ref(session, ref, catalogs)
    project_vaults = [session.active, *session.pulled]
    hits = []
    for vault, root in session.scope:
        for rel in catalogs[vault].rels:
            if (vault, rel) == (target.vault, target.rel_path):
                continue
            try:
                note, _ = notes.read_note(root / rel, session, strict=False)
            except UnicodeDecodeError:
                continue
            via: list[str] = []
            ambiguous = False
            for link in note.links:
                scope = link_scope(link.key, vault, project_vaults, SHARED_NAME)
                res = resolve(link.target, (catalogs[v] for v in scope))
                if target in res.matches:
                    ambiguous = ambiguous or res.ambiguous
                    if link.key not in via:
                        via.append(link.key)
            if via:
                hits.append(Backlink(vault, rel, tuple(via), ambiguous))
    return hits


def search(session: Session, query: str, limit: int = 20) -> list[SearchHit]:
    """Full-text search across scope, best matches first."""
    ranked = []
    for vault, root in session.scope:
        authorize(root, Op.READ, session)
        for hit in index.open_index(root).search(query, limit):
            ranked.append((hit.rank, vault, hit.rel_path, hit.snippet))
    ranked.sort(key=lambda r: r[0])
    return [SearchHit(vault, rel, snippet) for _, vault, rel, snippet in ranked[:limit]]


# Reading code -------------------------------------------------------------------------

MAX_FILES = 2000
MAX_LINES = 2000
MAX_CHARS = 200_000  # per read_code call, so one minified file can't return megabytes on one line
BINARY_SNIFF = 8192


class ReadError(Exception):
    """A code read was refused: not a file, binary, out of range, or a vault path."""


class FileList(NamedTuple):
    files: list[str]  # repo-relative, "/"-separated, sorted
    truncated: bool  # more paths matched than MAX_FILES


class CodeChunk(NamedTuple):
    path: str  # repo-relative
    start_line: int
    end_line: int  # inclusive; start_line - 1 when nothing was returned
    total_lines: int
    text: str
    next_start_line: int | None  # where the next page starts, or None at the end of the file


def _vault_rel(session: Session) -> str:
    return os.path.relpath(session.active_vault, session.active_repo).replace(os.sep, "/")


def _in_vault(rel: str, vault_rel: str) -> bool:
    return rel == vault_rel or rel.startswith(vault_rel + "/")


def _repo_target(session: Session, rel_path: str, what: str):
    """Authorize READ of ``rel_path`` in the active repo, refusing paths that land in a vault."""
    if not isinstance(rel_path, str) or os.path.isabs(rel_path):
        raise ReadError(f"{what} {rel_path!r} must be a path relative to the repo root")
    auth = authorize(session.active_repo / rel_path, Op.READ, session)
    if auth.area is not Area.ACTIVE_REPO:
        where = f"the {auth.vault} vault" if auth.vault else auth.area.value
        raise ReadError(f"{rel_path!r} is in {where}; vault notes are read with read_note and list_notes")
    return auth


def list_files(session: Session, subdir: str = "") -> FileList:
    """The active repo's files, as git sees them: tracked plus untracked, minus .gitignore'd, minus vault/.

    At most MAX_FILES paths; ``truncated`` says the list was cut short.
    """
    repo = session.active_repo
    subdir = (subdir or "").strip()
    pathspec: list[str] = []
    if subdir not in ("", ".", "/"):
        auth = _repo_target(session, subdir.rstrip("/"), "subdir")
        pathspec = ["--", auth.rel] if auth.rel else []
    out = git(repo, "--literal-pathspecs", "ls-files", "-z", "--cached", "--others", "--exclude-standard",
              *pathspec)
    vault_rel = _vault_rel(session)
    files: list[str] = []
    truncated = False
    for rel in sorted({p for p in out.split("\0") if p}):
        if _in_vault(rel, vault_rel) or not os.path.lexists(repo / rel):
            continue  # notes have their own tools; a deleted-but-staged path can't be read
        try:
            authorize(repo / rel, Op.READ, session)
        except PolicyError:
            continue  # list only what read_code would let you read
        if len(files) == MAX_FILES:
            truncated = True
            break
        files.append(rel)
    return FileList(files, truncated)


def read_code(session: Session, rel_path: str, start_line: int | None = None,
              end_line: int | None = None) -> CodeChunk:
    """Lines ``start_line``..``end_line`` (1-based, inclusive) of a text file in the active repo.

    At most MAX_LINES lines and MAX_CHARS characters per call; page with
    ``next_start_line``. Binary files (a NUL byte in the first 8 KB) are refused.
    """
    auth = _repo_target(session, rel_path, "rel_path")
    start = 1 if start_line is None else start_line
    if start < 1:
        raise ReadError(f"start_line is {start}; lines are numbered from 1")
    if end_line is not None and end_line < start:
        raise ReadError(f"end_line {end_line} is before start_line {start}")
    last = start + MAX_LINES - 1 if end_line is None else min(end_line, start + MAX_LINES - 1)

    try:
        f = open(auth.path, "rb")
    except IsADirectoryError:
        raise ReadError(f"{auth.rel} is a folder; use list_files") from None
    except FileNotFoundError:
        raise ReadError(f"{auth.rel} doesn't exist; use list_files to see what's there") from None
    with f:
        if not os.path.isfile(auth.path):
            raise ReadError(f"{auth.rel} is not a regular file")
        if b"\0" in f.read(BINARY_SNIFF):
            raise ReadError(f"{auth.rel} looks binary (a NUL byte in its first {BINARY_SNIFF} bytes), so it isn't returned")
        f.seek(0)
        kept: list[str] = []
        chars = 0
        cut = False
        total = 0
        for total, raw in enumerate(f, start=1):
            if start <= total <= last and not cut:
                line = raw.decode("utf-8", errors="replace")
                if chars + len(line) > MAX_CHARS:
                    if not kept:  # one line longer than the cap: return its head
                        kept.append(line[:MAX_CHARS] + "\n[line cut at the character cap]\n")
                    cut = True
                    continue
                kept.append(line)
                chars += len(line)
    if total and start > total:
        raise ReadError(f"start_line {start} is past the end of {auth.rel} ({total} lines)")
    end = start + len(kept) - 1
    return CodeChunk(auth.rel, start, end, total, "".join(kept), end + 1 if end < total else None)


# Proposing ----------------------------------------------------------------------------

MAX_SUMMARY = 200
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _check_source(source: str) -> None:
    allowed = SCHEMA["enums"]["source"]
    if source not in allowed:
        raise ProposalError(f"source is {source!r}, expected one of {allowed}")


def _check_summary(summary: str) -> str:
    # The summary becomes the commit subject, so a newline could forge a trailer.
    if not isinstance(summary, str) or not summary.strip():
        raise ProposalError("summary must be a non-empty line")
    if "\n" in summary or "\r" in summary:
        raise ProposalError("summary must be a single line")
    summary = summary.strip()
    if len(summary) > MAX_SUMMARY:
        raise ProposalError(f"summary is {len(summary)} characters; the cap is {MAX_SUMMARY}")
    return summary


def _check_size(content: str) -> None:
    if not isinstance(content, str):
        raise TypeError("content must be a str")
    size = len(content.encode("utf-8"))
    if size > queue.MAX_CONTENT_BYTES:
        raise ProposalTooLarge(f"content is {size} bytes; the cap is {queue.MAX_CONTENT_BYTES}")


def _propose(session: Session, vault: str, rel_path: str, content: str, source: str, summary: str,
             kind: str) -> Proposal:
    _check_source(source)
    summary = _check_summary(summary)
    _check_size(content)
    if not rel_path or os.path.isabs(rel_path) or not rel_path.casefold().endswith(".md"):
        raise ProposalError(f"rel_path {rel_path!r} must be a relative path to a .md note")

    auth = authorize(vault_root(session.registry, vault) / rel_path, Op.PROPOSE, session)
    if auth.vault is None:  # PROPOSE is only ever allowed inside a vault
        raise PolicyError(1, auth.path, "proposals go into a vault")
    repo_for(session.registry, auth.vault)  # there must be a repo to commit into
    if not auth.path.parent.is_dir():
        raise ProposalError(f"{auth.vault}:{auth.rel}: folder {auth.path.parent} doesn't exist")

    base_sha256 = base_content = None
    exists = os.path.lexists(auth.path)
    if kind == "create" and exists:
        raise ProposalError(f"{auth.vault}:{auth.rel} already exists; that's an edit, so use propose_edit")
    if kind == "edit":
        if not exists or not auth.path.is_file():
            raise ProposalError(f"{auth.vault}:{auth.rel} doesn't exist; that's a create, so use propose_note")
        base = auth.path.read_bytes()
        if base == content.encode("utf-8"):
            raise ProposalError(f"{auth.vault}:{auth.rel}: the edit changes nothing")
        try:
            base_content = base.decode("utf-8")
        except UnicodeDecodeError:
            raise ProposalError(f"{auth.vault}:{auth.rel} is not UTF-8 text, so it can't be edited") from None
        base_sha256 = hashlib.sha256(base).hexdigest()

    errors = notes.draft_errors(content, auth) + notes.source_errors(content, source, base_content)
    if errors:
        raise DraftInvalid(f"{auth.vault}:{auth.rel}", errors)

    return queue.insert(
        vault=auth.vault, rel_path=auth.rel, kind=kind, content=content, source=source, summary=summary,
        base_sha256=base_sha256, base_content=base_content,
    )


def propose_note(session: Session, vault: str, rel_path: str, content: str, source: str, summary: str) -> Proposal:
    """Queue the creation of a new note. Refused if the target exists."""
    return _propose(session, vault, rel_path, content, source, summary, "create")


def propose_edit(session: Session, vault: str, rel_path: str, content: str, source: str, summary: str) -> Proposal:
    """Queue a full replacement of an existing note, recording the hash it was based on."""
    return _propose(session, vault, rel_path, content, source, summary, "edit")


def propose_lesson(session: Session, rel_path: str, content: str, source: str, summary: str) -> Proposal:
    """propose_note into the shared vault's inbox/. ``rel_path`` is relative to inbox/."""
    rel = rel_path if rel_path.startswith("inbox/") else f"inbox/{rel_path}"
    return propose_note(session, SHARED_NAME, rel, content, source, summary)


# Logs -----------------------------------------------------------------------------------

def write_log(session: Session, run_id: str, body: str, source: str) -> str:
    """Create logs/<run_id>.md in the active vault and commit it; returns the commit sha.

    The file is created with the OS's exclusive create, never check-then-write.
    If it already exists, PolicyError(rule=5) and the existing log is untouched.
    """
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id) or ".." in run_id:
        raise ProposalError(f"run_id {run_id!r} must be letters, digits, '.', '-' or '_'")
    _check_source(source)
    _check_size(body)
    auth = authorize(session.active_vault / "logs" / f"{run_id}.md", Op.CREATE_LOG, session)
    repo = session.active_repo
    rel_to_repo = os.path.relpath(auth.path, repo)
    name = auth.path.name

    with write_lock():
        dirfd = open_dir_nofollow(auth.path.parent)

        def opener(path: str, flags: int) -> int:
            return os.open(path, flags | os.O_NOFOLLOW, 0o666, dir_fd=dirfd)

        try:
            path = name
            try:
                with open(path, "x", encoding="utf-8", newline="", opener=opener) as f:
                    f.write(body)
                    f.flush()
                    os.fsync(f.fileno())
            except FileExistsError:
                raise PolicyError(5, auth.path, "logs are create-only, and this log already exists") from None
            try:
                check_still_authorized(auth.path)
            except PolicyError:
                restore_at(dirfd, name, None)
                raise
            try:
                return commit_path(repo, rel_to_repo, [f"log({source}): {run_id}"], add=True)
            except CommitFailed:
                restore_at(dirfd, name, None)
                unstage(repo, rel_to_repo)
                raise
        finally:
            os.close(dirfd)
