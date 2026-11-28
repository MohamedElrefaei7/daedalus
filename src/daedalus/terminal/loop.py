"""The coding loop: /goal <note> works toward a goal note in a fresh worktree until it's honestly done.

Start (before anything is created, the goal must launch: core.goals.load, and
done_when must pass rule 7 as the sandbox will run it):
  1. A worktree agent/<goal-slug> (core.worktree.create).
  2. ★ A baseline: done_when, sandboxed, before any edit. For pytest, the
     collected, skipped, and xfailed counts from its summary line.

Each iteration n:
  1. ★ Fresh context: a new conversation, whose one user message holds the
     goal note, every earlier iteration's log, the worktree's git diff --stat,
     the last check's output tail, and any redirect. No tool history or
     reasoning_content carries over, which bounds context and cost however long
     the goal runs (within an iteration, reasoning_content is passed back as usual).
  2. Plan and edit: the coder profile, at most 12 tool rounds. The edit tools
     refuse the goal's protected paths (rule 6). ask_user pauses the run.
  3. Check: the loop runs done_when itself, sandboxed, whatever the agent ran.
  4. ★ The honest-done rule (core.goals.judge): exit 0, collected not below the
     baseline, skipped and xfailed not above it, no protected path changed.
  5. Log: logs/<goal-slug>-<NN>.md, create-only, committed (tools.write_log).
  6. Commit the worktree's changes onto agent/<goal-slug>: "iter <NN>: <summary>".
  7. Stop at the first of: done, max_iterations, the goal's budget (every
     ledger row carries the run_id; the budget, session, and daily caps are all
     checked before each call), or a stop at a check-in.

Check-ins every checkin_every iterations, and ★ immediately after any change to a
config file (core.goals.CONFIG_NAMES), showing its diff: continue, redirect, or
stop. Ctrl-C pauses; /goal resume commits the interrupted iteration's edits as
"iter <NN> (interrupted)" and goes on. Run state lives in
~/.local/state/daedalus/runs/<run_id>.json, written atomically, so a resume works
after a restart. Merging stays your command (/wt merge).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Protocol

from daedalus.core import goals, queue, tools, worktree
from daedalus.core.goals import Baseline, Counts, Goal, GoalError
from daedalus.core.session import Session
from daedalus.sandbox import runner as sandbox_runner
from daedalus.sandbox import venv as venvs
from daedalus.sandbox.config import load_config
from daedalus.sandbox.errors import SandboxError
from daedalus.core.policy import PolicyError

from . import ledger
from .agent import Agent, Events
from .config import Config
from .deepseek import DeepSeek, DeepSeekError
from .ledger import CapExceeded
from .meter import Interrupted, Meter

SOURCE = "deepseek"
LOG_TAIL_LINES = 40
MESSAGE_TAIL_CHARS = 6_000  # of the last check's output, in the next iteration's message
MAX_LESSONS = 3

Runner = Callable[..., sandbox_runner.RunResult]


class LoopUI(Protocol):
    events: Events

    def say(self, text: str, style: str | None = None) -> None: ...
    def checkin(self, info: "CheckIn") -> tuple[str, str | None]: ...  # ("continue" | "redirect" | "stop", text)


@dataclass(frozen=True)
class CheckIn:
    iteration: int
    max_iterations: int
    spent: float  # this batch (since the start, or since the last resume of a stopped run)
    budget: float  # the goal's budget_usd, per batch
    last_summary: str
    diff_stat: str
    config_changed: list[str]
    config_diff: str
    lockfile_note: str | None
    tests_modified: list[str] = ()  # test files that existed at the baseline and now differ
    tests_diff: str = ""
    total_spent: float = 0.0  # across every batch of this run
    batches: int = 1

MAX_TESTS_DIFF_CHARS = 6_000


def spend(state: dict) -> tuple[float, float, int]:
    """(this batch's spend, the run's total across every resume, the number of batches)."""
    total = ledger.spent(run_id=state["run_id"])
    batches = state.get("batches") or [{"spent_before": 0.0}]
    return round(total - batches[-1]["spent_before"], 6), total, len(batches)


def _spend_text(state: dict) -> str:
    batch, total, n = spend(state)
    text = f"spent ${batch:.4f} of ${state['budget_usd']:.2f}"
    return text + (f" this batch; ${total:.4f} in total across {n} batches" if n > 1 else "")


def _capped(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + f"\n[… diff cut at {limit:,} characters]"


# Run state -----------------------------------------------------------------------------------

def runs_dir() -> Path:
    return queue.STATE_ROOT / "runs"


def state_path(run_id: str) -> Path:
    return runs_dir() / f"{run_id}.json"


def save(state: dict) -> None:
    state["updated"] = _now()
    path = state_path(state["run_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def load_state(run_id: str) -> dict:
    try:
        return json.loads(state_path(run_id).read_text())
    except FileNotFoundError:
        raise GoalError(f"no run {run_id!r}") from None


def all_runs(project: str | None = None) -> list[dict]:
    if not runs_dir().is_dir():
        return []
    found = [json.loads(p.read_text()) for p in sorted(runs_dir().glob("*.json"))]
    return sorted((s for s in found if project is None or s["project"] == project), key=lambda s: s["started"])


def resumable(project: str) -> list[dict]:
    """Goal runs /goal resume may continue. A plan task's run is /run's to resume, never /goal's."""
    return [s for s in all_runs(project) if not s.get("plan_run")
            and (s["status"] == "paused" or (s["status"] == "stopped" and not s.get("done")))]


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _git(wt: str | Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(wt), *args], capture_output=True, text=True, check=True,
                          timeout=60).stdout


