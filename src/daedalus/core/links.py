"""Wikilink extraction and resolution, matching Obsidian.

Forms: [[name]], [[name|alias]], [[name#heading]], [[folder/name]], and their
combinations. Resolution is case-insensitive, like Obsidian: by filename stem,
or by vault-relative path when the link includes a folder. A stem that matches
several notes resolves to all of them and is marked ambiguous; nothing ever
silently picks one.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from .schema import LINK_KEYS

WIKILINK = re.compile(r"\[\[([^\[\]\n]+?)\]\]")
# Obsidian ignores links inside code, so strip fenced blocks and inline code first.
FENCED_CODE = re.compile(r"^(`{3,}|~{3,}).*?^\1[ \t]*$", re.DOTALL | re.MULTILINE)
INLINE_CODE = re.compile(r"(`+)(?!`).+?(?<!`)\1")


@dataclass(frozen=True)
class Link:
    target: str  # note name or vault-relative path, without heading or alias
    heading: str | None = None
    alias: str | None = None
    key: str = "body"  # "body", or the frontmatter key the link came from


def parse_link(inner: str, key: str = "body") -> Link | None:
    """Parse the text between [[ and ]]; None for a same-note link like [[#heading]]."""
    target, _, alias = inner.partition("|")
    target, _, heading = target.partition("#")
    # In tables Obsidian escapes the pipe as "\|", leaving a trailing backslash.
    target = target.rstrip("\\").strip()
    if not target:
        return None
    return Link(target=target, heading=heading.strip() or None, alias=alias.strip() or None, key=key)


def scan(text: str, key: str = "body") -> list[Link]:
    links = []
    for m in WIKILINK.finditer(text):
        link = parse_link(m.group(1), key)
        if link is not None:
            links.append(link)
    return links


def strip_code(body: str) -> str:
    return INLINE_CODE.sub("", FENCED_CODE.sub("", body))


def extract_links(frontmatter: Mapping[str, object], body: str) -> tuple[Link, ...]:
    """Links from the link-list frontmatter keys, then from the body."""
    links: list[Link] = []
    for key in LINK_KEYS:
        value = frontmatter.get(key) if isinstance(frontmatter, Mapping) else None
        items = value if isinstance(value, list) else [value]
        for item in items:
            # Non-string items (the unquoted-wikilink trap, [['a']]) carry no
            # link we can trust; notes.validate reports them.
            if isinstance(item, str):
                links.extend(scan(item, key))
    links.extend(scan(strip_code(body), "body"))
    return tuple(links)


def _norm(s: str) -> str:
    return unicodedata.normalize("NFC", s).casefold()


def _strip_md(s: str) -> str:
    return s[:-3] if s.casefold().endswith(".md") else s


@dataclass(frozen=True, order=True)
class Match:
    vault: str
    rel_path: str


@dataclass(frozen=True)
class Resolution:
    target: str
    matches: tuple[Match, ...]

    @property
    def ambiguous(self) -> bool:
        return len(self.matches) > 1

    @property
    def resolved(self) -> bool:
        return len(self.matches) == 1


@dataclass
class Catalog:
    """One vault's markdown files, indexed for link resolution."""

    vault: str
    rels: list[str]
    by_stem: dict[str, list[str]] = field(default_factory=dict)
    by_path: dict[str, str] = field(default_factory=dict)

    @classmethod
    def build(cls, vault: str, rels: Iterable[str]) -> "Catalog":
        cat = cls(vault=vault, rels=sorted(rels))
        for rel in cat.rels:
            no_ext = _strip_md(rel)
            cat.by_path[_norm(no_ext)] = rel
            cat.by_stem.setdefault(_norm(no_ext.rsplit("/", 1)[-1]), []).append(rel)
        return cat


def resolve(target: str, catalogs: Iterable[Catalog]) -> Resolution:
    """Every note in ``catalogs`` that the link target names."""
    catalogs = list(catalogs)
    t = _strip_md(target.strip()).strip("/")
    matches: list[Match] = []
    if "/" in t:
        key = _norm(t)
        matches = [Match(c.vault, c.by_path[key]) for c in catalogs if key in c.by_path]
        if not matches:  # Obsidian also accepts a trailing part of the path
            suffix = "/" + key
            matches = [
                Match(c.vault, rel) for c in catalogs for p, rel in c.by_path.items() if p.endswith(suffix)
            ]
    else:
        key = _norm(t)
        matches = [Match(c.vault, rel) for c in catalogs for rel in c.by_stem.get(key, [])]
    return Resolution(target=target, matches=tuple(sorted(matches)))


def link_scope(key: str, source_vault: str, project_vaults: Iterable[str], shared_vault: str) -> list[str]:
    """The vaults a link from ``source_vault`` under ``key`` may point into.

    shared_refs point into the shared vault, project_refs into the other
    projects in scope, and body and related links stay in their own vault.
    """
    if key == "shared_refs":
        return [shared_vault]
    if key == "project_refs":
        return [v for v in project_vaults if v != source_vault]
    return [source_vault]
