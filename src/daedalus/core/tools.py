"""The four read tools: list_notes, read_note, backlinks, search.

Scope is the session's active vault, its pulled vaults, and the shared vault.
Every file is read through notes.read_note, so every read passes the policy.
"""

from __future__ import annotations

from typing import NamedTuple

from . import index, notes
from .links import Catalog, Match, link_scope, parse_link, resolve
from .policy import Op, authorize
from .registry import SHARED_NAME
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
    """Notes in scope, optionally filtered. templates/ holds templates, not notes, so it is skipped."""
    prefix = folder.strip("/") + "/" if folder else None
    entries = []
    for vault, root in session.scope:
        for rel, _ in sorted(index.walk_markdown(root), key=lambda item: item[0]):
            if rel.startswith("templates/") or (prefix and not rel.startswith(prefix)):
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