# The iteration's message and the agent's reply -----------------------------------------------

ITERATION = """\
You are working toward a goal, in iteration {n} of at most {max}. Each iteration starts with a fresh \
conversation: what earlier iterations did is in their logs below, and the worktree holds their committed work.

Plan this iteration's step, citing precedent (find_precedent) for any design choice, or asking with ask_user. \
Then make the change with the edit tools. You may run `{check}` yourself; either way the loop runs it after \
your turn.

The goal counts as done only honestly: the check must exit 0{counts}{protected}. Deleting, skipping, or \
xfailing tests, or changing how tests are found, never counts, and a change to a config file \
(pyproject.toml, conftest.py, ...) is shown to Mohamed at once.{already}

End your reply with exactly these three lines:
PLAN: <what you planned this iteration>
DONE: <one line, at most 60 characters: what you changed>
NEXT: <what the next iteration should try, or "nothing" if the goal is met>"""


def _section(title: str, body: str) -> str:
    return f"## {title}\n\n{body.strip() or '(none)'}"


def _reply_field(text: str, name: str) -> str:
    m = re.search(rf"^\s*\**{name}\**\s*:\s*(.+)$", text or "", re.I | re.M)
    return " ".join(m.group(1).split()).strip("*` ") if m else ""


def _one_line(text: str, limit: int = 72) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _counts_text(c: dict | None) -> str:
    if not c:
        return "no pytest summary line"
    k = Counts(**c)
    return f"collected {k.collected}, skipped {k.skipped}, xfailed {k.xfailed}"


def baseline_text(b: dict) -> str:
    """The stored baseline in words: done_when's measurement (or that it's empty), and the whole suite's tests."""
    if b.get("empty"):
        head = f"done_when baseline: empty ({b.get('reason')})"
    else:
        head = (f"done_when baseline: {_counts_text(b['counts'])}, " if b.get("counts") else "done_when baseline: ") \
            + f"exit {b.get('exit_code')}"
    if b.get("dropped"):
        head += f" (not yet there, so left out: {', '.join(b['dropped'])})"
    if "suite_node_ids" in b:
        head += ("; whole suite at the start: " + (f"{len(b['suite_node_ids'])} tests" if not b.get("suite_error")
                                                   else f"not listed ({b['suite_error']})"))
    return head


def _resume(state: dict) -> str:
    """The command that resumes this run: /goal resume for a goal note, /run <plan> for a plan task."""
    return state.get("resume_hint") or "/goal resume"


def _tail_lines(text: str, n: int) -> str:
    return "\n".join((text or "").rstrip("\n").splitlines()[-n:])


# The loop ------------------------------------------------------------------------------------

