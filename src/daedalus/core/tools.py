"""The tools agents call.

Read tools: list_notes, read_note, backlinks, search. Scope is the session's
active vault, its pulled vaults, and the shared vault; every file is read
through notes.read_note, so every read passes the policy.

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
from .vaultio import check_still_authorized, commit_path, open_dir_nofollow, restore_at, unstage, write_lock
from .links import Catalog, Match, link_scope, parse_link, resolve
from .policy import Op, PolicyError, authorize
from .queue import CommitFailed, DraftInvalid, ProposalError, ProposalTooLarge, Proposal
from .registry import SHARED_NAME
from .schema import SCHEMA
from .session import Session


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
