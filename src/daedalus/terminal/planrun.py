"""/run <plan>: an approved plan's tasks become coding-loop goal runs, in order, on one branch.

★ One branch per plan. The first /run creates one worktree, agent/plan-<slug>-v<N>,
from your checkout's HEAD; the tasks run in order on it, so t2 sees t1's work.
Only you merge it (/wt merge), and dropping unmerged work still needs --force.

★ Only an approved plan runs: goals.load accepts the provenance
plan:<change_id>#<task id> only when the proposal row is applied and the note in
plans/ is accepted.

Before each task (its start, not a resume):
  0. The daily cap: if today's remaining room is less than the task's budget (its
     worst case), stop with daily_cap. That isn't a failure.
  1. Preflight: every protected path exists on the branch, and every test file
     the red test names exists (or this task lists it in files: it writes it).
  2. done_when passes rule 7, including the venv pin.
  3. The red test runs (the files that exist), sandboxed; if it already passes,
     stop with preflight: a task whose test is green before it starts has no
     honest done-condition.
  4. ★ The baseline is measured and STORED in plan_tasks before the first
     iteration. A resume uses the stored one, never a re-measured one.

Then the commit 10c coding loop runs the task: same honest-done rule, check-ins,
and create-only logs. ★ The run stops at the first task that doesn't finish;
later tasks stay pending. question, checkin, daily_cap, and interrupted are
resumable (/run again); max_iterations, budget, honest_done_refused, and
preflight are failures, and the way forward is Claude reading the run
(read_runs) and revising the plan.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import posixpath
import re
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

from daedalus.core import goals, planruns, plans, queue, worktree
from daedalus.core.goals import Goal, GoalError
from daedalus.core.planruns import PlanRun, PlanTask, RunError
from daedalus.core.policy import PolicyError, check_argv
from daedalus.core.session import Session
from daedalus.sandbox import venv as venvs
from daedalus.sandbox.config import load_config
from daedalus.sandbox.errors import SandboxError

from . import ledger
from . import loop as goal_loop

MAX_READ_RUNS_CHARS = 12_000
# Stop texts are stored in plan_tasks and sent to Claude by read_runs, so they name no slash command; the
# terminal adds the command to type (_say_stop).
STOP_TEXTS = {
    "daily_cap_before": ("today's remaining room under the daily cap (${room:.2f} of ${cap:.2f}) is less than "
                         "{tid}'s budget (${budget:.2f}); it can start after 00:00 UTC, or with a higher daily_cap_usd"),
    "daily_cap_during": "the daily cap stopped it mid-task; it can resume after 00:00 UTC",
    "session_cap": "the session cap stopped it; it resumes in a new terminal session",
    "checkin_stop": "Mohamed stopped it at a check-in",
}
# A test file a red test names: tests/test_x.py, optionally ::test_name (or a parametrized id).
RED_REF = re.compile(r"(?<![\w./-])((?:[\w.-]+/)*[\w.-]+\.py)((?:::[\w.\[\]-]+)*)")


def red_test_refs(red_test: str) -> list[tuple[str, str]]:
    """(file, file::node) for each test file the red test names, in order, once each."""
    seen, out = set(), []
    for m in RED_REF.finditer(red_test or ""):
        ref = m.group(1) + m.group(2)
        if ref not in seen:
            seen.add(ref)
            out.append((m.group(1), ref))
    return out


def _git(wt: str | Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(wt), *args], capture_output=True, text=True, check=True,
                          timeout=60).stdout


def approved_plan(session: Session, plan_ref: str) -> tuple[queue.Proposal, str, int]:
    """The plan's proposal row, slug, and version, if it's approved (applied, with an accepted note)."""
    slug, v = plans.resolve(session, plan_ref)
    chain = plans.versions(session, slug)
    if v.id is None or v.status != "applied":
        approved = [c for c in chain if c.status == "applied"]
        hint = (f"; v{approved[-1].version} is approved: /run {slug}-v{approved[-1].version}" if approved else
                "; approve it first with /approve")
        raise RunError(f"plan {slug} v{v.version} is {v.status}, not approved; /run runs only an approved plan{hint}")
    rec = queue.get(v.id)
    note = session.active_vault / rec.rel_path
    if not note.is_file() or plans._fm_of(note.read_text("utf-8")).get("status") != "accepted":
        raise RunError(f"plan {slug} v{v.version} was approved, but {session.active}:{rec.rel_path} isn't an accepted "
                       "plan note any more; /run runs only an approved plan")
    return rec, slug, v.version


def _task_fields(session: Session, rec: queue.Proposal) -> list[dict[str, Any]]:
    fm = plans._fm_of((session.active_vault / rec.rel_path).read_text("utf-8"))
    return [t for t in fm.get("tasks") or [] if isinstance(t, dict)]


class PlanRunner:
    def __init__(self, session: Session, ds, cfg, session_id: str, ui, meter=None, runner=None) -> None:
        self.loop = goal_loop.Loop(session, ds, cfg, session_id, ui, meter=meter, runner=runner)
        self.session, self.cfg, self.ui = session, cfg, ui

    @property
    def proposals(self) -> list[str]:
        return self.loop.proposals

    # The run --------------------------------------------------------------------------

    def run(self, plan_ref: str) -> PlanRun:
        rec, slug, n = approved_plan(self.session, plan_ref)
        fields = _task_fields(self.session, rec)
        tasks = [goals.load(self.session, f"plan:{rec.id}#{t.get('id')}") for t in fields]  # the launch rule
        run = planruns.for_plan(self.session.active, rec.id)
        if run is not None and run.status == "done":
            raise RunError(f"plan {slug} v{n} already ran to done on {run.branch}; /wt diff {run.wt_slug}, then "
                           f"/wt merge {run.wt_slug}")
        if run is not None and run.status == "failed":
            raise RunError(f"plan {slug} v{n}'s run stopped with a failure ({run.stop_reason}: {run.stop_text}), which "
                           "isn't resumable. Ask Claude to read the run (read_runs) and revise the plan (revise_plan); "
                           "--retry for a failed task is Up Next.")
        if run is None:
            made = worktree.create(self.session, f"plan-{slug}-v{n}")
            if made.warning:
                self.ui.say(f"⚠ {made.warning}", "yellow")
            r = made.record
            run = planruns.create(plan_change_id=rec.id, project=self.session.active, slug=slug, version=n,
                                  branch=r.branch, worktree_path=r.path, wt_slug=r.slug,
                                  task_rows=[(str(t.get("id")), str(t.get("summary", ""))) for t in fields])
            self.ui.say(f"Plan run {run.id}: {slug} v{n}, {len(tasks)} task(s), on {run.branch} ({run.worktree_path}).",
                        "bold")
        else:
            self.ui.say(f"Resuming plan run {run.id}: {slug} v{n} on {run.branch}.", "bold")
        with planruns.claim(self.session.active, run.id):
            for goal, t, f in zip(tasks, planruns.tasks(run.id), fields):
                if t.status == "done":
                    continue  # done tasks never run again
                try:
                    status, reason, text = self.run_task(run, goal, t, f)
                except RunError:  # refused before the task ran (e.g. its question is unanswered): as it was
                    planruns.set_run(run.id, "stopped", reason=run.stop_reason, text=run.stop_text, ended=True)
                    raise
                except BaseException as e:
                    msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
                    planruns.set_task(run.id, t.task_id, status="stopped", stop_reason="interrupted", text=msg,
                                      ended_at=queue._now())
                    planruns.set_run(run.id, "stopped", reason="interrupted", text=f"{t.task_id}: {msg}", ended=True)
                    raise
                if status != "done":  # stop at the first task that doesn't finish; later ones stay pending
                    planruns.set_run(run.id, status, reason=reason, text=f"{t.task_id}: {text}", ended=True)
                    self._say_stop(run, t.task_id, status, reason, text)
                    return planruns.get(run.id)
            planruns.set_run(run.id, "done", ended=True)
        self.ui.say(f"Plan {slug} v{n}: every task is done on {run.branch}. Review it with /wt diff {run.wt_slug}, "
                    f"then /wt merge {run.wt_slug}.", "bold green")
        return planruns.get(run.id)

    def _say_stop(self, run: PlanRun, tid: str, status: str, reason: str, text: str) -> None:
        ref = f"{run.slug}-v{run.version}"
        if reason in queue.RESUMABLE:
            nxt = (f"answer it with /answer, then /run {ref}" if reason == "question" else f"/run {ref} resumes {tid}")
            self.ui.say(f"Plan run stopped at {tid} ({reason}): {text}. {nxt}.", "yellow")
        else:
            self.ui.say(f"Plan run failed at {tid} ({reason}): {text}. Not resumable: Claude reads it with read_runs "
                        "and revises the plan.", "bold red")

    # One task -------------------------------------------------------------------------

    def header(self, goal: Goal, t: PlanTask, run: PlanRun) -> None:
        self.ui.say(f"{t.task_id} · {t.summary} · {t.iterations}/{goal.max_iterations} iterations · "
                    f"${t.cost_usd:.2f} of ${goal.budget_usd:.2f}", "bold cyan")

    def run_task(self, run: PlanRun, goal: Goal, t: PlanTask, f: dict) -> tuple[str, str | None, str | None]:
        if t.run_id is None:
            return self._start_task(run, goal, t, f)
        return self._resume_task(run, goal, t)

    def _stop(self, run: PlanRun, t: PlanTask, status: str, reason: str, text: str) -> tuple[str, str, str]:
        planruns.set_task(run.id, t.task_id, status=status, stop_reason=reason, text=text, ended_at=queue._now())
        return status, reason, text

    def _start_task(self, run: PlanRun, goal: Goal, t: PlanTask, f: dict) -> tuple[str, str | None, str | None]:
        now = ledger.utc_now()
        room = self.cfg.daily_cap_usd - ledger.spent(since_utc=ledger.start_of_day(now))
        if room < goal.budget_usd:
            return self._stop(run, t, "stopped", "daily_cap", STOP_TEXTS["daily_cap_before"].format(
                room=max(room, 0), cap=self.cfg.daily_cap_usd, tid=t.task_id, budget=goal.budget_usd))
        self.header(goal, t, run)
        problem = self.preflight(run, goal, f)
        if problem:
            return self._stop(run, t, "failed", "preflight", problem)
        wt = Path(run.worktree_path)
        base = _git(wt, "rev-parse", "HEAD").strip()
        rec = worktree.Record(run.project, run.wt_slug, run.branch, run.worktree_path, base)
        state = self.loop.new_state(goal, rec, base=base)
        state.update(plan_run=run.id, task_id=t.task_id, goal_text=goal.text,
                     goal_link=f"[[plans/{run.slug}-v{run.version}]] task {t.task_id}",
                     resume_hint=f"/run {run.slug}-v{run.version}")
        try:
            baseline, output = self.loop.measure_baseline(state, goal)
        except GoalError as e:
            return self._stop(run, t, "failed", "preflight", str(e))
        # ★ Stored before the first iteration; set_task refuses a second baseline for the same task.
        planruns.set_task(run.id, t.task_id, status="running", run_id=state["run_id"], base=base, baseline=baseline,
                          started_at=queue._now())
        stored = planruns.task(run.id, t.task_id).baseline
        final = self.loop.begin(state, stored, output)
        return self._settle(run, t, final)

    def _resume_task(self, run: PlanRun, goal: Goal, t: PlanTask) -> tuple[str, str | None, str | None]:
        if t.status == "failed" or (t.status == "stopped" and not t.resumable):
            raise RunError(f"{t.task_id} stopped with {t.stop_reason}, which isn't resumable")
        state = goal_loop.load_state(t.run_id)
        if state["status"] == "running":  # its process died mid-iteration: its edits wait, uncommitted
            state["status"], state["pause"] = "paused", "interrupted"
        state["baseline"] = t.baseline  # ★ the stored baseline, never a re-measured one
        goal_loop.save(state)
        self.header(goal, t, run)
        planruns.set_task(run.id, t.task_id, status="running", stop_reason=None, text=None, ended_at=None)
        try:
            final = self.loop.continue_run(state)
        except GoalError as e:  # e.g. the question is still unanswered: it stays stopped, as it was
            planruns.set_task(run.id, t.task_id, status=t.status, stop_reason=t.stop_reason, text=t.text,
                              ended_at=t.ended_at)
            raise RunError(str(e)) from None
        return self._settle(run, t, final)

    def _settle(self, run: PlanRun, t: PlanTask, state: dict) -> tuple[str, str | None, str | None]:
        status, reason, text = outcome(state)
        planruns.set_task(run.id, t.task_id, status=status, stop_reason=reason, text=text,
                          iterations=len(state["iterations"]), cost_usd=ledger.spent(run_id=state["run_id"]),
                          ended_at=None if status == "running" else queue._now())
        return status, reason, text

    # Preflight ------------------------------------------------------------------------

    def preflight(self, run: PlanRun, goal: Goal, f: dict) -> str | None:
        """None if the task can start; else why not (a preflight stop)."""
        wt = Path(run.worktree_path)
        missing = [p for p in goal.protected if not os.path.lexists(wt / p.rstrip("/"))]
        if missing:
            return (f"protected {', '.join(missing)} doesn't exist on {run.branch}, and a protected path must "
                    "already be there when the task starts")
        own = {posixpath.normpath(x).casefold() for x in f.get("files") or [] if isinstance(x, str)}
        refs = red_test_refs(str(f.get("red_test", "")))
        if not refs:
            return ("red_test names no test file (path/test_x.py or path/test_x.py::test_name), so the red test "
                    "can't be run before the task starts")
        absent = [file for file, _ in refs if not (wt / file).is_file()]
        stray = [file for file in absent if posixpath.normpath(file).casefold() not in own]
        if stray:
            return (f"the red test's {', '.join(stray)} doesn't exist on {run.branch}, and this task doesn't list it "
                    "in files, so nothing will write it")
        try:
            config = load_config(self.session.active_repo)
            goals.check_argv_only(goal, config)
            venv = venvs.ensure_venv(self.session.active, self.session.active_repo, goal_loop.sandbox_runner.spawn)
            goals.authorize_check(goal, config, venv)  # rule 7 with the venv pin, which commit 11 couldn't check
        except (GoalError, SandboxError) as e:
            return str(e)
        present = [ref for file, ref in refs if file not in absent]
        if present:
            argv = ["pytest", "-q", *present]
            try:
                check_argv(argv, config)
            except PolicyError as e:
                return f"the red test can't run sandboxed: rule {e.rule}: {e.reason}"
            s = replace(self.session, worktree=wt, protected=goal.protected)
            result = self.loop.runner(s, argv, wt)
            if result.exit_code == 0 and not absent:
                return (f"red test already passes (`{' '.join(argv)}` exited 0 before the task started), so the task "
                        "has no honest done-condition")
        return None


def outcome(state: dict) -> tuple[str, str | None, str | None]:
    """A finished (or paused) coding-loop run, as a plan task's (status, stop reason, text)."""
    if state["status"] == "paused":
        pause = state.get("pause")
        if pause == "ask_user":
            q = state.get("question") or {}
            title = _question_title(q.get("id"))
            return "stopped", "question", f"asked {q.get('id')}: {title}"
        if pause == "checkin":
            last = state["iterations"][-1]["summary"] if state["iterations"] else ""
            return "stopped", "checkin", f"paused at a check-in after iteration {len(state['iterations'])}: {last}"
        return "stopped", "interrupted", f"interrupted in iteration {state['next']}"
    if state.get("done"):
        return "done", None, None
    reason = state.get("stop_reason") or ""
    checked = [i for i in state["iterations"] if i.get("guards")]
    last = checked[-1] if checked else None
    if reason == "stopped at a check-in":
        return "stopped", "checkin", STOP_TEXTS["checkin_stop"]
    if reason == "the daily cap":
        return "stopped", "daily_cap", STOP_TEXTS["daily_cap_during"]
    if reason == "the session cap":
        return "stopped", "interrupted", STOP_TEXTS["session_cap"]
    if last is not None and last.get("exit_code") == 0 and not last.get("done"):
        return "failed", "honest_done_refused", last.get("verdict", "the check passed, but a guard failed")
    if reason == "budget_usd":
        return "failed", "budget", f"the task's budget ran out after {len(state['iterations'])} iteration(s)"
    return "failed", "max_iterations", (f"{len(state['iterations'])} iteration(s) and not done"
                                        + (f": {last['verdict']}" if last else ""))