class Loop:
    def __init__(self, session: Session, ds: DeepSeek, cfg: Config, session_id: str, ui: LoopUI,
                 meter: Meter | None = None, runner: Runner | None = None) -> None:
        if session.worktree is not None:
            raise GoalError("leave coder mode (/code off) before running a goal")
        self.session, self.ds, self.cfg, self.session_id, self.ui = session, ds, cfg, session_id, ui
        self.meter = meter or Meter()
        self.runner = runner or sandbox_runner.run
        self.proposals: list[str] = []  # made by the run's agents (questions), for /end's review
        # Called with the state when every other guard held; returns one more Guard (or None). /run sets it.
        self.after_check: Callable[[dict], goals.Guard | None] | None = None

    # Start ------------------------------------------------------------------------------

    def prepare(self, ref: str) -> tuple[Goal, Path]:
        """Every refusal that needs nothing created: the goal note, then done_when under rule 7."""
        goal = goals.load(self.session, ref)
        for s in resumable(self.session.active):
            if s["goal_ref"] == goal.ref:
                raise GoalError(f"run {s['run_id']} of this goal is {s['status']}; /goal resume continues it")
        config = load_config(self.session.active_repo)
        goals.check_argv_only(goal, config)  # no venv needed: refuse a program outside allow before any build
        venv = venvs.ensure_venv(self.session.active, self.session.active_repo, sandbox_runner.spawn)
        goals.authorize_check(goal, config, venv)
        return goal, venv

    def new_state(self, goal: Goal, rec: worktree.Record, base: str | None = None) -> dict[str, Any]:
        """A fresh run's state for ``goal`` in the worktree ``rec``, starting at ``base`` (default: its base)."""
        run_id = f"{goal.slug}-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d%H%M%S')}"
        logs = self.session.active_vault / "logs"
        offset = max([int(m.group(1)) for p in logs.glob(f"{goal.slug}-*.md")
                      if (m := re.fullmatch(rf"{re.escape(goal.slug)}-(\d+)\.md", p.name))] or [0])
        return {
            "run_id": run_id, "project": self.session.active, "goal_ref": goal.ref, "goal_slug": goal.slug,
            "wt_slug": rec.slug, "wt_path": rec.path, "branch": rec.branch, "base": base or rec.base,
            "argv": list(goal.argv), "protected": list(goal.protected), "max_iterations": goal.max_iterations,
            "budget_usd": goal.budget_usd, "checkin_every": goal.checkin_every,
            "limit_iterations": goal.max_iterations, "limit_budget": goal.budget_usd, "log_offset": offset,
            "iterations": [], "next": 1, "status": "running", "pause": None, "question": None, "redirect": None,
            "last_output": "", "config_changed": [], "stop_reason": None, "done": False, "started": _now(),
            "batches": [{"started": _now(), "spent_before": 0.0}],
        }

    def measure_baseline(self, state: dict, goal: Goal) -> tuple[dict, str]:
        """Before any edit: (the baseline, the done_when run's output). Two measurements:

        ★ done_when, with any path it names that doesn't exist yet dropped. If nothing is left, or pytest runs
        nothing (exit 4 or 5), the baseline is recorded as empty, never as a measurement of zero tests.
        ★ The whole suite's test IDs (`pytest --collect-only -q`), whatever done_when names, so a test deleted
        or renamed anywhere is caught at done.

        Raises GoalError if a pytest check that ran prints no summary line."""
        wt = Path(state["wt_path"])
        s = self.run_session(state)
        argv, dropped = (goals.drop_missing_paths(goal.argv, wt) if goal.is_pytest else (list(goal.argv), []))
        baseline: dict[str, Any] = {"exit_code": None, "counts": None, "node_ids": [], "collection_errors": [],
                                    "test_files": [], "empty": False, "reason": None, "argv": argv,
                                    "dropped": dropped}
        output = ""
        if goal.is_pytest and dropped and not goals.named_paths(argv):
            baseline.update(empty=True, reason="all named paths are created by the task")
        else:
            result = self.runner(s, argv, wt)
            output = result.stdout_tail
            baseline["exit_code"] = result.exit_code
            if goal.is_pytest and goals.classify_run(result.exit_code, result.stdout_tail,
                                                     result.stderr_tail) == "absent":
                what = "no such file or test" if result.exit_code == 4 else "no tests collected"
                baseline.update(empty=True, reason=f"pytest ran nothing (exit {result.exit_code}: {what})")
            elif goal.is_pytest:
                counts = goals.parse_pytest(result.stdout_tail)
                if counts is None:
                    raise GoalError(f"`{' '.join(argv)}` printed no pytest summary line at the start (exit "
                                    f"{result.exit_code}), so the honest-done guards couldn't be checked:\n"
                                    f"{_tail_lines(result.stdout_tail + result.stderr_tail, 15)}")
                listed = goals.parse_collect(self.runner(s, goals.collect_argv(argv), wt).stdout_tail)
                baseline.update(counts=counts.__dict__, node_ids=list(listed.node_ids),
                                collection_errors=list(listed.errors))
        if goal.is_pytest:
            baseline["test_files"] = list(goals.base_test_files(wt, state["base"]))
        baseline.update(self.collect_suite(s, wt))
        return baseline, output

    def collect_suite(self, s: Session, wt: Path) -> dict[str, Any]:
        """The whole suite's test IDs and the files that failed to collect, or why they couldn't be listed."""
        try:
            listed = self.runner(s, goals.collect_argv(("pytest",)), wt)
        except (PolicyError, SandboxError) as e:
            return {"suite_node_ids": [], "suite_collection_errors": [], "suite_error": str(e)}
        parsed = goals.parse_collect(listed.stdout_tail)
        return {"suite_node_ids": list(parsed.node_ids), "suite_collection_errors": list(parsed.errors),
                "suite_error": None}

    def begin(self, state: dict, baseline: dict, output: str) -> dict:
        """Record the baseline, say it, and run the iterations."""
        state["baseline"] = baseline
        state["last_output"] = output[-MESSAGE_TAIL_CHARS:]
        save(state)
        self.ui.say(f"  baseline: {baseline_text(baseline)}", "dim")
        return self.go(state)

    def start(self, ref: str) -> dict:
        goal, _ = self.prepare(ref)
        made = worktree.create(self.session, goal.slug)
        rec = made.record
        if made.warning:
            self.ui.say(f"⚠ {made.warning}", "yellow")
        state = self.new_state(goal, rec)
        try:
            self.ui.say(f"Run {state['run_id']}: {goal.ref} in {rec.path} on {rec.branch}. Baseline: "
                        f"`{' '.join(goal.argv)}` …", "bold")
            baseline, output = self.measure_baseline(state, goal)
        except BaseException:
            worktree.drop(self.session.registry, self.session.active, rec.slug, force=True)  # nothing in it yet
            raise
        return self.begin(state, baseline, output)

    def run_session(self, state: dict) -> Session:
        return replace(self.session, worktree=Path(state["wt_path"]), protected=tuple(state["protected"]))

    # Resume -----------------------------------------------------------------------------

    def resume(self, run_id: str | None = None, guidance: str | None = None) -> dict:
        candidates = resumable(self.session.active)
        if run_id is not None:
            candidates = [s for s in candidates if s["run_id"] == run_id]
        if not candidates:
            raise GoalError("no paused or stopped run to resume" + (f" with id {run_id}" if run_id else ""))
        return self.continue_run(candidates[-1], guidance)

    def continue_run(self, state: dict, guidance: str | None = None) -> dict:
        """Resume a paused or stopped run from its saved state (its baseline is the one in ``state``)."""
        last = state["iterations"][-1] if state["iterations"] else None
        held_at_checkin = state.get("pause") == "checkin" or state.get("stop_reason") == "stopped at a check-in"
        if held_at_checkin and last is not None and last["done"] and not guidance and self._unchanged_since(state, last):
            # The check-in held back an iteration that was honestly done; resuming is Mohamed's go-ahead.
            state["status"], state["pause"] = "running", None
            self.ui.say(f"Resuming {state['run_id']}: iteration {last['n']} was done and held at a check-in; "
                        "nothing has changed since, so it finishes as done.", "bold")
            return self.finish(state, "done")
        if state["status"] == "stopped":  # another batch: the goal note's iterations and budget again
            state["limit_iterations"] = len(state["iterations"]) + state["max_iterations"]
            total = ledger.spent(run_id=state["run_id"])
            state["limit_budget"] = round(total + state["budget_usd"], 6)
            state.setdefault("batches", [{"started": state["started"], "spent_before": 0.0}]).append(
                {"started": _now(), "spent_before": total})
            state["stop_reason"] = None
        if state["pause"] == "ask_user":
            q = state["question"]
            p = queue.get(q["id"])
            if p.status == "pending":
                raise GoalError(f"the run is waiting for your answer to {q['id']}: /answer {q['id']} <option>")
        if state["pause"] == "interrupted":
            self._commit_interrupted(state)
        if guidance:
            state["redirect"] = guidance
        state["status"], state["pause"] = "running", None
        save(state)
        self.ui.say(f"Resuming {state['run_id']} at iteration {state['next']}.", "bold")
        return self.go(state)

    def _unchanged_since(self, state: dict, last: dict) -> bool:
        wt = state["wt_path"]
        head = _git(wt, "rev-parse", "HEAD").strip()
        expected = last.get("commit") or head
        return head == expected and not worktree.uncommitted(self.run_session(state))

    def _commit_interrupted(self, state: dict) -> None:
        n = state["next"]
        nn = state["log_offset"] + n
        s = self.run_session(state)
        changes = worktree.uncommitted(s)
        sha = worktree.commit(s, f"iter {nn:02d} (interrupted)") if changes else None
        body = (f"# {state['goal_slug']} — iteration {nn:02d} (interrupted)\n\n- run: {state['run_id']}\n"
                f"- branch: {state['branch']}\n\nInterrupted with Ctrl-C before the check ran. "
                + (f"Its edits ({len(changes)} paths) were committed on resume as {sha[:7]}." if sha else
                   "It left no edits.") + "\n")
        tools.write_log(s, f"{state['goal_slug']}-{nn:02d}", body, SOURCE)
        state["iterations"].append({"n": n, "nn": nn, "summary": "interrupted", "next": "", "plan": "",
                                    "edits": [c[3:] for c in changes], "commit": sha, "exit_code": None,
                                    "counts": None, "guards": [], "done": False, "config": [], "interrupted": True})
        state["next"] = n + 1

    # The iterations ---------------------------------------------------------------------

    def go(self, state: dict) -> dict:
        while True:
            if len(state["iterations"]) >= state["limit_iterations"]:
                return self.finish(state, "max_iterations")
            outcome = self.iterate(state)
            if outcome in ("paused", "finished"):
                return state
            last = state["iterations"][-1]
            redirected = False
            # ★ A config change checks in at once, before done is declared, so Mohamed sees every change to how
            # tests are found or run while it happens, even in the iteration that finishes the goal.
            if last["config"]:
                choice = self.checkin(state, last)
                if choice == "stop":
                    return self.finish(state, "stopped at a check-in")
                if choice == "paused":
                    return state
                redirected = choice == "redirect"
            if last["done"] and not redirected:
                return self.finish(state, "done")
            if len(state["iterations"]) >= state["limit_iterations"]:
                return self.finish(state, "max_iterations")
            due = len(state["iterations"]) % state["checkin_every"] == 0
            if due and not last["config"]:
                choice = self.checkin(state, last)
                if choice == "stop":
                    return self.finish(state, "stopped at a check-in")
                if choice == "paused":
                    return state

    def message(self, state: dict, n: int) -> str:
        base = state["baseline"]
        counts = ""
        if base.get("empty"):
            counts = (f", with no test skipped or xfailed (its baseline is empty: {base.get('reason')}), and every "
                      "test in the whole suite at the start must still exist")
        elif base["counts"]:
            c = Counts(**base["counts"])
            counts = (f" with at least {c.collected} tests collected and no more than {c.skipped} skipped and "
                      f"{c.xfailed} xfailed (the counts at the start)")
        protected = (f", and these protected paths must not change: {', '.join(state['protected'])}"
                     if state["protected"] else "")
        already = (" Note: the check already passed at the start, so passing alone shows nothing; finish the whole "
                   "goal, with tests that cover it." if base["exit_code"] == 0 else "")
        head = ITERATION.format(n=n, max=state["limit_iterations"], check=" ".join(state["argv"]), counts=counts,
                                protected=protected, already=already)
        goal_text = state.get("goal_text") or tools.authorize(
            self.session.active_vault / state["goal_ref"].split(":", 1)[1], tools.Op.READ,
            self.session).path.read_text("utf-8")
        logs = [self._read_log(state, it["nn"]) for it in state["iterations"]]
        parts = [head, _section(f"The goal ({state['goal_ref']})", goal_text),
                 _section("Earlier iterations' logs", "\n\n".join(logs)),
                 _section("The worktree's changes so far (git diff --stat against the start)",
                          goals.diff_stat(state["wt_path"], state["base"])),
                 _section("The last check's output (its end)", f"```\n{state['last_output'].strip()}\n```")]
        q = state.get("question")
        if q and not q.get("delivered"):  # resume is refused until the question is no longer pending
            path = self.session.active_vault / q["rel_path"]
            answer = path.read_text("utf-8") if path.is_file() else "(the question was rejected, not answered)"
            parts.append(_section(f"Mohamed's answer to your question {q['id']}", answer))
            q["delivered"] = True
        if state.get("redirect"):
            parts.append(_section("Mohamed's redirect", state["redirect"]))
        return "\n\n".join(parts)

    def _read_log(self, state: dict, nn: int) -> str:
        path = self.session.active_vault / "logs" / f"{state['goal_slug']}-{nn:02d}.md"
        return path.read_text("utf-8") if path.is_file() else f"(log {nn:02d} is missing)"

    def iterate(self, state: dict) -> str:
        """One iteration. Returns "ok", "paused", or "finished" (a cap stopped the run)."""
        n = state["next"]
        nn = state["log_offset"] + n
        s = self.run_session(state)
        self.ui.say(f"\n── iteration {n} of {state['limit_iterations']} ── {state['run_id']}", "bold cyan")
        agent = Agent(s, self.ds, self.cfg, self.session_id, meter=self.meter, profile="coder",
                      run_id=state["run_id"], run_cap=state["limit_budget"])
        text = self.message(state, n)
        state["redirect"] = None
        save(state)
        capped: CapExceeded | None = None
        try:
            turn = agent.turn(text, self.ui.events)
        except KeyboardInterrupt:
            self.ui.events.stopped("interrupted")
            self.proposals += agent.proposals
            state["status"], state["pause"] = "paused", "interrupted"
            save(state)
            self.ui.say(f"Paused {state['run_id']} at iteration {n}; its edits stay uncommitted. {_resume(state)} "
                        f"commits them as iter {nn:02d} (interrupted) and goes on.", "yellow")
            return "paused"
        except CapExceeded as e:
            capped, turn = e, None
        except (DeepSeekError, Interrupted) as e:
            self.proposals += agent.proposals
            state["status"], state["pause"] = "paused", "interrupted"
            save(state)
            self.ui.say(f"Paused {state['run_id']}: {e}. {_resume(state)} retries iteration {n}.", "yellow")
            return "paused"
        self.proposals += agent.proposals
        if capped is not None and not agent.tool_calls:
            self.ui.say(str(capped), "bold red")  # stopped before anything happened in this iteration
            return self._finish_capped(state, capped)

        reply = turn.answer if turn is not None else ""
        edits = [f"{e.op} {e.rel_path}" for e in agent.edit_log.edits]
        config = goals.config_paths([e.rel_path for e in agent.edit_log.edits])
        record: dict[str, Any] = {
            "n": n, "nn": nn, "plan": _reply_field(reply, "PLAN"), "summary": _reply_field(reply, "DONE"),
            "next": _reply_field(reply, "NEXT"), "edits": edits, "config": config, "commit": None,
            "exit_code": None, "counts": None, "guards": [], "done": False, "asked": None,
            "rounds_stopped": bool(turn and turn.stopped),
        }
        if turn is not None and turn.asked:
            a = turn.asked
            record["asked"] = a["id"]
            state["question"] = {"id": a["id"], "vault": a["vault"], "rel_path": a["rel_path"], "delivered": False}
        else:
            self._check(state, record)
        record["summary"] = record["summary"] or _one_line(", ".join(edits) or "no changes", 60)
        self._log(state, record, reply, capped)
        changes = worktree.uncommitted(s)
        if changes:
            record["commit"] = worktree.commit(s, f"iter {nn:02d}: {_one_line(record['summary'], 60)}")
        state["iterations"].append(record)
        state["config_changed"] = sorted(set(state["config_changed"]) | set(config))
        state["next"] = n + 1
        save(state)
        self._say_iteration(record)
        if record["asked"]:
            state["status"], state["pause"] = "paused", "ask_user"
            save(state)
            self.ui.say(f"Paused {state['run_id']}: the agent asked {record['asked']}. Answer with /answer, then "
                        f"{_resume(state)}.", "magenta")
            return "paused"
        if capped is not None:
            self.ui.say(str(capped), "bold red")
            return self._finish_capped(state, capped)
        return "ok"

    def _check(self, state: dict, record: dict) -> None:
        s = self.run_session(state)
        result = self.runner(s, list(state["argv"]), Path(state["wt_path"]))
        out = result.stdout_tail + (("\n" + result.stderr_tail) if result.stderr_tail.strip() else "")
        state["last_output"] = out[-MESSAGE_TAIL_CHARS:]
        goal_like = Goal(state["goal_ref"], state["goal_slug"], None, "", tuple(state["argv"]), 0, 0.0, 1,
                         tuple(state["protected"]))
        baseline = Baseline.from_dict(state["baseline"])
        changed = goals.protected_changes(state["wt_path"], state["base"], tuple(state["protected"]))
        now_ids = None
        if goal_like.is_pytest:
            listed = self.runner(s, goals.collect_argv(state["argv"]), Path(state["wt_path"]))
            now_ids = goals.parse_collect(listed.stdout_tail).node_ids
        suite_now = None
        if result.exit_code == 0 and baseline.suite_measured:  # at done: the whole suite, listed again
            suite = self.collect_suite(s, Path(state["wt_path"]))
            suite_now = None if suite["suite_error"] else tuple(suite["suite_node_ids"])
        verdict = goals.judge(goal_like, result.exit_code, result.stdout_tail, baseline, changed, now_ids,
                              state["wt_path"], suite_now)
        if verdict.done and self.after_check is not None:  # e.g. a plan task's counterfactual red test
            extra = self.after_check(state)
            if extra is not None:
                verdict = goals.Verdict(verdict.done and extra.held, (*verdict.guards, extra))
        counts = goals.parse_pytest(result.stdout_tail) if goal_like.is_pytest else None
        missing = sorted(set(baseline.node_ids) - set(now_ids)) if now_ids is not None else None
        if suite_now is not None:
            missing = sorted(set(missing or ()) | (set(baseline.suite_node_ids) - set(suite_now)))
        record.update(exit_code=result.exit_code, counts=counts.__dict__ if counts else None,
                      guards=[g.__dict__ for g in verdict.guards], done=verdict.done, verdict=verdict.reason,
                      timed_out=result.timed_out, output_tail=_tail_lines(out, LOG_TAIL_LINES),
                      missing_node_ids=missing, protected_changed=changed)

    def _log(self, state: dict, r: dict, reply: str, capped: CapExceeded | None) -> None:
        lines = [f"# {state['goal_slug']} — iteration {r['nn']:02d}", "",
                 f"- run: {state['run_id']}",
                 f"- goal: {state.get('goal_link') or '[[' + state['goal_ref'].split(':', 1)[1].removesuffix('.md') + ']]'}",
                 f"- branch: {state['branch']}", "", "## Plan", "", r["plan"] or _one_line(reply, 400) or "(none)", "",
                 "## Files edited", ""]
        lines += [f"- {e}" for e in r["edits"]] or ["(none)"]
        if r["config"]:
            lines += ["", f"Config files changed (check-in shown): {', '.join(r['config'])}"]
        if r["asked"]:
            lines += ["", "## Check", "", f"Not run: the agent asked {r['asked']} and the run paused for the answer."]
        else:
            b = state["baseline"]
            lines += ["", "## Check", "",
                      f"`{' '.join(state['argv'])}` → exit {r['exit_code']}" + (" (timed out)" if r.get("timed_out") else "")
                      + (f" · {_counts_text(r['counts'])} (at the start: "
                         + ("the done_when baseline is empty" if b.get("empty") else _counts_text(b["counts"])) + ")"
                         if b["counts"] is not None or r["counts"] else ""),
                      "", "```", r.get("output_tail", "").rstrip(), "```", "", "## Guards", ""]
            lines += [f"- {'held' if g['held'] else 'FAILED'}: {g['name']}: {g['detail']}" for g in r["guards"]]
            lines += ["", "## Verdict", "", ("done: " if r["done"] else "not done: ") + r.get("verdict", "")]
        if r.get("rounds_stopped"):
            lines += ["", "The agent hit its 12 tool rounds this iteration."]
        if capped is not None:
            lines += ["", f"Stopped by a cap: {capped}"]
        lines += ["", "## Next", "", r["next"] or "(not said)", ""]
        tools.write_log(self.run_session(state), f"{state['goal_slug']}-{r['nn']:02d}", "\n".join(lines), SOURCE)

    def _say_iteration(self, r: dict) -> None:
        if r["asked"]:
            return
        held = " ".join(("✓" if g["held"] else "✗") + g["name"] for g in r["guards"])
        style = "green" if r["done"] else "yellow"
        self.ui.say(f"  check: exit {r['exit_code']} · {_counts_text(r['counts'])} · {held}", style)
        self.ui.say(f"  {'done' if r['done'] else 'not done'}: {r.get('verdict', '')}", style)
        self.ui.say(f"  logged iteration {r['nn']:02d}" + (f", committed {r['commit'][:7]}" if r["commit"] else
                                                          ", nothing to commit"), "dim")

    def _finish_capped(self, state: dict, e: CapExceeded) -> str:
        self.finish(state, {"goal": "budget_usd", "session": "the session cap", "daily": "the daily cap"}[e.cap])
        return "finished"

    # Check-ins --------------------------------------------------------------------------

    def checkin(self, state: dict, last: dict) -> str:
        wt = state["wt_path"]
        config_diff = ""
        if last["config"] and last["commit"]:
            config_diff = _git(wt, "show", "--format=", "--no-color", last["commit"], "--", *last["config"])
        read_from_checkout = [p for p in last["config"] if os.path.basename(p) in goals.READ_FROM_CHECKOUT]
        note = (f"{', '.join(read_from_checkout)}: daedalus reads these from your checkout, never the worktree, so this "
                "edit changes nothing that runs or gets installed until you merge." if read_from_checkout else None)
        if last["config"] and not config_diff.strip():  # edited, then put back within the iteration
            last["config_restored"] = True
            save(state)
            restored = (f"{', '.join(last['config'])} was edited during this iteration and restored: no net change, "
                        "but the iteration's own runs used the edited version.")
            note = f"{restored} {note}" if note else restored
        modified, tests_diff = goals.tests_modified(wt, state["base"], state["baseline"].get("test_files", []))
        batch, total, n = spend(state)
        info = CheckIn(len(state["iterations"]), state["limit_iterations"], batch, state["budget_usd"], last["summary"],
                       goals.diff_stat(wt, state["base"]), last["config"], config_diff, note, modified,
                       _capped(tests_diff, MAX_TESTS_DIFF_CHARS), total, n)
        try:
            choice, text = self.ui.checkin(info)
        except (EOFError, KeyboardInterrupt):
            state["status"], state["pause"] = "paused", "checkin"
            save(state)
            self.ui.say(f"Paused {state['run_id']} at a check-in; {_resume(state)} continues.", "yellow")
            return "paused"
        if choice == "redirect" and text:
            state["redirect"] = text
            save(state)
        return choice

    # The end ----------------------------------------------------------------------------

    def finish(self, state: dict, reason: str) -> dict:
        state["status"], state["pause"], state["stop_reason"] = "stopped", None, reason
        last = state["iterations"][-1] if state["iterations"] else None
        state["done"] = bool(last and last["done"]) and reason == "done"
        state["report"] = self.report(state)
        save(state)
        self.ui.say(state["report"], "bold" if state["done"] else None)
        self.lessons(state)
        return state

    def report(self, state: dict) -> str:
        its = state["iterations"]
        last = next((i for i in reversed(its) if i.get("guards")), None)
        b = state["baseline"]
        lines = [f"Run {state['run_id']} ended: {state['stop_reason']}" +
                 (" — the goal is honestly done." if state["done"] else "."),
                 f"  iterations: {len(its)} of {state['limit_iterations']} · {_spend_text(state)}",
                 f"  {baseline_text(b)}"]
        if last is not None:
            lines.append(f"  last check: `{' '.join(state['argv'])}` exit {last['exit_code']}"
                         + (f", {_counts_text(last['counts'])}" if last["counts"] else ""))
            lines += [f"    {'✓' if g['held'] else '✗'} {g['name']}: {g['detail']}" for g in last["guards"]]
        else:
            lines.append("  no check ran after an edit")
        lines.append(f"  config files changed: {', '.join(state['config_changed']) or 'none'}")
        modified, tests_diff = goals.tests_modified(state["wt_path"], state["base"], b.get("test_files", []))
        lines.append(f"  existing tests modified: {', '.join(modified) or 'none'}")
        if modified:
            lines += ["    " + line for line in _capped(tests_diff, MAX_TESTS_DIFF_CHARS).rstrip().splitlines()]
        committed = _git(state["wt_path"], "diff", "--stat", f"{state['base']}..HEAD").rstrip()
        lines += [f"  git diff --stat ({state['branch']} against its start):",
                  *("    " + line for line in (committed or "(no committed changes)").splitlines())]
        slug = state["wt_slug"]
        if state.get("plan_run"):
            lines.append("  Next: " + ("the plan's next task." if state["done"] else
                                       f"{_resume(state)} (/runs shows the plan's run)."))
            return "\n".join(lines)
        lines.append(f"  Next: /wt diff {slug}, then /wt merge {slug}" if state["done"] else
                     f"  Next: /wt diff {slug} to look; /goal resume <guidance> to go on (another "
                     f"{state['max_iterations']} iterations and ${state['budget_usd']:.2f}); /wt drop {slug} --force "
                     "to throw it away.")
        return "\n".join(lines)

    # Lessons ----------------------------------------------------------------------------

    def lessons(self, state: dict) -> list[str]:
        """At most 3 lessons, for failures the run later fixed: one tool-less, history-less call reads the logs."""
        its = [i for i in state["iterations"] if i.get("guards")]
        fixed = any(not a["done"] and any(later["done"] or later["exit_code"] == 0 for later in its[k + 1:])
                    for k, a in enumerate(its))
        if not fixed:
            self.ui.say("No lessons: nothing failed and was then fixed in this run.", "dim")
            return []
        logs = "\n\n".join(self._read_log(state, i["nn"]) for i in state["iterations"])
        agent = Agent(self.session, self.ds, self.cfg, self.session_id, meter=self.meter, run_id=state["run_id"],
                      run_cap=state["limit_budget"])
        try:
            raw = agent.one_shot(LESSON_SYSTEM, f"The run's logs, in order:\n\n{logs}")
        except (CapExceeded, DeepSeekError, Interrupted) as e:
            self.ui.say(f"No lessons proposed: {e}", "yellow")
            return []
        made = []
        for lesson in parse_lessons(raw)[:MAX_LESSONS]:
            try:
                content, rel = lesson_note(lesson, state)
                p = tools.propose_lesson(self.session, rel, content, SOURCE, f"lesson: {_one_line(lesson['title'], 150)}")
            except (ValueError, KeyError, TypeError, queue.ProposalError, tools.PolicyError) as e:
                self.ui.say(f"  a lesson wasn't proposed: {e}", "yellow")
                continue
            made.append(p.id)
            self.ui.say(f"→ proposed lesson {p.id}: shared:{p.rel_path}", "green")
        self.proposals += made
        if made:
            self.ui.say("/review lessons walks them.", "dim")
        return made


