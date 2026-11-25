"""Reading notes: frontmatter parsing and schema validation.

Notes in typed folders are validated against SCHEMA: in project vaults,
decisions/, components/, experiments/, concepts/, goals/, questions/, plans/;
in the shared vault, practices/ and lessons/. Root files (AGENTS.md,
CONTEXT.md), inbox/, logs/, sources/ and everything else are not validated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, overload

import yaml

from .links import Link, extract_links
from .policy import Authorized, Op, authorize
from .schema import LINK_KEYS, PROJECT_TYPED_FOLDERS, SCHEMA, SHARED_TYPED_FOLDERS
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
    for key in LINK_KEYS:
        if key not in frontmatter:
            continue
        value = frontmatter[key]
        if not isinstance(value, list):
            errors.append(f"key '{key}' must be a list of quoted wikilinks, got {type(value).__name__}")
            continue
        bad = [item for item in value if not isinstance(item, str)]
        if bad:
            errors.append(
                f"key '{key}' has non-string item(s) {bad!r}; quote wikilinks in YAML, "
                f"e.g. {key}: [\"[[name]]\"]"
            )
    return errors


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
        try:
            loaded = yaml.safe_load(raw)
        except yaml.YAMLError as e:
            loaded = None
            if typed:
                errors.append("frontmatter is not valid YAML: " + " ".join(str(e).split()))
        else:
            if isinstance(loaded, dict):
                frontmatter = loaded
            elif loaded is not None and typed:
                errors.append(f"frontmatter must be a YAML mapping, got {type(loaded).__name__}")
        if typed and not errors:
            folder, note_type = typed
            errors += validate(frontmatter, note_type, folder)

    note = Note(path=auth.path, frontmatter=frontmatter, body=body, links=extract_links(frontmatter, body))
    if strict:
        if errors:
            raise NoteSchemaError(auth.path, errors)
        return note
    return note, errors