def _question_title(change_id: str | None) -> str:
    if not change_id:
        return ""
    try:
        text = queue.get(change_id).content
    except queue.UnknownProposal:
        return ""
    m = re.search(r"^# (.+)$", text, re.M)
    return m.group(1).strip() if m else ""


# Reading a run: read_runs and /runs ------------------------------------------------------

def _counts(c: dict | None) -> dict[str, int] | None:
    if not c:
        return None
    k = goals.Counts(**c)
    return {"collected": k.collected, "skipped": k.skipped, "xfailed": k.xfailed}


def _log_last_summary(session: Session, state: dict, it: dict) -> str:
    """The last iteration's summary, from its log note: the plan and what's next, besides the DONE line."""
    path = session.active_vault / "logs" / f"{state['goal_slug']}-{it['nn']:02d}.md"
    text = path.read_text("utf-8") if path.is_file() else ""
    plan = re.search(r"^## Plan\s*\n+(.+?)(?=\n## |\Z)", text, re.S | re.M)
    nxt = re.search(r"^## Next\s*\n+(.+?)(?=\n## |\Z)", text, re.S | re.M)
    parts = [it.get("summary") or ""]
    if plan:
        parts.append("plan: " + " ".join(plan.group(1).split()))
    if nxt:
        parts.append("next: " + " ".join(nxt.group(1).split()))
    return " · ".join(p for p in parts if p)


