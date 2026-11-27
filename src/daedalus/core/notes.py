"""Reading notes: frontmatter parsing and schema validation.

Notes in typed folders are validated against SCHEMA: in project vaults,
decisions/, components/, experiments/, concepts/, goals/, questions/, plans/;
in the shared vault, practices/ and lessons/. Root files (AGENTS.md,
CONTEXT.md), inbox/, logs/, sources/ and everything else are not validated.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, overload

import yaml

from .links import Link, extract_links
from .policy import Authorized, Op, authorize
from .schema import LINK_KEYS, LIST_KEYS, PROJECT_TYPED_FOLDERS, SCHEMA, SHARED_TYPED_FOLDERS, STRING_LIST_KEYS
from .session import Area, Session

FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)^---[ \t]*(?:\r?\n|\Z)", re.DOTALL | re.MULTILINE)
EMPTY_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n---[ \t]*(?:\r?\n|\Z)")


@dataclass(frozen=True)
class Note:
    path: Path
    frontmatter: dict[str, Any]
    body: str
    links: tuple[Link, ...]


class NoteSchemaError(Exception):
    def __init__(self, path: Path | str, errors: list[str]) -> None:
        self.path = Path(path)
        self.errors = list(errors)
        super().__init__(f"{path}: invalid note:\n  " + "\n  ".join(self.errors))


def split_frontmatter(text: str) -> tuple[str | None, str]:
    """(raw frontmatter or None, body)."""
    m = EMPTY_FRONTMATTER.match(text)
    if m:
        return "", text[m.end():]
    m = FRONTMATTER.match(text)
    if not m:
        return None, text
    return m.group(1), text[m.end():]


def load_frontmatter(raw: str) -> tuple[dict[str, Any], str | None]:
    """(mapping, None) for a raw frontmatter block, or ({}, error) if it isn't a YAML mapping."""
    try:
        loaded = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        return {}, "frontmatter is not valid YAML: " + " ".join(str(e).split())
    if loaded is None:
        return {}, None
    if not isinstance(loaded, dict):
        return {}, f"frontmatter must be a YAML mapping, got {type(loaded).__name__}"
    return loaded, None


def expected_type(auth: Authorized) -> tuple[str, str] | None:
    """(folder, type) if ``auth`` is a markdown note in a typed folder, else None."""
    if auth.area in (Area.ACTIVE_VAULT, Area.PULLED_VAULT):
        folders = PROJECT_TYPED_FOLDERS
    elif auth.area is Area.SHARED_VAULT:
        folders = SHARED_TYPED_FOLDERS
    else:
        return None
    if "/" not in auth.rel or not auth.rel.casefold().endswith(".md"):
        return None
    folder = auth.rel.split("/", 1)[0]
    note_type = folders.get(folder)
    return (folder, note_type) if note_type else None


def validate(frontmatter: dict[str, Any], note_type: str, folder: str | None = None) -> list[str]:
    """Every way ``frontmatter`` breaks SCHEMA for ``note_type``; each message names its key."""
    errors = [f"missing base key '{k}'" for k in SCHEMA["base"] if k not in frontmatter]
    errors += [
        f"missing key '{k}' required for type '{note_type}'"
        for k in SCHEMA["extra"].get(note_type, [])
        if k not in frontmatter
    ]
    if "type" in frontmatter and frontmatter["type"] != note_type:
        where = f"notes in {folder}/" if folder else "this note"
        errors.append(f"key 'type' is {frontmatter['type']!r}, but {where} must have type '{note_type}'")
    for key, allowed in SCHEMA["enums"].items():
        if key in frontmatter and frontmatter[key] not in allowed:
            errors.append(f"key '{key}' is {frontmatter[key]!r}, expected one of {allowed}")
    for key in LIST_KEYS:
        if key not in frontmatter:
            continue
        value = frontmatter[key]
        if not isinstance(value, list):
            what = f"a string ({value!r})" if isinstance(value, str) else type(value).__name__
            errors.append(f"key '{key}' must be a YAML list, not {what}. In Obsidian, set the property's type to "
                          "List (right-click the property → Property type → List), then add each entry as its own item")
            continue
        if key not in STRING_LIST_KEYS:
            continue
        bad = [item for item in value if not isinstance(item, str)]
        if bad:
            hint = f"quote wikilinks in YAML, e.g. {key}: [\"[[name]]\"]" if key in LINK_KEYS else "quote each item"
            errors.append(f"key '{key}' has non-string item(s) {bad!r}; {hint}")
        typed_in = [item for item in value if isinstance(item, str) and _looks_like_a_list(item)]
        if typed_in:
            errors.append(f"key '{key}' has {typed_in[0]!r} as one item: a whole list typed into one item. In "
                          "Obsidian, set the property's type to List and add each entry as its own item, without "
                          "brackets or quotes")
    return errors


