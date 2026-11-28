"""Goal notes and the honest-done rule, for the coding loop (daedalus.terminal.loop).

A goal note (goals/<slug>.md, type goal) says what to build and how to know it's
done: done_when is a command, run sandboxed; max_iterations, budget_usd, and
checkin_every bound the run; protected lists repo-relative paths the run may
not change.

★ Only the human launches goals: a goal launches only with source: human, or
status: accepted (how an agent's draft gets there, by /promote). done_when is
parsed with shlex and authorized as a sandboxed command (rule 7) before any
worktree exists, so a goal whose check can't run leaves nothing behind.

★ The honest-done rule. A check that exits 0 isn't enough: an agent stuck on a
failing test can delete it, skip it, xfail it, or change how tests are found.
The goal is done only if all of these hold:
  1. the check exits 0;
  2. for pytest, at least as many tests were collected as at the baseline;
  3. for pytest, no more were skipped, and no more xfailed, than at the baseline;
  4. for pytest, every node ID collected at the baseline (`pytest --collect-only
     -q`, sandboxed) still exists, so a failing test can't be swapped for a
     trivial one; and if the baseline had collection errors, every test file
     present at the baseline still exists;
  5. no protected path differs from the worktree's base.
"""

from __future__ import annotations

import os
import posixpath
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import tools
from .notes import Note
from .policy import PolicyError, authorize_command, check_argv
from .registry import PROJECT_NAME
from .session import Session, SessionError, normalize_protected

# Files whose change alters how tests are found or run, or what gets installed: allowed, but each change makes
# the loop check in at once.
CONFIG_NAMES = ("pyproject.toml", "setup.cfg", "tox.ini", "pytest.ini", ".pytest.ini", "conftest.py",
                "daedalus.toml", "uv.lock")
READ_FROM_CHECKOUT = ("daedalus.toml", "uv.lock")  # 10a reads these from your checkout, never the worktree


class GoalError(Exception):
    """The goal can't launch; the message says why and how to fix it."""


@dataclass(frozen=True)
class Goal:
    ref: str  # vault:goals/<slug>.md
    slug: str
    note: Note
    text: str  # the note's whole text, as the agent sees it
    argv: tuple[str, ...]  # done_when, split with shlex
    max_iterations: int
    budget_usd: float
    checkin_every: int
    protected: tuple[str, ...]

    @property
    def is_pytest(self) -> bool:
        return self.argv[0] == "pytest"


# Loading and the launch rule ---------------------------------------------------------------

def _positive_int(fm: dict, key: str) -> int:
    v = fm.get(key)
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        raise GoalError(f"{key} must be a positive whole number, not {v!r}")
    return v


PLAN_PROVENANCE = re.compile(r"plan:(?P<id>[0-9a-z]{14})#(?P<task>t\d+)")
PLAN_CHECKIN_EVERY = 3  # plan tasks carry no checkin_every, so a task checks in every 3 iterations


