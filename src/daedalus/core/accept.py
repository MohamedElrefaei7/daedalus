"""How precedent gets made: ask_user's question, the human's /answer, and /promote.

ask_user (agents): proposes a typed question note into questions/ through the
normal queue. The agent's turn ends there.

answer_question (/answer, the human): approves the question if it's still
pending, then in ONE commit creates an accepted decision (source: human,
linked to the question) and marks the question answered, linked to the
decision. Plan approval in commit 12 reuses the same multi-path apply.

promote (/promote, the human): moves an inbox draft into the folder its type
maps to, as accepted, keeping its source, in ONE commit (the inbox file's
deletion and the target's creation).

Both human writes go through apply.apply_changes: path-limited staging,
rollback of every path if the commit fails, crash reconciliation, and the
write lock throughout.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import gate, notes, queue, tools
from .apply import Change, apply, apply_changes, repo_for, vault_root
from .errors import DraftInvalid, ProposalError
from .policy import Op, authorize
from .queue import Proposal
from .registry import SHARED_NAME
from .schema import DESIGN_CATEGORIES, PROJECT_TYPED_FOLDERS, SHARED_TYPED_FOLDERS
from .session import Session

MAX_QUESTION = 300
MAX_SLUG = 60


class AnswerError(ProposalError):
    """/answer was refused; nothing was written."""


class PromoteError(ProposalError):
    """/promote was refused; nothing was written."""


# Frontmatter edits that leave every other line as it was ---------------------------------

def _yaml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", value) and value not in ("true", "false", "null") \
            else json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return "[" + ", ".join(json.dumps(v, ensure_ascii=False) for v in value) + "]"
    raise TypeError(f"can't write {value!r} as a one-line YAML value")


def set_frontmatter(text: str, updates: dict) -> str:
    """``text`` with each key in ``updates`` set, as a one-line value; every other line is kept byte for byte.

    A key's old value may span lines (a block list Obsidian wrote); all of it
    is replaced. A missing key is added at the end of the block. The result
    is re-parsed to check it says exactly what was asked.
    """
    raw, _ = notes.split_frontmatter(text)
    if not raw or not text.startswith("---"):
        raise ProposalError("the note has no frontmatter block to edit")
    offset = text.index(raw)  # the block's text starts right after the opening '---' line
    lines = raw.split("\n")
    for key, value in updates.items():
        line = f"{key}: {_yaml_value(value)}"
        start = next((i for i, ln in enumerate(lines) if re.match(rf"^{re.escape(key)}\s*:", ln)), None)
        if start is None:
            insert_at = len(lines) - 1 if lines and lines[-1] == "" else len(lines)
            lines.insert(insert_at, line)
            continue
        end = start + 1
        while end < len(lines) and lines[end].startswith((" ", "\t", "-")):  # a block value's continuation
            end += 1
        lines[start:end] = [line]
    out = text[:offset] + "\n".join(lines) + text[offset + len(raw):]
    parsed, error = notes.load_frontmatter(notes.split_frontmatter(out)[0] or "")
    if error or any(parsed.get(k) != (list(v) if isinstance(v, tuple) else v) for k, v in updates.items()):
        raise ProposalError(f"couldn't rewrite the frontmatter cleanly ({error or 'values differ'}); edit it by hand")
    return out


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def slugify(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    slug = ""
    for w in words:
        if len(slug) + len(w) + 1 > MAX_SLUG:
            break
        slug = f"{slug}-{w}" if slug else w
    return slug or "question"


def _free_name(folder: Path, stem: str) -> str:
    name, n = f"{stem}.md", 2
    while os.path.lexists(folder / name):
        name, n = f"{stem}-{n}.md", n + 1
    return name


def _link(value: str) -> str:
    value = value.strip()
    return value if value.startswith("[[") and value.endswith("]]") else f"[[{value}]]"


def _today() -> str:
    return dt.date.today().isoformat()


# ask_user ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Asked:
    proposal: Proposal
    question: str
    category: str
    options: tuple[str, ...]


def question_note(question: str, category: str, options: list[str], context_refs: list[str], source: str,
                  why: str) -> str:
    related = [_link(r) for r in context_refs]
    fm = "\n".join([
        "---", "type: question", "status: proposed", f'created: "{_today()}"',
        f"related: {_yaml_value(related)}", "shared_refs: []", "project_refs: []", f"source: {source}",
        f"options: {_yaml_value(list(options))}", "answered: false", "---",
    ])
    numbered = "\n".join(f"{i}. {o}" for i, o in enumerate(options, 1))
    context = "\n".join(f"- {r}" for r in related)
    return (f"{fm}\n\n# {question}\n\n## Question\n\n{question}\n\nCategory: `{category}`\n\n## Options\n\n"
            f"{numbered}\n\n## Context\n\n{context}\n\n{why}\n\n## Answer\n")


def ask_user(session: Session, source: str, question: str, category: str, options: list[str],
             context_refs: list[str]) -> Asked:
    """Propose a question note into the active project's questions/; the caller ends the agent's turn."""
    question = " ".join((question or "").split())
    if not question:
        raise ProposalError("ask_user needs the question")
    if len(question) > MAX_QUESTION:
        raise ProposalError(f"the question is {len(question)} characters; keep it under {MAX_QUESTION}")
    if category not in DESIGN_CATEGORIES:
        raise ProposalError(f"unknown design category {category!r}; the categories are {', '.join(DESIGN_CATEGORIES)}")
    if not isinstance(options, list) or not isinstance(context_refs, list):
        raise ProposalError("options and context_refs must be lists of strings")
    opts = [" ".join(str(o).split()) for o in options]
    if len(opts) < 2 or not all(opts) or len(set(opts)) != len(opts):
        raise ProposalError("ask_user needs at least two distinct, non-empty options")
    refs = [r for r in (context_refs or []) if isinstance(r, str) and r.strip()]
    if not refs:
        raise ProposalError("context_refs must name at least one note (the link rule); not AGENTS or CONTEXT")

    found = gate.find_precedent(session, question, category)
    if found:
        why = ("Why no precedent applied: find_precedent found " + ", ".join(f"[[{p.ref.split(':', 1)[1][:-3]}]]"
               for p in found) + ", and the agent judged that none of them settles this choice.")
    else:
        why = ("Why no precedent applied: find_precedent found no accepted decision in this project's decisions/ "
               f"and no accepted shared practice for this {category} choice.")
    folder = session.active_vault / "questions"
    rel = f"questions/{_free_name(folder, slugify(question))}"
    content = question_note(question, category, opts, refs, source, why)
    summary = f"question: {question}"[:200]
    p = tools.propose_note(session, session.active, rel, content, source, summary)
    return Asked(p, question, category, tuple(opts))


# /answer -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Answered:
    question_ref: str
    decision_ref: str
    chosen: str
    commit_sha: str
    approved_question: str | None  # the approval commit, if /answer approved the question first


def _question_parts(text: str) -> tuple[dict, str, str]:
    raw, body = notes.split_frontmatter(text)
    fm, error = notes.load_frontmatter(raw or "")
    if raw is None or error or fm.get("type") != "question":
        raise AnswerError("that isn't a question note")
    title = gate.title_of(notes.Note(Path(), fm, body, ()), "question")
    m = re.search(r"^Category: `([a-z_]+)`", body, re.M)
    return fm, title, (m.group(1) if m else "")


def parse_answer(answer: str, options: list[str]) -> tuple[str, list[str]]:
    """(chosen, rejected) from an option number or free text."""
    text = (answer or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    if not text:
        raise AnswerError("give an option number or the answer as text")
    if text.isdigit():
        n = int(text)
        if not 1 <= n <= len(options):
            raise AnswerError(f"option {n} doesn't exist; pick 1–{len(options)} or write the answer out")
        return options[n - 1], [o for i, o in enumerate(options) if i != n - 1]
    return text, list(options)


def decision_note(title: str, question_link: str, question: str, category: str, chosen: str,
                  rejected: list[str]) -> str:
    fm = "\n".join(["---", "type: decision", "status: accepted", f'created: "{_today()}"',
                    f"related: {_yaml_value([question_link])}", "shared_refs: []", "project_refs: []",
                    "source: human", "---"])
    cat = f"\n\nCategory: `{category}`" if category else ""
    rejected_md = "\n".join(f"- {o}" for o in rejected) or "- (none)"
    return (f"{fm}\n\n# {title}\n\n## Context\n\n{question}{cat}\n\nAsked in {question_link}.\n\n## Decision\n\n"
            f"{chosen}\n\n## Rejected options\n\n{rejected_md}\n\n## Consequences\n")


def _fill_answer_section(body: str, text: str) -> str:
    m = re.search(r"^## Answer[ \t]*\n", body, re.M)
    if not m:
        return body.rstrip("\n") + f"\n\n## Answer\n\n{text}\n"
    nxt = re.search(r"^#{1,2} ", body[m.end():], re.M)
    end = m.end() + nxt.start() if nxt else len(body)
    rest = body[end:]
    return body[: m.end()] + f"\n{text}\n" + ("\n" + rest if rest else "")


def answer_question(session: Session, question_id: str, answer: str) -> Answered:
    """/answer: approve the question if pending, then one commit: the accepted decision and the answered question."""
    rec = queue.get(question_id)  # UnknownProposal for an unknown id: nothing written
    if rec.vault != session.active or not rec.rel_path.startswith("questions/") or rec.kind != "create":
        raise AnswerError(f"{question_id} isn't a question in {session.active}'s questions/ "
                          f"(it's {rec.kind} {rec.vault}:{rec.rel_path})")
    if rec.status not in ("pending", "applied"):
        raise AnswerError(f"question {question_id} is {rec.status}; only a pending or approved question can be answered")

    # Validate the answer against the question before anything is written, including the approval.
    source_text = rec.content
    root = vault_root(session.registry, rec.vault)
    qpath = authorize(root / rec.rel_path, Op.USER_ACTION, session).path
    if rec.status == "applied":
        if not qpath.is_file():
            raise AnswerError(f"{rec.vault}:{rec.rel_path} no longer exists")
        source_text = qpath.read_text(encoding="utf-8")
    fm, title, category = _question_parts(source_text)
    if fm.get("answered") is True:
        raise AnswerError(f"{rec.vault}:{rec.rel_path} is already answered")
    options = [str(o) for o in (fm.get("options") or [])]
    chosen, rejected = parse_answer(answer, options)

    decisions = authorize(root / "decisions" / "x.md", Op.USER_ACTION, session).path.parent
    stem = rec.rel_path.rsplit("/", 1)[-1][:-3]
    decision_rel = f"decisions/{_free_name(decisions, stem)}"
    dpath = authorize(root / decision_rel, Op.USER_ACTION, session).path
    q_link, d_link = f"[[{rec.rel_path[:-3]}]]", f"[[{decision_rel[:-3]}]]"
    question_text = re.search(r"^## Question\s*\n+(.*?)(?=\n\s*\n|\Z)", notes.split_frontmatter(source_text)[1],
                              re.S | re.M)
    decision = decision_note(title, q_link, question_text.group(1).strip() if question_text else title, category,
                             chosen, rejected)
    errors = notes.draft_errors(decision, authorize(dpath, Op.USER_ACTION, session))
    if errors:
        raise DraftInvalid(f"{rec.vault}:{decision_rel}", errors)

    approved = None
    if rec.status == "pending":
        approved = apply(session, question_id).commit_sha
    current = qpath.read_text(encoding="utf-8")
    fm, _, _ = _question_parts(current)
    related = [str(r) for r in (fm.get("related") or [])]
    updated = set_frontmatter(current, {"answered": True, "related": [*related, d_link]})
    raw, body = notes.split_frontmatter(updated)
    updated = updated[: len(updated) - len(body)] + _fill_answer_section(body, f"{chosen}. Decided in {d_link}.")
    errors = notes.draft_errors(updated, authorize(qpath, Op.USER_ACTION, session))
    if errors:
        raise DraftInvalid(f"{rec.vault}:{rec.rel_path}", errors)

    repo = repo_for(session.registry, rec.vault)
    sha = apply_changes(repo, [Change(dpath, decision, None), Change(qpath, updated, _sha(current))],
                        summary=f"answer: {title}"[:200])
    return Answered(f"{rec.vault}:{rec.rel_path}", f"{rec.vault}:{decision_rel}", chosen, sha, approved)


# /promote -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Promoted:
    source_ref: str
    target_ref: str
    commit_sha: str


def promote(session: Session, ref: str, folder: str | None = None) -> Promoted:
    """/promote: an inbox draft becomes an accepted note in the folder its type maps to, in one commit."""
    match = tools.resolve_ref(session, ref)
    if match.vault not in (session.active, SHARED_NAME):
        raise PromoteError(f"{match.vault} is a pulled project, which is read-only (rule 3); promote there "
                           f"with /vault {match.vault}")
    if not match.rel_path.startswith("inbox/") or match.rel_path.count("/") != 1:
        raise PromoteError(f"{match.vault}:{match.rel_path} isn't an inbox/ draft; only inbox/ drafts are promoted")
    root = vault_root(session.registry, match.vault)
    src = authorize(root / match.rel_path, Op.PROPOSE, session)  # inbox/ is where drafts live; PROPOSE covers it
    text = src.path.read_text(encoding="utf-8")
    raw, _ = notes.split_frontmatter(text)
    fm, error = notes.load_frontmatter(raw) if raw is not None else ({}, None)
    note_type = fm.get("type") if isinstance(fm.get("type"), str) else None
    if error or note_type is None:
        raise PromoteError(f"{match.vault}:{match.rel_path} has no type in its frontmatter, so it has no folder to "
                           "go to; add `type:` (decision, component, concept, ...) first")
    if match.vault == SHARED_NAME:
        mapping = {t: f for f, t in SHARED_TYPED_FOLDERS.items()}
        if note_type not in mapping:
            raise PromoteError(f"shared:{match.rel_path} is type {note_type!r}; the shared inbox promotes only "
                               "practice (to practices/) or lesson (to lessons/)")
    else:
        mapping = {t: f for f, t in PROJECT_TYPED_FOLDERS.items()}
        if note_type not in mapping:
            hint = " Practices and lessons belong in the shared inbox." if note_type in SHARED_TYPED_FOLDERS.values() else ""
            raise PromoteError(f"{match.vault}:{match.rel_path} is type {note_type!r}, which has no project folder.{hint}")
    target_folder = mapping[note_type]
    if folder is not None and folder.strip("/") != target_folder:
        raise PromoteError(f"a {note_type} note goes in {target_folder}/, not {folder.strip('/')}/")

    name = match.rel_path.split("/", 1)[1]
    target_rel = f"{target_folder}/{name}"
    target = authorize(root / target_rel, Op.USER_ACTION, session)
    if os.path.lexists(target.path):
        raise PromoteError(f"{match.vault}:{target_rel} already exists; rename the draft or edit the existing note")
    accepted = set_frontmatter(text, {"status": "accepted"})
    errors = notes.draft_errors(accepted, target) + tools.link_errors(session, target, accepted)
    if errors:
        raise DraftInvalid(f"{match.vault}:{target_rel}", errors)

    repo = repo_for(session.registry, match.vault)
    sha = apply_changes(repo, [Change(src.path, None, _sha(text)), Change(target.path, accepted, None)],
                        summary=f"promote: {match.rel_path} → {target_rel}")
    return Promoted(f"{match.vault}:{match.rel_path}", f"{match.vault}:{target_rel}", sha)