LESSON_SYSTEM = """\
You read a coding run's logs and extract lessons. A lesson records something that FAILED in the run and was \
LATER FIXED: what broke, why it broke, and the fix. Nothing else qualifies: no successes, no style notes, no \
unfixed failures. Most runs have none or one. At most 3.

Reply with JSON only, no prose: {"lessons": [{"file": "<kebab-case-name>.md", "title": "<short title>", \
"applies_to": ["<tag>", ...], "broke": "<what broke>", "why": "<why>", "fix": "<the fix>"}]}. \
Use {"lessons": []} when nothing qualifies."""


def parse_lessons(raw: str) -> list[dict]:
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
    items = data.get("lessons") if isinstance(data, dict) else None
    return [x for x in items or [] if isinstance(x, dict)]


def lesson_note(lesson: dict, state: dict) -> tuple[str, str]:
    """(content, rel_path in the shared inbox) for one parsed lesson."""
    for key in ("title", "broke", "why", "fix"):
        if not isinstance(lesson.get(key), str) or not lesson[key].strip():
            raise ValueError(f"the lesson has no {key}")
    name = re.sub(r"[^a-z0-9-]+", "-", str(lesson.get("file", lesson["title"])).lower().removesuffix(".md")).strip("-")
    tags = [t for t in lesson.get("applies_to", []) if isinstance(t, str) and re.fullmatch(r"[A-Za-z0-9_-]+", t)]
    log_link = f"[[logs/{state['goal_slug']}-{state['iterations'][-1]['nn']:02d}]]"
    fm = ("---\ntype: lesson\nstatus: draft\n"
          f"created: \"{dt.date.today().isoformat()}\"\nrelated: []\nshared_refs: []\n"
          f"project_refs: [\"{log_link}\"]\nsource: deepseek\napplies_to: {json.dumps(tags or ['coding'])}\n---\n\n")
    body = (f"# {lesson['title'].strip()}\n\n## What broke\n\n{lesson['broke'].strip()}\n\n## Why\n\n"
            f"{lesson['why'].strip()}\n\n## The fix\n\n{lesson['fix'].strip()}\n\nFrom run {state['run_id']} in "
            f"{state['project']} ({log_link}).\n")
    return fm + body, f"{name or 'lesson'}.md"