def load(session: Session, ref: str) -> Goal:
    """Read and check a goal in the active project: a goal note, or a plan task by its provenance
    ``plan:<change_id>#<task id>``. Raises GoalError, or the policy's PolicyError."""
    m = PLAN_PROVENANCE.fullmatch(ref.strip())
    if m:
        return load_plan_task(session, m["id"], m["task"])
    match = tools.resolve_ref(session, ref if "/" in ref or ":" in ref else f"goals/{ref}")
    if match.vault != session.active:
        raise GoalError(f"{match.vault}:{match.rel_path} isn't in the active project ({session.active}); goals run "
                        "only in the project they belong to")
    if not match.rel_path.startswith("goals/"):
        raise GoalError(f"{match.rel_path} isn't in goals/")
    full = f"{match.vault}:{match.rel_path}"
    note, errors = tools.read_note(session, full, strict=False)
    if errors:
        raise GoalError(f"{full} breaks the schema ({'; '.join(errors)}); fix it before running it")
    fm = note.frontmatter
    if fm.get("type") != "goal":
        raise GoalError(f"{full} is a {fm.get('type')!r} note, not a goal")
    if fm.get("source") != "human" and fm.get("status") != "accepted":
        raise GoalError(f"{full} has source {fm.get('source')!r} and status {fm.get('status')!r}. Only you launch "
                        "goals: a goal runs only if you wrote it (source: human) or accepted it (status: accepted, "
                        f"by /promote)")
    slug = match.rel_path.rsplit("/", 1)[-1].removesuffix(".md")
    if not PROJECT_NAME.fullmatch(slug):
        raise GoalError(f"the goal's file name {slug!r} must follow the project name rules, since it names the "
                        "worktree and its branch")
    done_when = fm.get("done_when")
    if not isinstance(done_when, str) or not done_when.strip():
        raise GoalError(f"{full} has no done_when: say which command shows the goal is done, e.g. \"pytest -q\"")
    try:
        argv = tuple(shlex.split(done_when))
    except ValueError as e:
        raise GoalError(f"done_when {done_when!r} can't be parsed ({e})") from None
    budget = fm.get("budget_usd")
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget <= 0:
        raise GoalError(f"budget_usd must be a positive number of dollars, not {budget!r}")
    raw_protected = fm.get("protected")
    if not isinstance(raw_protected, list):
        raise GoalError(f"protected must be a list of repo-relative paths (it may be empty), not {raw_protected!r}")
    try:
        protected = normalize_protected(raw_protected)
    except SessionError as e:
        raise GoalError(str(e)) from None
    missing = [p for p in protected if not os.path.lexists(session.active_repo / p.rstrip("/"))]
    if missing:
        raise GoalError(f"protected {', '.join(repr(m) for m in missing)} doesn't exist in {session.active_repo}. "
                        "Protected paths name files or folders the run must not change, so one that doesn't exist "
                        "protects nothing. In Obsidian's Properties panel, add each path as its own list item, "
                        "without brackets or quotes.")
    text = tools.authorize(session.active_vault / match.rel_path, tools.Op.READ, session).path.read_text("utf-8")
    return Goal(full, slug, note, text, argv, _positive_int(fm, "max_iterations"), float(budget),
                _positive_int(fm, "checkin_every"), protected)


def check_argv_only(goal: Goal, config) -> None:
    """Rule 7's checks that need no venv, so a program outside allow is refused before anything is built."""
    try:
        check_argv(list(goal.argv), config)
    except PolicyError as e:
        raise GoalError(f"done_when {' '.join(goal.argv)!r} can't run sandboxed: rule {e.rule}: {e.reason}") from None


def authorize_check(goal: Goal, config, venv: Path) -> Path:
    """Rule 7 on done_when, as the sandbox will run it. Returns the pinned program."""
    try:
        check_argv(list(goal.argv), config)
        return authorize_command(list(goal.argv), config, venv)
    except PolicyError as e:
        raise GoalError(f"done_when {' '.join(goal.argv)!r} can't run sandboxed: rule {e.rule}: {e.reason}") from None


# pytest's summary line -----------------------------------------------------------------------

@dataclass(frozen=True)
class Counts:
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    xfailed: int = 0
    xpassed: int = 0
    deselected: int = 0

    @property
    def collected(self) -> int:
        """Tests collected and not deselected. A module that fails to import counts as one, via its error."""
        return self.passed + self.failed + self.errors + self.skipped + self.xfailed + self.xpassed


_SUMMARY = re.compile(r"^=*\s*(?P<body>(?:\d+ \w+(?:, )?)+|no tests ran)(?: in [\d.]+s.*?)?\s*=*\s*$")
_PART = re.compile(r"(\d+) (\w+)")
_KEYS = {"passed": "passed", "failed": "failed", "error": "errors", "errors": "errors", "skipped": "skipped",
         "xfailed": "xfailed", "xpassed": "xpassed", "deselected": "deselected"}


def parse_pytest(output: str) -> Counts | None:
    """The counts in pytest's final summary line ("1 failed, 2 passed, 1 skipped in 0.02s"), or None."""
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        m = _SUMMARY.match(line)
        if not m or " in " not in line and "no tests ran" not in line:
            continue
        found: dict[str, int] = {}
        for n, word in _PART.findall(m.group("body")):
            if word in _KEYS:
                found[_KEYS[word]] = found.get(_KEYS[word], 0) + int(n)
        return Counts(**found)
    return None