def task_summary(session: Session, t: PlanTask) -> dict[str, Any]:
    out: dict[str, Any] = {"id": t.task_id, "summary": t.summary, "status": t.status, "stop_reason": t.stop_reason,
                           "iterations": t.iterations, "cost_usd": round(t.cost_usd, 6), "tokens_in": 0,
                           "tokens_out": 0}
    if t.text:
        out["question" if t.stop_reason == "question" else "checkin" if t.stop_reason == "checkin" else "note"] = t.text
    if t.run_id is None:
        return out
    tot = ledger.totals(run_id=t.run_id)
    out.update(tokens_in=tot.prompt_cache_hit + tot.prompt_cache_miss, tokens_out=tot.completion,
               cost_usd=round(tot.cost_usd, 6))
    try:
        state = goal_loop.load_state(t.run_id)
    except GoalError:
        return out
    out["iterations"] = len(state["iterations"])
    checked = [i for i in state["iterations"] if i.get("guards")]
    last = checked[-1] if checked else None
    out["honest_done"] = {
        "baseline": _counts((t.baseline or {}).get("counts")),
        "baseline_exit_code": (t.baseline or {}).get("exit_code"),
        "last_check": None if last is None else {"exit_code": last["exit_code"], **(_counts(last["counts"]) or {})},
        "missing_node_ids": None if last is None else last.get("missing_node_ids"),
        "protected_changed": None if last is None else last.get("protected_changed"),
        "verdict": None if last is None else last.get("verdict"),
    }
    if state["iterations"]:
        it = state["iterations"][-1]
        out["last_iteration"] = _log_last_summary(session, state, it)
        out["log"] = f"[[logs/{state['goal_slug']}-{it['nn']:02d}]]"
    out["iteration_summaries"] = [{"n": i["n"], "summary": i.get("summary", ""),
                                   "verdict": i.get("verdict") or ("asked " + i["asked"] if i.get("asked") else None)}
                                  for i in state["iterations"]]
    return out


