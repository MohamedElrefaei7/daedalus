"""Plans: Claude writes a plan to Mohamed's spec; it waits in the queue until he approves it.

A plan is a typed ``plan`` note, ``plans/<slug>-v<N>.md`` in the active
project's vault. Its frontmatter carries the base keys plus:

  spec       a wikilink that resolves, unambiguously, to a note in the active vault
  decisions  a list of {category, choice, basis}: category one of DESIGN_CATEGORIES;
             basis "new", or a wikilink gate.check_basis returns OK for
  tasks      a non-empty list of {id, summary, files, red_test, done_when,
             max_iterations, budget_usd, protected?}

and its body is free markdown under ``## Context and non-obvious decisions``.

★ validate_plan returns EVERY error at once, each naming its field (and its
task id where there is one), so one round trip tells Claude all it must fix.

★ No unapproved plan touches the vault. submit and revise validate in full,
then write only to the queue (authorized as Op.WRITE_PLAN into plans/). The
server sets type, status (proposed), created, source, and supersedes; Claude
supplies only spec, decisions, tasks, and the link keys.

Versions: submit makes v1. revise makes v<N+1>, carrying
``supersedes: "[[<slug>-v<N>]]"``, and marks v<N> superseded in the queue in
the same transaction if it's still pending. Nothing is deleted: every version
stays readable, and plan_status shows the chain.

Approving a plan (approve_plan, Mohamed's action) re-validates it and writes the
plan note and one decision note per new decision in ONE commit, or nothing.
apply.apply refuses plan proposals, so the generic path can never skip writing
the decisions. Running an approved plan is daedalus.terminal.planrun (/run).
"""

from __future__ import annotations

import datetime as dt
import os
import posixpath
import re
import shlex
from dataclasses import dataclass
from typing import Any

import yaml

from . import gate, notes, planruns, queue, tools
from .apply import repo_for
from .errors import DraftInvalid, ProposalError
from .policy import Authorized, Op, PolicyError, authorize, check_argv
from .registry import PROJECT_NAME
from .schema import DESIGN_CATEGORIES, SCHEMA
from .session import Area, Session, SessionError, normalize_protected

FOLDER = "plans"
BODY_HEADING = "## Context and non-obvious decisions"
TASK_ID = re.compile(r"t\d+")
PLAN_FILE = re.compile(r"plans/(?P<slug>[A-Za-z0-9][A-Za-z0-9_-]*)-v(?P<n>[1-9]\d*)\.md")
VERSIONED = re.compile(r"(?P<slug>.+)-v(?P<n>[1-9]\d*)")
MAX_SLUG = 64
MAX_ITERATIONS = 10

TASK_REQUIRED = ("id", "summary", "files", "red_test", "done_when", "max_iterations", "budget_usd")
TASK_OPTIONAL = ("protected", "red_test_note")
DECISION_KEYS = ("category", "choice", "basis")
DECISION_OPTIONAL = ("consequences",)  # commit 12b: what follows from the choice; written into the note
DECISION_SERVER_KEYS = ("note",)  # approval links each new decision to the note it wrote
# What Claude may put in a plan's frontmatter; the server sets the rest.
CLAUDE_KEYS = ("spec", "decisions", "tasks", "related", "shared_refs", "project_refs")
SERVER_KEYS = ("type", "status", "created", "source", "supersedes")


class PlanError(ProposalError):
    """A plan reference names nothing, or a revision targets the wrong version."""


class PlanInvalid(DraftInvalid):
    """The plan failed validation; ``errors`` holds every error at once. Nothing was queued."""


# Validation ----------------------------------------------------------------------------------

def default_session_cap() -> float:
    """The session cap from ~/.config/daedalus/config.toml, the same one the terminal enforces."""
    from daedalus.terminal.config import load_config  # front-end config; read only when no cap is passed

    return load_config().session_cap_usd


def _plan_auth(session: Session, rel_path: str = "plans/plan.md") -> Authorized:
    """Where a plan is validated as if it lived: the active vault's plans/ (for the schema and link rule)."""
    return Authorized(session.active_vault / rel_path, Area.ACTIVE_VAULT, session.active, rel_path)