@dataclass(frozen=True)
class Baseline:
    exit_code: int | None  # None for an empty done_when baseline (nothing was run)
    counts: Counts | None  # pytest only
    node_ids: tuple[str, ...] = ()  # pytest only: what --collect-only listed at the start
    collection_errors: tuple[str, ...] = ()  # files pytest couldn't collect at the start
    test_files: tuple[str, ...] = ()  # test files in the base commit (the fallback when collection had errors)
    # ★ commit 12b: a done_when run that measured nothing is recorded as empty, never as a measurement
    empty: bool = False
    reason: str | None = None
    # ★ commit 12b: the whole suite's test IDs at the start, whatever done_when names
    suite_node_ids: tuple[str, ...] = ()
    suite_collection_errors: tuple[str, ...] = ()
    suite_measured: bool = False  # False for runs that began before commit 12b, or when pytest can't run
    suite_error: str | None = None

    @classmethod
    def from_dict(cls, b: dict) -> "Baseline":
        return cls(b.get("exit_code"), Counts(**b["counts"]) if b.get("counts") else None,
                   tuple(b.get("node_ids", ())), tuple(b.get("collection_errors", ())), tuple(b.get("test_files", ())),
                   bool(b.get("empty")), b.get("reason"), tuple(b.get("suite_node_ids", ())),
                   tuple(b.get("suite_collection_errors", ())), "suite_node_ids" in b and not b.get("suite_error"),
                   b.get("suite_error"))


# What a red-test run means -------------------------------------------------------------------

_COLLECTION_ERROR = re.compile(r"found no collectors|^ERROR \S+\.py\b|\b\d+ errors? in [\d.]+s|error during collection",
                               re.M)
_ABSENT = re.compile(r"file or directory not found|not found:|no match in any of", re.I)


def classify_run(exit_code: int, stdout: str, stderr: str = "") -> str:
    """A pytest run of named tests, as "pass", "red", "absent", or "error".

    0 passes. 1 (failures) and 2 (a collection or import error) are red. 4 is pytest's usage error: a named node
    in a module that fails to import is reported as exit 4 with the collection error ("found no collectors"),
    which is red for the right reason; "not found ... no match" with nothing run is absent, as is 5 (nothing
    collected). Anything else is an error."""
    said = f"{stdout}\n{stderr}"
    if exit_code == 0:
        return "pass"
    if exit_code in (1, 2):
        return "red"
    if exit_code == 4 and _COLLECTION_ERROR.search(said):
        return "red"
    if exit_code == 5 or (exit_code == 4 and _ABSENT.search(said)):
        return "absent"
    return "error"


# The paths done_when names -------------------------------------------------------------------

# pytest options whose next argument is a value, never a path to collect.
VALUE_OPTIONS = {"-k", "-m", "-p", "-c", "-o", "-W", "--rootdir", "--confcutdir", "--basetemp", "--deselect",
                 "--ignore", "--ignore-glob", "--maxfail", "--tb", "--durations", "--junitxml", "--log-level",
                 "--import-mode", "-n", "--timeout", "--override-ini", "--capture", "-r"}


def named_paths(argv: tuple[str, ...] | list[str]) -> list[int]:
    """Indexes of the arguments in a pytest argv that name files or folders to collect."""
    out = []
    for i, a in enumerate(argv[1:], 1):
        if a.startswith("-") or argv[i - 1] in VALUE_OPTIONS:
            continue
        out.append(i)
    return out


def drop_missing_paths(argv: tuple[str, ...] | list[str], wt: Path | str) -> tuple[list[str], list[str]]:
    """(argv without the paths that don't exist in ``wt`` yet, the dropped ones)."""
    keep, dropped = [argv[0]], []
    named = set(named_paths(argv))
    for i, a in enumerate(argv[1:], 1):
        if i in named and not os.path.lexists(Path(wt) / a.split("::", 1)[0]):
            dropped.append(a)
        else:
            keep.append(a)
    return keep, dropped


# pytest --collect-only ----------------------------------------------------------------------