def _looks_like_a_list(item: str) -> bool:
    """A JSON array written as one string, e.g. '["a.py"]'. A wikilink like '[[x]]' isn't one (x isn't quoted)."""
    s = item.strip()
    if not (s.startswith("[") and s.endswith("]")):
        return False
    try:
        return isinstance(json.loads(s), list)
    except ValueError:
        return False


@overload
def read_note(path: str | Path, session: Session, strict: Literal[True] = ...) -> Note: ...
@overload
def read_note(path: str | Path, session: Session, strict: Literal[False]) -> tuple[Note, list[str]]: ...


def read_note(path: str | Path, session: Session, strict: bool = True) -> Note | tuple[Note, list[str]]:
    """Read a note after authorizing READ.

    strict (the default) raises NoteSchemaError if a typed note is invalid;
    strict=False returns (note, errors) instead.
    """
    auth = authorize(path, Op.READ, session)
    text = auth.path.read_text(encoding="utf-8")
    raw, body = split_frontmatter(text)
    typed = expected_type(auth)
    errors: list[str] = []
    frontmatter: dict[str, Any] = {}

    if raw is None:
        if typed:
            errors.append("no '---'-delimited frontmatter block at the top of the file")
    else:
        frontmatter, error = load_frontmatter(raw)
        if error and typed:
            errors.append(error)
        if typed and not errors:
            folder, note_type = typed
            errors += validate(frontmatter, note_type, folder)

    note = Note(path=auth.path, frontmatter=frontmatter, body=body, links=extract_links(frontmatter, body))
    if strict:
        if errors:
            raise NoteSchemaError(auth.path, errors)
        return note
    return note, errors


# Every note type the schema knows, from the typed folders of both vault kinds.
KNOWN_TYPES = frozenset(PROJECT_TYPED_FOLDERS.values()) | frozenset(SHARED_TYPED_FOLDERS.values())


def draft_errors(content: str, auth: Authorized) -> list[str]:
    """Why a proposed draft would be an invalid note; [] if it's fine.

    A draft headed for a typed folder (questions/, decisions/, ...) is validated
    against that folder's type. Anywhere else (inbox/, CONTEXT.md, ...) it is
    validated only if its frontmatter declares a type. A frontmatter block
    that doesn't parse is refused everywhere: it is never what the author meant.
    """
    raw, _ = split_frontmatter(content)
    typed = expected_type(auth)
    if raw is None:
        return ["no '---'-delimited frontmatter block at the top of the file"] if typed else []
    frontmatter, error = load_frontmatter(raw)
    if error:
        return [error]
    if typed:
        folder, note_type = typed
        return validate(frontmatter, note_type, folder)
    if "type" not in frontmatter:
        return []
    declared = frontmatter["type"]
    if declared not in KNOWN_TYPES:
        return [f"key 'type' is {declared!r}, expected one of {sorted(KNOWN_TYPES)}"]
    return validate(frontmatter, declared)


def draft_type(content: str, auth: Authorized) -> str | None:
    """The type a draft is validated against: its typed folder's, else a known type it declares; else None."""
    typed = expected_type(auth)
    if typed:
        return typed[1]
    raw, _ = split_frontmatter(content)
    if raw is None:
        return None
    frontmatter, error = load_frontmatter(raw)
    declared = frontmatter.get("type") if not error else None
    return declared if isinstance(declared, str) and declared in KNOWN_TYPES else None


def draft_links(content: str) -> tuple[Link, ...]:
    """Every link in a draft: body wikilinks and the frontmatter link keys."""
    raw, body = split_frontmatter(content)
    frontmatter: dict[str, Any] = {}
    if raw is not None:
        frontmatter, _ = load_frontmatter(raw)
    return extract_links(frontmatter, body)


_NO_SOURCE = object()


def _source_of(text: str) -> object:
    raw, _ = split_frontmatter(text)
    if raw is None:
        return _NO_SOURCE
    frontmatter, _error = load_frontmatter(raw)
    return frontmatter.get("source", _NO_SOURCE)


def source_errors(content: str, proposal_source: str, base_content: str | None = None) -> list[str]:
    """Provenance checks on a draft's frontmatter ``source``.

    A create: if the draft declares a source, it must be the proposal's source;
    an agent can't file its work as a human's. An edit: only a human proposal
    may change the source (including adding or removing it), so an agent may
    edit a human's draft but the note stays attributed to the human.
    """
    new = _source_of(content)
    show = lambda v: "no source" if v is _NO_SOURCE else repr(v)  # noqa: E731
    if base_content is None:
        if new is not _NO_SOURCE and new != proposal_source:
            return [f"key 'source' is {show(new)}, but this proposal comes from {proposal_source!r}; "
                    "a new note's source must be the proposal's source"]
        return []
    old = _source_of(base_content)
    if new != old and proposal_source != "human":
        return [f"key 'source' changes from {show(old)} to {show(new)}; only a human proposal may change "
                "a note's source"]
    return []