def _one_line(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "\n" not in value and "\r" not in value


def _spec_errors(session: Session, spec: Any) -> list[str]:
    if not isinstance(spec, str) or not spec.strip():
        return ["spec: must be a wikilink to the note holding the specification, e.g. \"[[mathx-cli-spec]]\""]
    spec = spec.strip()
    if not (spec.startswith("[[") and spec.endswith("]]")):
        return [f"spec: {spec!r} must be a wikilink, e.g. \"[[mathx-cli-spec]]\""]
    try:
        match = tools.resolve_ref(session, spec)
    except tools.AmbiguousRefError as e:
        names = ", ".join(f"{m.vault}:{m.rel_path}" for m in e.matches)
        forms = " or ".join(f"[[{m.rel_path[:-3]}]]" for m in e.matches)
        return [f"spec: {spec} is ambiguous: it matches {names}. Link it by path, e.g. {forms}"]
    except tools.RefError as e:
        return [f"spec: {spec} doesn't name a note ({e}); write the spec note in {session.active}'s vault first"]
    if match.vault != session.active:
        return [f"spec: {spec} is {match.vault}:{match.rel_path}, but the spec must be a note in the active "
                f"project's vault ({session.active})"]
    if not os.path.isfile(session.vault_root(match.vault) / match.rel_path):
        return [f"spec: {spec} resolves to {match.vault}:{match.rel_path}, which doesn't exist"]
    return []


def _decision_errors(session: Session, decisions: Any) -> list[str]:
    if not isinstance(decisions, list):
        return []  # notes.validate already said it must be a list
    errors: list[str] = []
    for i, d in enumerate(decisions, 1):
        where = f"decisions[{i}]"
        if not isinstance(d, dict):
            errors.append(f"{where}: must be a mapping {{category, choice, basis}}, not {type(d).__name__}")
            continue
        for key in sorted(set(d) - set(DECISION_KEYS) - set(DECISION_OPTIONAL), key=str):
            if key in DECISION_SERVER_KEYS:
                errors.append(f"{where}.{key}: set by the server when Mohamed approves the plan, never by you; "
                              "leave it out")
            else:
                errors.append(f"{where}.{key}: unknown key; a decision has category, choice, and basis, and "
                              "optionally consequences")
        if "consequences" in d and (not isinstance(d["consequences"], str) or not d["consequences"].strip()):
            errors.append(f"{where}.consequences: must be non-empty text saying what follows from the choice, or "
                          "left out")
        category, choice, basis = d.get("category"), d.get("choice"), d.get("basis")
        cat_ok = category in DESIGN_CATEGORIES
        if not cat_ok:
            errors.append(f"{where}.category: {category!r} isn't a design category; use one of "
                          f"{', '.join(DESIGN_CATEGORIES)}")
        choice_ok = _one_line(choice)
        if not choice_ok:
            errors.append(f"{where}.choice: must be one line saying what is decided")
        if not isinstance(basis, str) or not basis.strip():
            errors.append(f"{where}.basis: must be \"new\" or a wikilink to accepted precedent")
            continue
        # The basis is judged on its own, so a bad category doesn't hide a bad basis.
        verdict = gate.check_basis(session, category if cat_ok else DESIGN_CATEGORIES[0],
                                   choice if choice_ok else "(choice)", basis)
        if isinstance(verdict, gate.REFUSED):
            errors.append(f"{where}.basis: {verdict.reason}")
    return errors


def _file_errors(session: Session, where: str, files: Any) -> list[str]:
    if not isinstance(files, list) or not files:
        what = f"a string ({files!r})" if isinstance(files, str) else repr(files)
        return [f"{where}.files: must be a non-empty list of repo-relative paths, not {what}"]
    repo = os.path.realpath(session.active_repo)
    vault = os.path.realpath(session.active_vault)
    vault_rel = os.path.relpath(vault, repo).replace(os.sep, "/").casefold()
    errors: list[str] = []
    for f in files:
        if not isinstance(f, str) or not f.strip():
            errors.append(f"{where}.files: {f!r} must be a non-empty path")
            continue
        if os.path.isabs(f) or f.startswith("~"):
            errors.append(f"{where}.files: {f!r} must be relative to the repo root")
            continue
        norm = posixpath.normpath(f.strip())
        parts = norm.split("/")
        if norm == "." or parts[0] == "..":
            errors.append(f"{where}.files: {f!r} escapes the repo")
            continue
        if any(p.casefold() == ".git" for p in parts):
            errors.append(f"{where}.files: {f!r} is under .git, which no task may touch")
            continue
        if norm.casefold() == vault_rel or norm.casefold().startswith(vault_rel + "/"):
            errors.append(f"{where}.files: {f!r} is under {vault_rel}/: tasks change code, and vault notes go "
                          "through the queue")
            continue
        real = os.path.realpath(os.path.join(repo, norm))  # a symlink can escape where the text doesn't
        if real != repo and not real.startswith(repo + os.sep):
            errors.append(f"{where}.files: {f!r} escapes the repo (it resolves to {real})")
        elif real == vault or real.startswith(vault + os.sep):
            errors.append(f"{where}.files: {f!r} resolves into the vault ({real})")
        elif any(p.casefold() == ".git" for p in real[len(repo):].split(os.sep)):
            errors.append(f"{where}.files: {f!r} resolves under .git")
    return errors


class _Commands:
    """daedalus.toml from the checkout, loaded once per validation."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self._loaded = False
        self.config = None
        self.error: str | None = None

    def get(self):
        if not self._loaded:
            from daedalus.sandbox.config import load_config  # the same reader the runner uses
            from daedalus.sandbox.errors import SandboxConfigError

            self._loaded = True
            try:
                self.config = load_config(self.session.active_repo)  # your checkout, never a worktree
            except SandboxConfigError as e:
                first = str(e).split("\n\n", 1)[0].rstrip(":")
                self.error = first + (' with a [sandbox] table, e.g. allow = ["pytest"]' if first != str(e) else "")
        return self.config


def _done_when_errors(where: str, done_when: Any, commands: _Commands) -> list[str]:
    if not isinstance(done_when, str) or not done_when.strip():
        return [f"{where}.done_when: must be the command that shows the task is done, e.g. \"pytest -q\""]
    try:
        argv = shlex.split(done_when)
    except ValueError as e:
        return [f"{where}.done_when: {done_when!r} can't be parsed ({e})"]
    if not argv:
        return [f"{where}.done_when: {done_when!r} has no command"]
    config = commands.get()
    if config is None:
        return [f"{where}.done_when: {done_when!r} can't be authorized: {commands.error}"]
    try:
        check_argv(argv, config)
    except PolicyError as e:
        return [f"{where}.done_when: {done_when!r} can't run sandboxed: rule {e.rule}: {e.reason}"]
    return []


def _task_errors(session: Session, tasks: Any, cap: float) -> list[str]:
    if not isinstance(tasks, list):
        return []  # notes.validate already said it must be a list
    if not tasks:
        return ["tasks: must list at least one task"]
    errors: list[str] = []
    seen: dict[str, int] = {}
    commands = _Commands(session)
    earlier_files: set[str] = set()  # files listed by tasks with a lower index (A2: they may be protected later)
    for i, t in enumerate(tasks, 1):
        if not isinstance(t, dict):
            errors.append(f"tasks[#{i}]: must be a mapping with {', '.join(TASK_REQUIRED)}, not {type(t).__name__}")
            continue
        tid = t.get("id")
        id_ok = isinstance(tid, str) and TASK_ID.fullmatch(tid) is not None
        where = f"tasks[{tid}]" if id_ok else f"tasks[#{i}]"
        if not id_ok:
            errors.append(f"{where}.id: {tid!r} must be t followed by digits, e.g. \"t1\"")
        elif tid in seen:
            errors.append(f"{where}.id: duplicate task id {tid!r} (task #{seen[tid]} and task #{i}); ids must be "
                          "unique within the plan")
        else:
            seen[tid] = i
        for key in sorted(set(t) - set(TASK_REQUIRED) - set(TASK_OPTIONAL), key=str):
            errors.append(f"{where}.{key}: unknown key; a task has {', '.join(TASK_REQUIRED)}, and optionally "
                          "protected and red_test_note")
        for key in TASK_REQUIRED:
            if key not in t:
                errors.append(f"{where}.{key}: missing")
        if "summary" in t and not _one_line(t["summary"]):
            errors.append(f"{where}.summary: must be one line")
        if "files" in t:
            errors += _file_errors(session, where, t["files"])
        if "red_test" in t:
            errors += _red_test_errors(session, where, t["red_test"], _norm_files(t.get("files")) | earlier_files)
        if "red_test_note" in t and (not isinstance(t["red_test_note"], str) or not t["red_test_note"].strip()):
            errors.append(f"{where}.red_test_note: must be non-empty text, or left out")
        if "done_when" in t:
            errors += _done_when_errors(where, t["done_when"], commands)
        if "max_iterations" in t:
            n = t["max_iterations"]
            if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= MAX_ITERATIONS:
                errors.append(f"{where}.max_iterations: {n!r} must be a whole number from 1 to {MAX_ITERATIONS}")
        if "budget_usd" in t:
            b = t["budget_usd"]
            if isinstance(b, bool) or not isinstance(b, (int, float)) or not b > 0:
                errors.append(f"{where}.budget_usd: {b!r} must be a number of dollars greater than 0")
            elif b > cap:
                errors.append(f"{where}.budget_usd: {b!r} is above the session cap (${cap:.2f}); a task can't "
                              "budget more than one session may spend")
        if "protected" in t:
            errors += _protected_errors(session, where, t["protected"], earlier_files)
        earlier_files |= _norm_files(t.get("files"))
    return errors


RED_TEST_EXAMPLE = '["tests/test_x.py::test_name"]'


def _red_test_errors(session: Session, where: str, raw: Any, creators: set[str]) -> list[str]:
    """★ red_test is a non-empty list of `path.py` or `path.py::node` (commit 12c): nothing parses paths out of
    prose any more, so `[tool.pytest.ini_options]` in a sentence can never become a file."""
    if isinstance(raw, str):
        return [f"{where}.red_test: is text, and it must now be a list of the red tests, each path.py or "
                f"path.py::test_name, e.g. {RED_TEST_EXAMPLE}; put any explanation in red_test_note"]
    if not isinstance(raw, list) or not raw:
        return [f"{where}.red_test: must be a non-empty list of the red tests, each path.py or path.py::test_name, "
                f"e.g. {RED_TEST_EXAMPLE}"]
    repo = os.path.realpath(session.active_repo)
    errors: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip() or "\n" in item:
            errors.append(f"{where}.red_test: {item!r} must be one path.py or path.py::test_name")
            continue
        path, sep, node = item.strip().partition("::")
        norm = posixpath.normpath(path)
        parts = norm.split("/")
        if sep and not node.strip():
            errors.append(f"{where}.red_test: {item!r} has '::' but no test name after it")
        elif os.path.isabs(path) or path.startswith("~"):
            errors.append(f"{where}.red_test: {item!r} must be relative to the repo root")
        elif not path.endswith(".py"):
            errors.append(f"{where}.red_test: {item!r} must name a .py test file (path.py or path.py::test_name)")
        elif norm != path or parts[0] == ".." or ".." in parts:
            errors.append(f"{where}.red_test: {item!r} must be a plain repo-relative path, with no '..' or './'")
        elif any(p.casefold() == ".git" for p in parts):
            errors.append(f"{where}.red_test: {item!r} is under .git")
        else:
            real = os.path.realpath(os.path.join(repo, norm))
            if real != repo and not real.startswith(repo + os.sep):
                errors.append(f"{where}.red_test: {item!r} escapes the repo (it resolves to {real})")
            elif not os.path.lexists(os.path.join(repo, norm)) and norm.casefold() not in creators:
                errors.append(f"{where}.red_test: {path!r} doesn't exist, and neither this task nor an earlier one "
                              "lists it in files")
    return errors


def _norm_files(files: Any) -> set[str]:
    if not isinstance(files, list):
        return set()
    return {posixpath.normpath(f.strip()).casefold() for f in files if isinstance(f, str) and f.strip()}


def _created_earlier(path: str, earlier_files: set[str]) -> bool:
    """A protected path an earlier task creates: the file itself, or (for a folder) a file inside it."""
    norm = posixpath.normpath(path.rstrip("/")).casefold()
    return norm in earlier_files or any(f.startswith(norm + "/") for f in earlier_files)


def _protected_errors(session: Session, where: str, raw: Any, earlier_files: set[str] = frozenset()) -> list[str]:
    if not isinstance(raw, list):
        what = f"a string ({raw!r})" if isinstance(raw, str) else type(raw).__name__
        return [f"{where}.protected: must be a list of repo-relative paths, not {what}"]
    try:
        protected = normalize_protected(raw)
    except SessionError as e:
        return [f"{where}.protected: {e}"]
    return [f"{where}.protected: {p!r} doesn't exist in {session.active_repo} and no earlier task lists it in "
            "files; a protected path must name a file or folder that's already there, or one an earlier task "
            "creates (add each path as its own list item)"
            for p in protected
            if not os.path.lexists(session.active_repo / p.rstrip("/")) and not _created_earlier(p, earlier_files)]


def _body_errors(body: str) -> list[str]:
    lines = body.splitlines()
    at = next((i for i, line in enumerate(lines) if line.strip() == BODY_HEADING), None)
    if at is None:
        return [f"body: needs a '{BODY_HEADING}' section"]
    rest = "\n".join(line for line in lines[at + 1:]).strip()
    if not rest:
        return [f"body: '{BODY_HEADING}' is empty; say what Mohamed needs to know to approve the plan"]
    return []


def validate_plan(session: Session, text: str, *, session_cap_usd: float | None = None,
                  rel_path: str = "plans/plan.md") -> list[str]:
    """Every way ``text`` fails as a plan, all at once; [] if it's valid. Each error names its field."""
    raw, body = notes.split_frontmatter(text)
    if raw is None:
        return ["frontmatter: no '---'-delimited frontmatter block at the top of the plan"]
    fm, error = notes.load_frontmatter(raw)
    if error:
        return [f"frontmatter: {error}"]
    cap = default_session_cap() if session_cap_usd is None else session_cap_usd
    errors = notes.validate(fm, "plan", FOLDER)
    if "spec" in fm:
        errors += _spec_errors(session, fm["spec"])
    errors += _decision_errors(session, fm.get("decisions"))
    errors += _task_errors(session, fm.get("tasks"), cap)
    if "supersedes" in fm:
        s = fm["supersedes"]
        if not (isinstance(s, str) and s.startswith("[[") and s.endswith("]]") and VERSIONED.fullmatch(s[2:-2])):
            errors.append(f"supersedes: {s!r} must be a wikilink to the previous version, e.g. \"[[my-plan-v1]]\"")
    errors += _body_errors(body)
    errors += [f"links: {e}" for e in tools.link_errors(session, _plan_auth(session, rel_path), text)]
    return errors


# Versions ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Version:
    slug: str
    version: int
    rel_path: str
    id: str | None  # the queue proposal; None for a plan note written straight into plans/
    status: str  # the queue status, or the note's own status for a plan in the vault
    reason: str | None  # why it was rejected (or superseded)
    supersedes: str | None
    updated_at: str | None


def _fm_of(text: str) -> dict[str, Any]:
    raw, _ = notes.split_frontmatter(text)
    if raw is None:
        return {}
    fm, _ = notes.load_frontmatter(raw)
    return fm


def _parse_rel(rel_path: str) -> tuple[str, int] | None:
    m = PLAN_FILE.fullmatch(rel_path)
    return (m["slug"], int(m["n"])) if m else None


def is_plan(p: queue.Proposal, session: Session) -> bool:
    return p.vault == session.active and _parse_rel(p.rel_path) is not None


def versions(session: Session, slug: str) -> list[Version]:
    """Every version of ``slug`` in the active project, oldest first: from the queue, and any in plans/."""
    out: dict[int, Version] = {}
    for p in queue.select(vault=session.active):
        parsed = _parse_rel(p.rel_path)
        if parsed is None or parsed[0] != slug:
            continue
        n = parsed[1]
        if n in out:  # versions are numbered past every record, so this is only a hand-made duplicate
            continue
        sup = _fm_of(p.content).get("supersedes")
        out[n] = Version(slug, n, p.rel_path, p.id, p.status, p.status_reason,
                         sup if isinstance(sup, str) else None, p.updated_at)
    folder = session.active_vault / FOLDER
    if folder.is_dir():
        for path in sorted(folder.glob(f"{slug}-v*.md")):
            parsed = _parse_rel(f"{FOLDER}/{path.name}")
            if parsed is None or parsed[0] != slug or parsed[1] in out:
                continue
            fm = _fm_of(path.read_text("utf-8", errors="replace"))
            sup = fm.get("supersedes")
            out[parsed[1]] = Version(slug, parsed[1], f"{FOLDER}/{path.name}", None,
                                     str(fm.get("status", "unknown")), None,
                                     sup if isinstance(sup, str) else None, None)
    return [out[n] for n in sorted(out)]


def resolve(session: Session, plan_ref: str) -> tuple[str, Version]:
    """(slug, version) for a proposal id, a slug (its latest version), ``<slug>-v<N>``, or ``plans/<slug>-v<N>.md``."""
    if not isinstance(plan_ref, str) or not plan_ref.strip():
        raise PlanError("plan_ref must name a plan: its proposal id, its slug, or <slug>-v<N>")
    ref = plan_ref.strip()
    if ref.startswith("[[") and ref.endswith("]]"):
        ref = ref[2:-2]
    if ref.startswith(f"{session.active}:"):
        ref = ref.split(":", 1)[1]
    try:
        p = queue.get(ref)
    except queue.UnknownProposal:
        p = None
    if p is not None:
        parsed = _parse_rel(p.rel_path)
        if parsed is None or p.vault != session.active:
            raise PlanError(f"proposal {ref} is {p.vault}:{p.rel_path}, not a plan in {session.active}")
        slug, n = parsed
    else:
        name = ref.removeprefix(f"{FOLDER}/").removesuffix(".md")
        m = VERSIONED.fullmatch(name)
        slug, n = (m["slug"], int(m["n"])) if m else (name, None)
    chain = versions(session, slug)
    if not chain:
        raise PlanError(f"no plan {slug!r} in {session.active}; list_plans shows the plans there")
    if n is None:
        return slug, chain[-1]
    found = next((v for v in chain if v.version == n), None)
    if found is None:
        raise PlanError(f"plan {slug!r} has no v{n} (it has {', '.join(f'v{v.version}' for v in chain)})")
    return slug, found


# Submitting and revising ---------------------------------------------------------------------

@dataclass(frozen=True)
class Submitted:
    proposal: queue.Proposal
    slug: str
    version: int
    superseded: str | None = None  # the previous version's id, if it was marked superseded


def _slug_errors(slug: Any) -> list[str]:
    if not isinstance(slug, str) or not PROJECT_NAME.fullmatch(slug) or len(slug) > MAX_SLUG:
        return [f"slug: {slug!r} must be letters, digits, '-' or '_', starting with a letter or digit, at most "
                f"{MAX_SLUG} characters"]
    if VERSIONED.fullmatch(slug):
        return [f"slug: {slug!r} mustn't end in -v<N>; the server adds the version"]
    return []


def _frontmatter_errors(frontmatter: Any) -> list[str]:
    if not isinstance(frontmatter, dict):
        return [f"frontmatter: must be a mapping of keys to values, not {type(frontmatter).__name__}"]
    errors = []
    for key in frontmatter:
        if key in SERVER_KEYS:
            errors.append(f"frontmatter.{key}: set by the server, never by you; leave it out")
        elif key not in CLAUDE_KEYS:
            errors.append(f"frontmatter.{key}: unknown key; a plan's frontmatter takes {', '.join(CLAUDE_KEYS)}")
    return errors


def build(session: Session, slug: str, version: int, frontmatter: dict[str, Any], body: str, source: str,
          supersedes: str | None = None, today: dt.date | None = None) -> str:
    """The plan note's text: the server's keys around Claude's, then the body under its heading."""
    given = {k: v for k, v in frontmatter.items() if k in CLAUDE_KEYS}
    related = given.get("related", [])
    spec = given.get("spec")
    if isinstance(related, list) and isinstance(spec, str) and not _spec_errors(session, spec) \
            and spec.strip() not in related:
        related = [spec.strip(), *related]  # the plan links its spec, so it shows in the spec's backlinks
    fm: dict[str, Any] = {
        "type": "plan",
        "status": "proposed",
        "created": (today or dt.date.today()).isoformat(),
        "related": related,
        "shared_refs": given.get("shared_refs", []),
        "project_refs": given.get("project_refs", []),
        "source": source,
    }
    if supersedes is not None:
        fm["supersedes"] = supersedes
    for key in SCHEMA["extra"]["plan"]:
        if key in given:
            fm[key] = given[key]
    text = body.strip() if isinstance(body, str) else ""
    if not any(line.strip() == BODY_HEADING for line in text.splitlines()):
        text = f"{BODY_HEADING}\n\n{text}".rstrip()
    dumped = yaml.safe_dump(fm, sort_keys=False, default_flow_style=None, allow_unicode=True, width=1000)
    return f"---\n{dumped}---\n\n# {slug} v{version}\n\n{text}\n"


def _queue_plan(session: Session, slug: str, version: int, text: str, source: str, summary: str,
                supersede: str | None) -> queue.Proposal:
    rel = f"{FOLDER}/{slug}-v{version}.md"
    auth = authorize(session.active_vault / rel, Op.WRITE_PLAN, session)  # rule 5: plans/ only
    repo_for(session.registry, session.active)
    if not auth.path.parent.is_dir():
        raise ProposalError(f"{session.active}:{FOLDER}/ doesn't exist")
    if os.path.lexists(auth.path):
        raise ProposalError(f"{session.active}:{rel} already exists")
    tools._check_size(text)
    return queue.insert(vault=session.active, rel_path=auth.rel, kind="create", content=text, source=source,
                        summary=tools._check_summary(summary), supersede=supersede)


def submit(session: Session, source: str, slug: str, frontmatter: Any, body: Any,
           session_cap_usd: float | None = None) -> Submitted:
    """Validate a new plan in full and queue it as plans/<slug>-v1.md. Raises PlanInvalid with every error, queuing
    nothing."""
    tools._check_source(source)
    errors = _slug_errors(slug) + _frontmatter_errors(frontmatter)
    if not isinstance(body, str):
        errors.append("body: must be markdown text")
    if not _slug_errors(slug):
        existing = versions(session, slug)
        if existing:
            latest = existing[-1]
            errors.append(f"slug: plan {slug!r} already exists (v{latest.version} is {latest.status}); change it "
                          "with revise_plan, or pick another slug")
    fm = frontmatter if isinstance(frontmatter, dict) else {}
    text = build(session, slug if isinstance(slug, str) else "plan", 1, fm, body if isinstance(body, str) else "",
                 source)
    errors += validate_plan(session, text, session_cap_usd=session_cap_usd)
    if errors:
        raise PlanInvalid(f"{session.active}:{FOLDER}/{slug}-v1.md", errors)
    p = _queue_plan(session, slug, 1, text, source, f"plan: {slug} v1", None)
    return Submitted(proposal=p, slug=slug, version=1)


def revise(session: Session, source: str, plan_ref: str, frontmatter: Any, body: Any,
           session_cap_usd: float | None = None) -> Submitted:
    """Queue plans/<slug>-v<N+1>.md superseding v<N>, the latest; v<N> is marked superseded if still pending."""
    tools._check_source(source)
    slug, named = resolve(session, plan_ref)
    latest = versions(session, slug)[-1]
    if named.version != latest.version:
        raise PlanError(f"{slug} v{named.version} isn't the latest version; revise v{latest.version} "
                        f"({latest.status}) instead")
    if latest.status == "applying":
        raise PlanError(f"{slug} v{latest.version} is being applied right now; try again")
    n = latest.version + 1
    errors = _frontmatter_errors(frontmatter)
    if not isinstance(body, str):
        errors.append("body: must be markdown text")
    fm = frontmatter if isinstance(frontmatter, dict) else {}
    supersedes = f"[[{slug}-v{latest.version}]]"
    text = build(session, slug, n, fm, body if isinstance(body, str) else "", source, supersedes=supersedes)
    errors += validate_plan(session, text, session_cap_usd=session_cap_usd)
    if errors:
        raise PlanInvalid(f"{session.active}:{FOLDER}/{slug}-v{n}.md", errors)
    supersede = latest.id if latest.id is not None and latest.status == "pending" else None
    p = _queue_plan(session, slug, n, text, source, f"plan: {slug} v{n}", supersede)
    return Submitted(proposal=p, slug=slug, version=n, superseded=supersede)


# Reading plans -------------------------------------------------------------------------------

@dataclass(frozen=True)
class DecisionView:
    category: str
    choice: str
    basis: str
    consequences: str | None = None

    @property
    def is_new(self) -> bool:
        return self.basis.strip() == "new"


@dataclass(frozen=True)
class TaskView:
    id: str
    summary: str
    files: tuple[str, ...]
    red_test: tuple[str, ...]
    done_when: str
    max_iterations: Any
    budget_usd: Any
    protected: tuple[str, ...]
    red_test_note: str | None = None


@dataclass(frozen=True)
class PlanView:
    slug: str
    version: int
    spec: str
    supersedes: str | None
    decisions: tuple[DecisionView, ...]
    tasks: tuple[TaskView, ...]
    body: str  # everything after the title heading


def _strs(value: Any) -> tuple[str, ...]:
    return tuple(str(v) for v in value) if isinstance(value, list) else ((str(value),) if value else ())


def view(text: str, rel_path: str) -> PlanView:
    """A lenient reading of a plan's text, for display; validation is validate_plan's job."""
    raw, body = notes.split_frontmatter(text)
    fm = notes.load_frontmatter(raw)[0] if raw is not None else {}
    slug, n = _parse_rel(rel_path) or (rel_path, 0)
    decisions = tuple(DecisionView(str(d.get("category", "")), str(d.get("choice", "")), str(d.get("basis", "")),
                                   d["consequences"].strip() if isinstance(d.get("consequences"), str) else None)
                      for d in fm.get("decisions") or [] if isinstance(d, dict))
    tasks = tuple(TaskView(str(t.get("id", "?")), str(t.get("summary", "")), _strs(t.get("files")),
                           _strs(t.get("red_test")), str(t.get("done_when", "")), t.get("max_iterations"),
                           t.get("budget_usd"), _strs(t.get("protected")),
                           t["red_test_note"] if isinstance(t.get("red_test_note"), str) else None)
                  for t in fm.get("tasks") or [] if isinstance(t, dict))
    lines = body.strip().splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    sup = fm.get("supersedes")
    return PlanView(slug, n, str(fm.get("spec", "")), sup if isinstance(sup, str) else None, decisions, tasks,
                    "\n".join(lines).strip())


def _version_dict(v: Version) -> dict[str, Any]:
    return {"version": v.version, "id": v.id, "rel_path": v.rel_path, "status": v.status, "reason": v.reason,
            "supersedes": v.supersedes, "updated_at": v.updated_at}


def status(session: Session, plan_ref: str) -> dict[str, Any]:
    """The named version's status, its rejection reason if any, and the whole version chain."""
    slug, v = resolve(session, plan_ref)
    chain = versions(session, slug)
    latest = chain[-1]
    return {
        "slug": slug, "version": v.version, "id": v.id, "rel_path": v.rel_path, "status": v.status,
        "rejection_reason": v.reason if v.status == "rejected" else None,
        "latest_version": latest.version, "latest_status": latest.status,
        "chain": [_version_dict(c) for c in chain],
        "run": planruns.summary_for(session.active, v.id) if v.status == "applied" else None,
    }


def summary_of(p: queue.Proposal) -> dict[str, Any]:
    pv = view(p.content, p.rel_path)
    return {
        "id": p.id, "slug": pv.slug, "version": pv.version, "rel_path": p.rel_path, "status": p.status,
        "source": p.source, "spec": pv.spec, "tasks": len(pv.tasks),
        "decisions": [{"category": d.category, "choice": d.choice, "basis": d.basis,
                       "marked": "NEW" if d.is_new else "cited"} for d in pv.decisions],
    }


def list_plans(session: Session) -> dict[str, Any]:
    """Pending and approved plans in the active project, plus rejected ones not yet revised."""
    pending, rejected = [], []
    latest_by_slug: dict[str, int] = {}
    records = [p for p in queue.select(vault=session.active) if _parse_rel(p.rel_path) is not None]
    for p in records:
        slug, n = _parse_rel(p.rel_path)
        latest_by_slug[slug] = max(latest_by_slug.get(slug, 0), n)
    for p in records:
        slug, n = _parse_rel(p.rel_path)
        if p.status == "pending":
            entry = summary_of(p)
            entry["chain"] = [_version_dict(v) for v in versions(session, slug)]
            pending.append(entry)
        elif p.status == "rejected" and n == latest_by_slug[slug]:
            rejected.append({**summary_of(p), "rejection_reason": p.status_reason})
    approved = []
    applied = {p.rel_path: p.id for p in records if p.status == "applied"}
    for e in tools.list_notes(session, folder=FOLDER, type="plan", status="accepted"):
        if e.vault != session.active:
            continue
        parsed = _parse_rel(e.rel_path)
        pid = applied.get(e.rel_path)
        approved.append({"rel_path": e.rel_path, "slug": parsed[0] if parsed else e.rel_path,
                         "version": parsed[1] if parsed else None, "id": pid,
                         "run": planruns.summary_for(session.active, pid)})
    return {"project": session.active, "pending": pending, "approved": approved, "rejected": rejected}


# Approving (commit 12) -----------------------------------------------------------------------

DECISION_STEM_MAX = 48
DECISION_TITLE_MAX = 90


@dataclass(frozen=True)
class NewDecision:
    index: int  # 1-based, as validate_plan numbers decisions
    category: str
    choice: str
    stem: str
    rel_path: str
    text: str


@dataclass(frozen=True)
class Prepared:
    """Everything approval will write, worked out (and checked) before anything is written."""

    proposal: queue.Proposal
    slug: str
    version: int
    view: PlanView
    plan_text: str  # the accepted plan note
    decisions: tuple[NewDecision, ...]


@dataclass(frozen=True)
class Approved:
    proposal: queue.Proposal
    slug: str
    version: int
    plan_ref: str
    decision_refs: tuple[str, ...]
    commit_sha: str


STEM_STOP_WORDS = frozenset("a an and as at by for in into is it of on or the to when with".split())


def decision_stem(category: str, choice: str) -> str:
    """``<category>-<slugified choice>``: cut at a word, at most 48 characters, with trailing stop words dropped
    (so a stem never ends in -as or -it). The collision suffix comes after this."""
    words: list[str] = []
    size = len(category)
    for word in re.findall(r"[a-z0-9]+", choice.lower()):
        if size + 1 + len(word) > DECISION_STEM_MAX:
            break
        words.append(word)
        size += 1 + len(word)
    while words and words[-1] in STEM_STOP_WORDS:
        words.pop()
    return "-".join([category.replace("_", "-"), *words])[:DECISION_STEM_MAX].rstrip("-")


def _stem_taken(session: Session, stem: str) -> bool:
    """True if a bare [[stem]] would already resolve to something the link resolver can see."""
    try:
        tools.resolve_ref(session, stem)
    except tools.AmbiguousRefError:
        return True
    except tools.RefError:
        return os.path.lexists(session.active_vault / "decisions" / f"{stem}.md")
    return True


def _free_stem(session: Session, base: str, used: set[str]) -> str:
    stem, n = base, 2
    while stem in used or _stem_taken(session, stem):
        suffix = f"-{n}"
        stem, n = base[:DECISION_STEM_MAX - len(suffix)].rstrip("-") + suffix, n + 1
    return stem


def _decision_template(session: Session) -> str:
    """The vault's own decision template (the one hand-written decisions start from), else the package's."""
    from .lifecycle import TEMPLATES

    for path in (session.active_vault / "templates" / "decision.md",
                 TEMPLATES / "project-vault" / "templates" / "decision.md"):
        if path.is_file():
            return path.read_text(encoding="utf-8")
    raise ProposalError("no decision template: neither the vault's templates/decision.md nor the package's exists")


def _fill_section(body: str, heading: str, text: str) -> str:
    """Put ``text`` right under ``## heading`` (added at the end if the template has no such section)."""
    m = re.search(rf"^## {re.escape(heading)}[ \t]*\n", body, re.M)
    if not m:
        return body.rstrip("\n") + f"\n\n## {heading}\n\n{text}\n"
    return body[: m.end()] + f"\n{text}\n" + body[m.end():]


def decision_text(session: Session, category: str, choice: str, plan_stem: str, spec: str, source: str,
                  today: str, consequences: str | None = None) -> str:
    """A decision note from the vault's decision template, as if written by hand: accepted, and new in the plan."""
    from .accept import set_frontmatter

    template = _decision_template(session)
    title = choice if len(choice) <= DECISION_TITLE_MAX else choice[: DECISION_TITLE_MAX - 1].rstrip() + "…"
    text = set_frontmatter(template, {"type": "decision", "status": "accepted", "created": today,
                                      "related": [f"[[{plan_stem}]]", spec], "source": source,
                                      "category": category})
    raw, body = notes.split_frontmatter(text)
    head = text[: len(text) - len(body)]
    body = body.replace("{{title}}", title).replace("{{date}}", today)
    body = _fill_section(body, "Context", f"New in [[{plan_stem}]], approved on {today}. Category: `{category}`. "
                                          f"Spec: {spec}.")
    body = _fill_section(body, "Decision", choice)
    body = _fill_section(body, "Consequences", consequences or f"Not stated in [[{plan_stem}]].")  # never blank
    return head + body


def _accepted_plan_text(text: str, notes_by_index: dict[int, str]) -> str:
    """The plan note as approval writes it: status accepted, each new decision linked to its note."""
    raw, body = notes.split_frontmatter(text)
    fm, _ = notes.load_frontmatter(raw)
    fm["status"] = "accepted"
    for i, stem in notes_by_index.items():
        fm["decisions"][i - 1]["note"] = f"[[{stem}]]"
    related = list(fm.get("related") or [])
    fm["related"] = related + [f"[[{stem}]]" for stem in notes_by_index.values() if f"[[{stem}]]" not in related]
    dumped = yaml.safe_dump(fm, sort_keys=False, default_flow_style=None, allow_unicode=True, width=1000)
    return f"---\n{dumped}---\n{body if body.startswith(chr(10)) else chr(10) + body}"


def _refuse_unless_approvable(session: Session, rec: queue.Proposal) -> tuple[str, int]:
    parsed = _parse_rel(rec.rel_path)
    if rec.vault != session.active or parsed is None or rec.kind != "create":
        raise PlanError(f"proposal {rec.id} is {rec.kind} {rec.vault}:{rec.rel_path}, not a plan in "
                        f"{session.active}'s plans/")
    slug, n = parsed
    if rec.status != "pending":
        why = {"superseded": "a later version replaced it; approve the latest",
               "rejected": f"it was rejected ({rec.status_reason}); Claude revises it with revise_plan",
               "applied": "it's already approved"}.get(rec.status, "only a pending plan can be approved")
        raise queue.NotPending(f"plan {slug} v{n} ({rec.id}) is {rec.status}, not pending: {why}")
    latest = versions(session, slug)[-1]
    if latest.version != n:
        raise PlanError(f"plan {slug} v{n} isn't the latest version (v{latest.version} is {latest.status}); only "
                        "the latest version can be approved")
    return slug, n


def prepare_approval(session: Session, change_id: str, *, session_cap_usd: float | None = None,
                     today: dt.date | None = None) -> Prepared:
    """Check the plan can be approved now and work out every note approval writes. Writes nothing."""
    rec = queue.get(change_id)
    slug, n = _refuse_unless_approvable(session, rec)
    # Re-validate against the state now: precedent may have been un-accepted, the spec moved, or the allow list
    # changed since the plan was submitted.
    errors = validate_plan(session, rec.content, session_cap_usd=session_cap_usd, rel_path=rec.rel_path)
    if errors:
        raise PlanInvalid(f"{rec.vault}:{rec.rel_path} no longer validates, so it stays pending", errors)
    pv = view(rec.content, rec.rel_path)
    fm = _fm_of(rec.content)
    plan_stem = f"{slug}-v{n}"
    day = (today or dt.date.today()).isoformat()
    used = {plan_stem}
    new: list[NewDecision] = []
    for i, d in enumerate(pv.decisions, 1):
        if not d.is_new:
            continue
        stem = _free_stem(session, decision_stem(d.category, d.choice), used)
        used.add(stem)
        rel = f"decisions/{stem}.md"
        text = decision_text(session, d.category, d.choice, plan_stem, str(fm["spec"]).strip(), rec.source, day,
                             d.consequences)
        new.append(NewDecision(i, d.category, d.choice, stem, rel, text))
    plan_text = _accepted_plan_text(rec.content, {d.index: d.stem for d in new})
    for rel, text in [(rec.rel_path, plan_text), *((d.rel_path, d.text) for d in new)]:
        auth = Authorized(session.active_vault / rel, Area.ACTIVE_VAULT, session.active, rel)
        errs = notes.draft_errors(text, auth)
        if errs:
            raise DraftInvalid(f"{session.active}:{rel}", errs)
    return Prepared(rec, slug, n, pv, plan_text, tuple(new))


def approve_plan(session: Session, change_id: str, *, session_cap_usd: float | None = None,
                 today: dt.date | None = None) -> Approved:
    """Write the plan note and one decision note per new decision, as one commit. Mohamed's action only.

    Refused (nothing written, the plan stays as it was) unless the row is a pending plan and the latest version
    of its slug, and it still validates. Every target must still be absent at write time; any failure rolls every
    path back. The plan proposal goes applying → applied with the commit sha, or failed with the reason.
    """
    from .apply import Change, apply_changes
    from .vaultio import head_or_none, write_lock

    with write_lock():  # held from the checks to the commit, so nothing changes in between
        prep = prepare_approval(session, change_id, session_cap_usd=session_cap_usd, today=today)
        rec = prep.proposal
        repo = repo_for(session.registry, session.active)
        plan_path = authorize(session.active_vault / rec.rel_path, Op.WRITE_PLAN, session).path
        changes = [Change(plan_path, prep.plan_text, None)]
        for d in prep.decisions:
            changes.append(Change(authorize(session.active_vault / d.rel_path, Op.USER_ACTION, session).path,
                                  d.text, None))
        if not queue.transition(rec.id, "pending", "applying", target_path=str(plan_path), repo_path=str(repo),
                                base_head=head_or_none(repo)):
            raise queue.NotPending(f"plan {rec.id} is no longer pending")
        try:
            sha = apply_changes(repo, changes, f"approve plan {prep.slug}-v{prep.version}",
                                message=[f"agent({rec.source}): approve plan {prep.slug}-v{prep.version}"],
                                change_id=rec.id)
        except queue.StaleProposal as e:  # checked before any write: nothing was written, so it stays pending
            queue.transition(rec.id, "applying", "pending")
            raise e
        except BaseException as e:
            queue.transition(rec.id, "applying", "failed", reason=f"approval failed, nothing was written: {e}")
            raise
        queue.transition(rec.id, "applying", "applied", commit_sha=sha)
    return Approved(queue.get(rec.id), prep.slug, prep.version, f"{session.active}:{rec.rel_path}",
                    tuple(f"{session.active}:{d.rel_path}" for d in prep.decisions), sha)
