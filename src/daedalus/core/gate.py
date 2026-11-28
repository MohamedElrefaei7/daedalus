"""The design-decision gate: no design choice goes ahead without a precedent or the human's answer.

A design choice is one of schema.DESIGN_CATEGORIES. Its basis is either a
wikilink to a precedent, or the literal ``new``.

What counts as precedent, and nothing else:
  * a note in the ACTIVE project's decisions/ with status: accepted, or
  * a note in the SHARED vault's practices/ with status: accepted.

check_basis returns OK(note) for those, NEEDS_USER for ``new`` (the agent
asks with ask_user and stops), and REFUSED(reason) for anything else, with a
reason that says what would make it valid. A pulled project's decisions don't
bind this one; the reason suggests promoting one to a shared practice.

The gate only reads, through tools.read_note, so the path policy and schema
validation apply as usual. propose_with_basis is the entry point the coding
loop and plan approval (commits 10-12) call before proposing a design choice.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple, Union

from . import index, tools
from .errors import ProposalError
from .notes import Note
from .policy import Op, authorize
from .queue import Proposal
from .registry import SHARED_NAME
from .schema import DESIGN_CATEGORIES
from .session import Session

PRECEDENT_FOLDERS = {"active": "decisions/", SHARED_NAME: "practices/"}
MAX_PRECEDENTS = 10
EXCERPT_CHARS = 300


@dataclass(frozen=True)
class OK:
    """``ref`` is accepted precedent for the choice."""

    note: Note
    ref: str
    verdict: str = "OK"


@dataclass(frozen=True)
class NeedsUser:
    """No precedent: ask the human (ask_user) and stop."""

    verdict: str = "NEEDS_USER"


NEEDS_USER = NeedsUser()


@dataclass(frozen=True)
class REFUSED:
    """The basis isn't precedent; ``reason`` says what would make it valid."""

    reason: str
    verdict: str = "REFUSED"


Verdict = Union[OK, NeedsUser, REFUSED]


class GateRefused(ProposalError):
    """A design choice was proposed without precedent."""


def _valid_basis_hint() -> str:
    return ("Cite an accepted note in this project's decisions/ or an accepted shared practice "
            "(shared:practices/), or use basis 'new' if nothing does, so Mohamed decides")


def check_basis(session: Session, category: str, choice: str, basis: str) -> Verdict:
    """Is ``basis`` precedent for ``choice`` (a design choice in ``category``)?"""
    if category not in DESIGN_CATEGORIES:
        return REFUSED(f"unknown design category {category!r}; the categories are {', '.join(DESIGN_CATEGORIES)}")
    if not isinstance(choice, str) or not choice.strip():
        return REFUSED("say what the design choice is")
    basis = (basis or "").strip()
    if basis == "new":
        return NEEDS_USER
    if not (basis.startswith("[[") and basis.endswith("]]")):
        return REFUSED(f"basis {basis!r} must be a wikilink such as [[storage-engine]], or 'new'")
    try:
        match = tools.resolve_ref(session, basis)
    except tools.AmbiguousRefError as e:
        names = ", ".join(f"{m.vault}:{m.rel_path}" for m in e.matches)
        return REFUSED(f"{basis} is ambiguous (it matches {names}); link it by path, e.g. "
                       f"[[decisions/{e.ref.rsplit('/', 1)[-1]}]]")
    except tools.RefError as e:
        return REFUSED(f"{basis} doesn't name a note in this session ({e}). {_valid_basis_hint()}.")
    ref = f"{match.vault}:{match.rel_path}"

    if match.vault in session.pulled:
        return REFUSED(f"{ref} is a decision of the pulled project {match.vault!r}, and another project's "
                       f"decisions don't bind {session.active!r}. If it should apply here too, promote it into a "
                       "shared practice (shared:practices/) and cite that, or use basis 'new', so Mohamed decides.")
    folder = PRECEDENT_FOLDERS["active" if match.vault == session.active else SHARED_NAME]
    if not match.rel_path.startswith(folder):
        where = "this project's decisions/" if folder == "decisions/" else "the shared practices/"
        return REFUSED(f"{ref} isn't in {where}, so it isn't precedent. {_valid_basis_hint()}.")
    try:
        note, errors = tools.read_note(session, ref, strict=False)
    except FileNotFoundError:
        return REFUSED(f"{ref} doesn't exist. {_valid_basis_hint()}.")
    if errors:
        return REFUSED(f"{ref} breaks the schema ({'; '.join(errors)}), so it can't be precedent until it's fixed")
    status = note.frontmatter.get("status")
    if status != "accepted":
        how = ("cite the note that superseded it" if status == "superseded" else
               "it becomes precedent only once Mohamed accepts it (with /answer or /promote)")
        return REFUSED(f"{ref} is {status!r}, not accepted: {how}. Or use basis 'new', so Mohamed decides.")
    return OK(note, ref)