def read_runs(session: Session, plan_ref: str | None = None) -> dict[str, Any]:
    """How a plan run went, as JSON for Claude (and /runs), at most 12,000 characters."""
    if plan_ref:
        slug, v = plans.resolve(session, plan_ref)
        run = planruns.for_plan(session.active, v.id) if v.id else None
        if run is None:
            raise RunError(f"plan {slug} v{v.version} hasn't run; Mohamed starts it with /run {slug}-v{v.version}")
    else:
        run = planruns.latest(session.active)
        if run is None:
            raise RunError(f"no plan has run in {session.active} yet")
    repo = session.active_repo
    try:
        head = _git(repo, "rev-parse", run.branch).strip()
        stat = _git(repo, "diff", "--stat", f"HEAD...{run.branch}").rstrip()
    except subprocess.CalledProcessError:
        head, stat = None, f"(branch {run.branch} is gone)"
    out: dict[str, Any] = {
        "plan": f"{run.slug}-v{run.version}", "slug": run.slug, "version": run.version, "run_id": run.id,
        "branch": run.branch, "head": head, "diffstat_against_main": stat or "(no changes)",
        "status": run.status, "stop_reason": run.stop_reason, "stop_text": run.stop_text,
        "started_at": run.started_at, "ended_at": run.ended_at,
        "tasks": [task_summary(session, t) for t in planruns.tasks(run.id)],
    }
    return cap_output(out)