VERBOSITY = {"-q", "-qq", "-qqq", "--quiet", "-v", "-vv", "-vvv", "--verbose"}
TEST_FILE = re.compile(r"(^|/)(test_[^/]*|[^/]*_test)\.py$")


def collect_argv(argv: tuple[str, ...] | list[str]) -> list[str]:
    """done_when as a collection run: the same selection, verbosity replaced by one -q (-qq prints per-file counts)."""
    return [argv[0], *[a for a in argv[1:] if a not in VERBOSITY], "--collect-only", "-q"]


@dataclass(frozen=True)
class Collected:
    node_ids: tuple[str, ...]
    errors: tuple[str, ...]  # files that failed to collect


def parse_collect(output: str) -> Collected:
    """`pytest --collect-only -q`: node IDs (one per line, up to the first blank line) and the ERROR'd files."""
    ids: list[str] = []
    for line in output.splitlines():
        if not line.strip():
            break
        if "::" in line and not line.startswith(("ERROR", "FAILED", "=", "_", " ")):
            ids.append(line.rstrip())
    errors = [m.group(1) for m in re.finditer(r"^ERROR (\S+?)(?: - .*)?$", output, re.M)]
    if not errors and re.search(r"\b\d+ errors?\b.*\bin [\d.]+s", output):
        errors = ["(a collection error)"]
    return Collected(tuple(dict.fromkeys(ids)), tuple(dict.fromkeys(errors)))


def base_test_files(wt: Path | str, base: str) -> tuple[str, ...]:
    return tuple(p for p in _git(wt, "ls-tree", "-r", "--name-only", base).splitlines() if TEST_FILE.search(p))


def _listed(items: list[str], limit: int = 5) -> str:
    shown = ", ".join(items[:limit])
    return shown + (f", and {len(items) - limit} more" if len(items) > limit else "")


# The honest-done rule -------------------------------------------------------------------------

@dataclass(frozen=True)
class Guard:
    name: str  # "exit", "collected", "skipped", "xfailed", "protected"
    held: bool
    detail: str  # plain words


@dataclass(frozen=True)
class Verdict:
    done: bool
    guards: tuple[Guard, ...]

    @property
    def reason(self) -> str:
        failed = [g for g in self.guards if not g.held]
        if not failed:
            return "the check passed, and every guard held"
        if failed[0].name == "exit":
            return failed[0].detail
        return "the check passed, but " + "; and ".join(g.detail for g in failed)


def _n(k: int, one: str, many: str) -> str:
    return f"{k} {one if k == 1 else many}"