def require(session: Session, category: str, choice: str, basis: str) -> OK:
    """check_basis, raising GateRefused unless the verdict is OK."""
    verdict = check_basis(session, category, choice, basis)
    if isinstance(verdict, OK):
        return verdict
    if isinstance(verdict, NeedsUser):
        raise GateRefused(f"{category} choice {choice!r} has no precedent: ask the human with ask_user, then stop")
    raise GateRefused(f"{category} choice {choice!r}: {verdict.reason}")


def propose_with_basis(session: Session, category: str, choice: str, basis: str, vault: str, rel_path: str,
                       content: str, source: str, summary: str) -> Proposal:
    """Propose a note that makes a design choice, only if its basis is precedent. Nothing is queued otherwise."""
    require(session, category, choice, basis)
    return tools.propose_note(session, vault, rel_path, content, source, summary)


# find_precedent --------------------------------------------------------------------------

class Precedent(NamedTuple):
    ref: str
    title: str
    status: str
    excerpt: str  # the first 300 characters of the body


def title_of(note: Note, rel_path: str) -> str:
    for line in note.body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return rel_path.rsplit("/", 1)[-1].removesuffix(".md")


def find_precedent(session: Session, topic: str, category: str | None = None,
                   limit: int = MAX_PRECEDENTS) -> list[Precedent]:
    """Accepted notes in the active decisions/ and the shared practices/ that match any word of ``topic``.

    Best matches first (bm25). With ``category``, notes that mention it come
    first. Only accepted notes that pass the schema are returned, so every
    result passes check_basis.
    """
    if category is not None and category not in DESIGN_CATEGORIES:
        raise GateRefused(f"unknown design category {category!r}; the categories are {', '.join(DESIGN_CATEGORIES)}")
    if not index.to_fts_query(topic or "", any_word=True):
        raise GateRefused("find_precedent needs a topic: a few words about the choice")
    ranked = []
    for vault, folder in ((session.active, "decisions/"), (SHARED_NAME, "practices/")):
        root = session.vault_root(vault)
        authorize(root, Op.READ, session)
        for hit in index.open_index(root).search(topic, limit=500, any_word=True):
            if not hit.rel_path.startswith(folder):
                continue
            ref = f"{vault}:{hit.rel_path}"
            note, errors = tools.read_note(session, ref, strict=False)
            if errors or note.frontmatter.get("status") != "accepted":
                continue
            body = note.body.strip()
            mentions = category is not None and (category in body or category.replace("_", " ") in body.lower())
            ranked.append((0 if mentions else 1, hit.rank, ref,
                           Precedent(ref, title_of(note, hit.rel_path), "accepted", body[:EXCERPT_CHARS])))
    ranked.sort(key=lambda r: (r[0], r[1], r[2]))
    return [r[3] for r in ranked[:max(1, min(limit, MAX_PRECEDENTS))]]
