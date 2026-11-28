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

import datetime as dt
import hashlib
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from . import index, notes, queue
from .apply import repo_for, vault_root
from .vaultio import (
    atomic_write_at, check_still_authorized, commit_path, git, open_dir_nofollow, read_at, restore_at, unstage,
    write_lock,
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


def code_root(session: Session) -> tuple[Path, Area]:
    """Where the code tools read: the worktree while one is active (the agent must see its own edits), else
    the active repo."""
    if session.worktree is not None:
        return session.worktree, Area.WORKTREE
    return session.active_repo, Area.ACTIVE_REPO


def _repo_target(session: Session, rel_path: str, what: str):
    """Authorize READ of ``rel_path`` in the code root, refusing paths that land in a vault."""
    if not isinstance(rel_path, str) or os.path.isabs(rel_path):
        raise ReadError(f"{what} {rel_path!r} must be a path relative to the repo root")
    root, area = code_root(session)
    auth = authorize(root / rel_path, Op.READ, session)
    if auth.area is not area:
        where = f"the {auth.vault} vault" if auth.vault else auth.area.value
        raise ReadError(f"{rel_path!r} is in {where}; vault notes are read with read_note and list_notes")
    return auth


def list_files(session: Session, subdir: str = "") -> FileList:
    """The code root's files (the worktree's while one is active, else the active repo's), as git sees them:
    tracked plus untracked, minus .gitignore'd, minus vault/.

    At most MAX_FILES paths; ``truncated`` says the list was cut short.
    """
    repo, _ = code_root(session)
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
    """Lines ``start_line``..``end_line`` (1-based, inclusive) of a text file in the code root: the
    worktree while one is active, else the active repo.

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
        raise ProposalError(f"{auth.vault}:{auth.rel} already exists, and propose_note only creates new notes; "
                            "pick another path")
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

    errors = (notes.draft_errors(content, auth) + notes.source_errors(content, source, base_content)
              + link_errors(session, auth, content))
    if errors:
        raise DraftInvalid(f"{auth.vault}:{auth.rel}", errors)

    return queue.insert(
        vault=auth.vault, rel_path=auth.rel, kind=kind, content=content, source=source, summary=summary,
        base_sha256=base_sha256, base_content=base_content,
    )


# Root files describe a vault; they aren't notes, so nothing links to them.
ROOT_FILES = ("AGENTS.md", "CONTEXT.md")
_ROOT_STEMS = frozenset(name[:-3].casefold() for name in ROOT_FILES)


def link_errors(session: Session, auth, content: str) -> list[str]:
    """The link rule for typed drafts: at least one link, none to AGENTS.md or CONTEXT.md, none ambiguous.

    Any of related, shared_refs, project_refs, or a body link satisfies the
    rule; a dangling link counts, as it does in Obsidian (the note may not be
    written yet). A link is refused if it resolves to a root file in the vault
    it points into, if its target's name is AGENTS or CONTEXT whether or not
    it resolves, or if it matches more than one note in its scope: the error
    names the matches and their path-qualified forms.
    """
    if notes.draft_type(content, auth) is None:
        return []
    links = notes.draft_links(content)
    errors: list[str] = []
    root_links = 0
    catalogs = _catalogs(session)
    projects = [session.active, *session.pulled]
    for link in links:
        where = "the body" if link.key == "body" else f"'{link.key}'"
        name = link.target.rsplit("/", 1)[-1]
        stem = name[:-3] if name.casefold().endswith(".md") else name
        scope = [v for v in link_scope(link.key, auth.vault, projects, SHARED_NAME) if v in catalogs]
        res = resolve(link.target, (catalogs[v] for v in scope))
        roots = [m for m in res.matches if m.rel_path in ROOT_FILES]
        if roots or stem.casefold() in _ROOT_STEMS:
            root_links += 1
            target = f" (resolves to {roots[0].vault}:{roots[0].rel_path})" if roots else ""
            errors.append(f"[[{link.target}]] in {where}{target}: AGENTS.md and CONTEXT.md are never link "
                          "targets; link a note instead")
        elif res.ambiguous:  # a link that doesn't resolve yet is fine; one that resolves to several isn't
            names = ", ".join(f"{m.vault}:{m.rel_path}" for m in res.matches)
            qualified = " or ".join(f"[[{m.rel_path[:-3]}]]" for m in res.matches)
            errors.append(f"[[{link.target}]] in {where} is ambiguous: it matches {names}. Link by path instead, "
                          f"e.g. {qualified}")
    if not links:
        errors.append("no links: a note must link at least one other note, in related, shared_refs, or "
                      "project_refs, or in the body (AGENTS.md and CONTEXT.md don't count)")
    elif root_links == len(links):
        errors.append("every link here is to a root file, so the note links no other note")
    return errors


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


# Code edits ---------------------------------------------------------------------------
# Only in the worktree (rule 6: WRITE_CODE is allowed nowhere else), written atomically with the same
# realpath re-check as apply. edit_code is the default: a replacement costs far fewer output tokens than a
# rewrite. Every edit lands in the session's EditLog, which commit 10c's summaries use.

MAX_WRITE_BYTES = 64 * 1024
NEW_FILE_MODE = 0o644


class CodeEditError(Exception):
    """A code edit was refused: no match, several matches, binary or non-UTF-8 content, too large, missing."""


@dataclass(frozen=True)
class Edit:
    op: str  # "edit", "write", "create", "delete"
    rel_path: str  # worktree-relative
    before_sha256: str | None
    after_sha256: str | None
    bytes_before: int
    bytes_after: int
    at: str  # UTC, ISO 8601


@dataclass
class EditLog:
    """This conversation's edits, in order. In memory only."""

    edits: list[Edit] = field(default_factory=list)

    def add(self, edit: Edit) -> Edit:
        self.edits.append(edit)
        return edit

    def paths(self) -> list[str]:
        return list(dict.fromkeys(e.rel_path for e in self.edits))


def _sha(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def _code_target(session: Session, rel_path: str):
    """Authorize WRITE_CODE on ``rel_path`` in the active worktree (rules 1, 4, and 6)."""
    if session.worktree is None:
        raise PolicyError(6, str(rel_path), "no worktree is active, and code is written only in a worktree "
                                            "(/wt new <slug>, then /code <slug>)")
    if not isinstance(rel_path, str) or not rel_path.strip():
        raise CodeEditError(f"rel_path {rel_path!r} must be a path relative to the worktree root")
    # An absolute path is left to the policy, so one into your checkout is refused by rule 6, by name.
    auth = authorize(session.worktree / rel_path, Op.WRITE_CODE, session)
    hit = protected_match(auth.rel, session.protected)
    if hit is not None:
        raise PolicyError(6, auth.path, f"{auth.rel} is protected by the goal ({hit}); a goal run can't change it")
    return auth


def protected_match(rel: str, protected: tuple[str, ...]) -> str | None:
    """The protected entry covering worktree-relative ``rel``, or None. Compared case-folded, as macOS does."""
    r = rel.casefold()
    for entry in protected:
        e = entry.casefold()
        if e.endswith("/") and (r + "/").startswith(e) or r == e.rstrip("/"):
            return entry
    return None


def _text(data: bytes, what: str) -> str:
    if b"\0" in data:
        raise CodeEditError(f"{what} contains a NUL byte; binary content isn't written")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise CodeEditError(f"{what} isn't valid UTF-8 (byte {e.start}); only UTF-8 text is written") from None


def _check_new_text(content: str, what: str) -> bytes:
    if not isinstance(content, str):
        raise CodeEditError(f"{what} must be a string")
    if "\0" in content:
        raise CodeEditError(f"{what} contains a NUL byte; binary content isn't written")
    try:
        return content.encode("utf-8")
    except UnicodeEncodeError as e:
        raise CodeEditError(f"{what} can't be encoded as UTF-8 ({e.reason})") from None


def _write(auth, data: bytes, mode: int | None) -> None:
    """Atomically write ``data`` at the authorized path, then confirm it still resolves there."""
    os.makedirs(auth.path.parent, exist_ok=True)  # new folders inside the worktree, for a new file
    dirfd = open_dir_nofollow(auth.path.parent)
    try:
        previous = read_at(dirfd, auth.path.name)
        atomic_write_at(dirfd, auth.path.name, data, mode)
        try:
            check_still_authorized(auth.path)
        except PolicyError:
            restore_at(dirfd, auth.path.name, previous)
            raise
    finally:
        os.close(dirfd)


def _existing(auth) -> tuple[bytes, int] | None:
    if not os.path.lexists(auth.path):
        return None
    if not stat.S_ISREG(os.lstat(auth.path).st_mode):
        raise CodeEditError(f"{auth.rel} isn't a regular file")
    dirfd = open_dir_nofollow(auth.path.parent)
    try:
        return read_at(dirfd, auth.path.name)
    finally:
        os.close(dirfd)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def edit_code(session: Session, rel_path: str, old: str, new: str, log: EditLog | None = None) -> Edit:
    """Replace the one occurrence of ``old`` with ``new`` in a worktree file. ``old`` must occur exactly once."""
    auth = _code_target(session, rel_path)
    if not isinstance(old, str) or not old:
        raise CodeEditError("old must be a non-empty string copied exactly from the file")
    _check_new_text(new, "new")
    found = _existing(auth)
    if found is None:
        raise CodeEditError(f"{auth.rel} doesn't exist; create it with write_code")
    data, mode = found
    text = _text(data, auth.rel)
    count = text.count(old)
    if count != 1:
        how = ("isn't in the file; copy it exactly, whitespace included (read_code shows the current text)"
               if count == 0 else "matches more than once; include more surrounding lines so it matches once")
        raise CodeEditError(f"old matches {count} times in {auth.rel}: it {how}")
    after = text.replace(old, new, 1).encode("utf-8")
    _write(auth, after, mode)
    edit = Edit("edit", auth.rel, _sha(data), _sha(after), len(data), len(after), _now())
    return log.add(edit) if log is not None else edit


def write_code(session: Session, rel_path: str, content: str, log: EditLog | None = None) -> Edit:
    """Create a worktree file, or replace a small one (at most 64 KB, before and after)."""
    auth = _code_target(session, rel_path)
    data = _check_new_text(content, "content")
    if len(data) > MAX_WRITE_BYTES:
        raise CodeEditError(f"content is {len(data):,} bytes; write_code takes at most {MAX_WRITE_BYTES:,}. Write a "
                            "smaller file, or change an existing one with edit_code")
    found = _existing(auth)
    if found is not None and len(found[0]) > MAX_WRITE_BYTES:
        raise CodeEditError(f"{auth.rel} is {len(found[0]):,} bytes, too large to replace whole; change it with "
                            "edit_code")
    _write(auth, data, found[1] if found is not None else NEW_FILE_MODE)
    edit = Edit("write" if found is not None else "create", auth.rel, _sha(found[0]) if found else None, _sha(data),
                len(found[0]) if found else 0, len(data), _now())
    return log.add(edit) if log is not None else edit


def delete_code(session: Session, rel_path: str, log: EditLog | None = None) -> Edit:
    """Delete a file in the worktree."""
    auth = _code_target(session, rel_path)
    found = _existing(auth)
    if found is None:
        raise CodeEditError(f"{auth.rel} doesn't exist")
    dirfd = open_dir_nofollow(auth.path.parent)
    try:
        os.unlink(auth.path.name, dir_fd=dirfd)
    finally:
        os.close(dirfd)
    edit = Edit("delete", auth.rel, _sha(found[0]), None, len(found[0]), 0, _now())
    return log.add(edit) if log is not None else edit