def judge(goal: Goal, exit_code: int, output: str, baseline: Baseline, protected_changed: list[str],
          now_ids: tuple[str, ...] | None = None, wt: Path | str | None = None,
          suite_now: tuple[str, ...] | None = None) -> Verdict:
    guards = [Guard("exit", exit_code == 0, "the check exited 0" if exit_code == 0 else
                    f"the check failed (exit {exit_code})")]
    if goal.is_pytest:
        now, then = parse_pytest(output), baseline.counts
        if baseline.empty:  # nothing was measured at the start: compare against zero, and say so
            then = Counts()
            guards.append(Guard("baseline", True, f"the done_when baseline is empty ({baseline.reason}), so its "
                                                  "counts start from zero; the whole suite's tests are guarded"))
        if now is None or then is None:
            which = "now" if now is None else "at the start"
            for name in ("collected", "skipped", "xfailed"):
                guards.append(Guard(name, False, f"pytest's summary line couldn't be read {which}, so {name} can't "
                                                 "be compared"))
        else:
            lost = then.collected - now.collected
            guards.append(Guard("collected", lost <= 0,
                                f"{now.collected} tests were collected, at least the {then.collected} at the start"
                                if lost <= 0 else
                                f"{_n(lost, 'fewer test was', 'fewer tests were')} collected than at the start "
                                f"({then.collected} → {now.collected})"))
            for name, word in (("skipped", "skipped"), ("xfailed", "marked xfail")):
                a, b = getattr(then, name), getattr(now, name)
                guards.append(Guard(name, b <= a, f"{b} {word}, no more than the {a} at the start" if b <= a else
                                    f"{_n(b - a, 'more test was', 'more tests were')} {word} than at the start "
                                    f"({a} → {b})"))
        if now_ids is None:
            guards.append(Guard("node_ids", False, "the tests couldn't be listed now (pytest --collect-only), so "
                                                   "the start's tests can't be checked"))
        else:
            gone = sorted(set(baseline.node_ids) - set(now_ids))
            guards.append(Guard("node_ids", not gone,
                                f"all {len(baseline.node_ids)} tests from the start still exist" if not gone else
                                f"{_n(len(gone), 'test from the start no longer exists', 'tests from the start no longer exist')}"
                                f": {_listed(gone)}"))
        if baseline.collection_errors:
            missing = [f for f in baseline.test_files if wt is None or not (Path(wt) / f).exists()]
            guards.append(Guard("test_files", not missing,
                                f"all {len(baseline.test_files)} test files from the start still exist" if not missing
                                else f"{_n(len(missing), 'test file from the start no longer exists', 'test files from the start no longer exist')}"
                                f": {_listed(missing)} (the start had collection errors, so files are checked)"))
    if exit_code == 0:  # ★ the whole suite's test IDs, whatever done_when names (checked at done)
        if not baseline.suite_measured:
            guards.append(Guard("suite", True, "the whole suite's tests weren't listed at the start ("
                                + (baseline.suite_error or "the run began before they were") + ")"))
        elif suite_now is None:
            guards.append(Guard("suite", False, "the whole suite's tests couldn't be listed now (pytest "
                                                "--collect-only), so the start's tests can't be checked"))
        else:
            gone = sorted(set(baseline.suite_node_ids) - set(suite_now))
            missing_files = [f for f in baseline.suite_collection_errors
                             if wt is not None and not f.startswith("(") and not (Path(wt) / f).exists()]
            ok = not gone and not missing_files
            detail = (f"all {len(baseline.suite_node_ids)} tests in the whole suite at the start still exist" if ok else
                      "; ".join(x for x in (
                          f"{_n(len(gone), 'test in the suite at the start no longer exists', 'tests in the suite at the start no longer exist')}: {_listed(gone)}" if gone else "",
                          f"{_n(len(missing_files), 'file', 'files')} that failed to collect at the start no longer "
                          f"{'exists' if len(missing_files) == 1 else 'exist'}: {_listed(missing_files)}"
                          if missing_files else "") if x))
            guards.append(Guard("suite", ok, detail))
    guards.append(Guard("protected", not protected_changed,
                        "no protected path changed" if not protected_changed else
                        f"protected {'path' if len(protected_changed) == 1 else 'paths'} changed: "
                        f"{', '.join(protected_changed)}"))
    return Verdict(all(g.held for g in guards), tuple(guards))


# The worktree's changes ----------------------------------------------------------------------

def _git(wt: Path | str, *args: str) -> str:
    return subprocess.run(["git", "-C", str(wt), *args], capture_output=True, text=True, check=True,
                          timeout=60).stdout


def protected_changes(wt: Path | str, base: str, protected: tuple[str, ...]) -> list[str]:
    """Protected paths that differ from ``base`` in the worktree: committed, uncommitted, or new."""
    if not protected:
        return []
    specs = ["--", *protected]
    changed = _git(wt, "diff", "--name-only", "--no-renames", base, *specs).splitlines()
    added = _git(wt, "ls-files", "--others", "--exclude-standard", *specs).splitlines()
    return sorted({p for p in changed + added if p})


def diff_stat(wt: Path | str, base: str) -> str:
    """`git diff --stat` of the worktree (committed and not) against its base, plus any untracked new files."""
    stat = _git(wt, "diff", "--stat", base).rstrip()
    new = [p for p in _git(wt, "ls-files", "--others", "--exclude-standard").splitlines() if p]
    lines = [stat] if stat else []
    lines += [f" {p} (new, uncommitted)" for p in new]
    return "\n".join(lines) or "(no changes)"


def tests_modified(wt: Path | str, base: str, test_files: tuple[str, ...] | list[str]) -> tuple[list[str], str]:
    """Test files that existed at the base and now differ (edited or deleted): (paths, their diff)."""
    if not test_files:
        return [], ""
    specs = ["--", *test_files]
    paths = [p for p in _git(wt, "diff", "--name-only", "--no-renames", base, *specs).splitlines() if p]
    return paths, (_git(wt, "diff", "--no-color", "--no-renames", base, *specs) if paths else "")