def cap_output(out: dict[str, Any], limit: int = MAX_READ_RUNS_CHARS) -> dict[str, Any]:
    """Trim the oldest iteration summaries first (t1's first ones) until the JSON fits, and say so."""
    def size() -> int:
        return len(json.dumps(out, ensure_ascii=False))

    dropped = 0
    for t in out["tasks"]:
        while size() > limit and t.get("iteration_summaries"):
            t["iteration_summaries"].pop(0)
            dropped += 1
            out["trimmed"] = (f"left out the {dropped} oldest iteration summaries to stay under {limit:,} characters; "
                              "each task's last_iteration and log link are kept")
    if size() > limit:
        out["diffstat_against_main"] = out["diffstat_against_main"][:1000] + "\n[… cut]"
        out["trimmed"] = (out.get("trimmed", "") + "; the diffstat was cut").lstrip("; ")
    return out


def format_runs(summary: dict[str, Any]) -> str:
    """read_runs' summary, for a person (/runs)."""
    lines = [f"plan {summary['plan']} · run {summary['run_id']} · {summary['status']}"
             + (f" ({summary['stop_reason']}: {summary['stop_text']})" if summary.get("stop_reason") else ""),
             f"branch {summary['branch']} at {(summary['head'] or '?')[:7]}"]
    for t in summary["tasks"]:
        lines.append(f"  {t['id']} · {t['summary']} · {t['status']}"
                     + (f" ({t['stop_reason']})" if t.get("stop_reason") else "")
                     + f" · {t['iterations']} iteration(s) · ${t['cost_usd']:.4f} · ↑ {t['tokens_in']:,} ↓ {t['tokens_out']:,}")
        hd = t.get("honest_done")
        if hd and hd.get("last_check"):
            b, c = hd.get("baseline") or {}, hd["last_check"]
            lines.append(f"      check: exit {c.get('exit_code')}, collected {c.get('collected')} (start "
                         f"{b.get('collected')}), skipped {c.get('skipped')}, xfailed {c.get('xfailed')}"
                         + (f"; missing: {', '.join(hd['missing_node_ids'])}" if hd.get("missing_node_ids") else "")
                         + (f"; protected changed: {', '.join(hd['protected_changed'])}"
                            if hd.get("protected_changed") else ""))
            if hd.get("verdict"):
                lines.append(f"      {hd['verdict']}")
        for key in ("question", "checkin", "note"):
            if t.get(key):
                lines.append(f"      {key}: {t[key]}")
        if t.get("last_iteration"):
            lines.append(f"      last: {t['last_iteration']}  {t.get('log', '')}")
    lines.append("  diff --stat against your checkout:")
    lines += ["    " + ln for ln in summary["diffstat_against_main"].splitlines()]
    if summary.get("trimmed"):
        lines.append(f"  ({summary['trimmed']})")
    return "\n".join(lines)