def is_config(rel: str) -> bool:
    return posixpath.basename(rel) in CONFIG_NAMES


def config_paths(paths: list[str]) -> list[str]:
    return [p for p in dict.fromkeys(paths) if is_config(p)]


# Plan tasks ------------------------------------------------------------------------------------

def load_plan_task(session: Session, change_id: str, task_id: str) -> Goal:
    """A goal from an approved plan's task. ★ The launch rule for plans: the plan's proposal row must be applied
    AND its note in plans/ must be accepted. A pending, rejected, or superseded plan never launches, nor one
    whose note was edited out of acceptance after approval."""
    from . import plans, queue

    ref = f"plan:{change_id}#{task_id}"
    try:
        rec = queue.get(change_id)
    except queue.UnknownProposal:
        raise GoalError(f"{ref}: no plan proposal {change_id}") from None
    parsed = plans._parse_rel(rec.rel_path)
    if rec.vault != session.active or parsed is None:
        raise GoalError(f"{ref}: {change_id} isn't a plan in {session.active}")
    if rec.status != "applied":
        raise GoalError(f"{ref}: the plan {rec.rel_path} is {rec.status}, not approved. Only an approved plan's "
                        "tasks launch: approve it with /approve first")
    full = f"{session.active}:{rec.rel_path}"
    try:
        note, errors = tools.read_note(session, full, strict=False)
    except FileNotFoundError:
        raise GoalError(f"{ref}: the plan was approved but {full} is gone") from None
    fm = note.frontmatter
    if fm.get("type") != "plan" or fm.get("status") != "accepted" or errors:
        raise GoalError(f"{ref}: {full} isn't an accepted plan note (type {fm.get('type')!r}, status "
                        f"{fm.get('status')!r}{'; ' + '; '.join(errors) if errors else ''}); only an approved plan's "
                        "tasks launch")
    found = [t for t in fm.get("tasks") or [] if isinstance(t, dict) and t.get("id") == task_id]
    if not found:
        raise GoalError(f"{ref}: {full} has no task {task_id}")
    t = found[0]
    slug, n = parsed
    try:
        argv = tuple(shlex.split(str(t["done_when"])))
        protected = normalize_protected(list(t.get("protected") or []))
    except (ValueError, KeyError, SessionError) as e:
        raise GoalError(f"{ref}: the task can't run: {e}") from None
    if not argv:
        raise GoalError(f"{ref}: done_when is empty")
    return Goal(ref, f"plan-{slug}-v{n}-{task_id}", note, task_text(slug, n, fm, t, note.body), argv,
                int(t["max_iterations"]), float(t["budget_usd"]), PLAN_CHECKIN_EVERY, protected)


def task_text(slug: str, n: int, fm: dict, t: dict, body: str) -> str:
    """What the coding agent reads as its goal: the task, the plan's binding decisions, and its context."""
    lines = [f"# Task {t['id']} of plan {slug} v{n}: {t['summary']}", "",
             f"Plan: [[plans/{slug}-v{n}]] (approved). Spec: {fm.get('spec')} (read it with read_note).", "",
             f"- Files to start from: {', '.join(t.get('files') or [])}",
             f"- Red test (fails before the change, passes after): {t['red_test']}",
             f"- Done when: `{t['done_when']}`",
             f"- At most {t['max_iterations']} iterations and ${float(t['budget_usd']):.2f}"]
    if t.get("protected"):
        lines.append(f"- Protected (you may not change these): {', '.join(t['protected'])}")
    decisions = [d for d in fm.get("decisions") or [] if isinstance(d, dict)]
    if decisions:
        lines += ["", "## Binding decisions (approved with the plan; follow them, don't revisit them)", ""]
        for d in decisions:
            cite = d.get("note") if str(d.get("basis")).strip() == "new" else d.get("basis")
            lines.append(f"- {d.get('category')}: {d.get('choice')} ({cite})")
    lines += ["", "## The plan's context", "", body.strip()]
    return "\n".join(lines) + "\n"
