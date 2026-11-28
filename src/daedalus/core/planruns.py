"""Plan run state (queue schema v5): one run per approved plan, its tasks in order, on one branch.

The plan note is never edited after approval, so where a run has got to lives
here, in queue.sqlite, never in the vault:

  plan_runs   one row per approved plan that /run has started: its branch and
              worktree (agent/plan-<slug>-v<N>), status, and why it stopped.
  plan_tasks  one row per task: status, the coding loop's run id (its ledger rows
              and logs), the branch commit it started from, and ★ the baseline,
              measured once before its first iteration and stored, so a resume
              never re-measures it (an interrupted iteration that deleted a test
              would otherwise lower the bar for the rest of the task).

Statuses: pending, running, done, stopped, failed. A stop's reason is one of
queue.STOP_REASONS; question, checkin, daily_cap, and interrupted are resumable
(/run again), the rest are failures that Claude fixes with revise_plan.

★ A running run holds an OS lock on its own lock file for its whole life, so
"no live process" is a lock anyone can take; queue.connect() reconciles such
runs to stopped / interrupted, as it reconciles 'applying' proposals.
"""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from . import queue
from .errors import ProposalError
from .vaultio import write_lock


class RunError(ProposalError):
    """A plan run can't start or resume; the message says why."""


@dataclass(frozen=True)
class PlanRun:
    id: str
    plan_change_id: str
    project: str
    slug: str
    version: int
    branch: str
    worktree_path: str
    wt_slug: str
    status: str
    stop_reason: str | None
    stop_text: str | None
    started_at: str
    ended_at: str | None
    updated_at: str


@dataclass(frozen=True)
class PlanTask:
    plan_run_id: str
    task_id: str
    idx: int
    summary: str
    status: str
    stop_reason: str | None
    run_id: str | None
    base: str | None
    baseline: dict[str, Any] | None
    iterations: int
    cost_usd: float
    text: str | None
    started_at: str | None
    ended_at: str | None

    @property
    def resumable(self) -> bool:
        return self.status == "stopped" and self.stop_reason in queue.RESUMABLE


_RUN_COLS = ("id", "plan_change_id", "project", "slug", "version", "branch", "worktree_path", "wt_slug", "status",
             "stop_reason", "stop_text", "started_at", "ended_at", "updated_at")
_TASK_COLS = ("plan_run_id", "task_id", "idx", "summary", "status", "stop_reason", "run_id", "base", "baseline",
              "iterations", "cost_usd", "text", "started_at", "ended_at")


def _run(row) -> PlanRun:
    return PlanRun(*row)


def _task(row) -> PlanTask:
    d = dict(zip(_TASK_COLS, row))
    d["baseline"] = json.loads(d["baseline"]) if d["baseline"] else None
    return PlanTask(**d)


def get(run_id: str) -> PlanRun:
    with queue.connect() as con:
        row = con.execute(f"SELECT {', '.join(_RUN_COLS)} FROM plan_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise RunError(f"no plan run {run_id!r}")
    return _run(row)


def for_plan(project: str, plan_change_id: str) -> PlanRun | None:
    with queue.connect() as con:
        row = con.execute(f"SELECT {', '.join(_RUN_COLS)} FROM plan_runs WHERE project = ? AND plan_change_id = ? "
                          "ORDER BY started_at DESC LIMIT 1", (project, plan_change_id)).fetchone()
    return _run(row) if row else None


def runs(project: str) -> list[PlanRun]:
    with queue.connect() as con:
        rows = con.execute(f"SELECT {', '.join(_RUN_COLS)} FROM plan_runs WHERE project = ? ORDER BY started_at, id",
                           (project,)).fetchall()
    return [_run(r) for r in rows]


def latest(project: str) -> PlanRun | None:
    found = runs(project)
    return max(found, key=lambda r: (r.updated_at, r.started_at)) if found else None


def tasks(run_id: str) -> list[PlanTask]:
    with queue.connect() as con:
        rows = con.execute(f"SELECT {', '.join(_TASK_COLS)} FROM plan_tasks WHERE plan_run_id = ? ORDER BY idx",
                           (run_id,)).fetchall()
    return [_task(r) for r in rows]


def task(run_id: str, task_id: str) -> PlanTask:
    found = [t for t in tasks(run_id) if t.task_id == task_id]
    if not found:
        raise RunError(f"plan run {run_id} has no task {task_id!r}")
    return found[0]


def create(*, plan_change_id: str, project: str, slug: str, version: int, branch: str, worktree_path: str,
           wt_slug: str, task_rows: list[tuple[str, str]]) -> PlanRun:
    """A new run (pending) with every task pending, in order. ``task_rows`` is [(task_id, summary)]."""
    run_id, now = queue.new_id(), queue._now()
    with queue.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute(f"INSERT INTO plan_runs ({', '.join(_RUN_COLS)}) VALUES ({', '.join('?' * len(_RUN_COLS))})",
                        (run_id, plan_change_id, project, slug, version, branch, worktree_path, wt_slug, "pending",
                         None, None, now, None, now))
            for i, (tid, summary) in enumerate(task_rows, 1):
                con.execute("INSERT INTO plan_tasks (plan_run_id, task_id, idx, summary, status) VALUES "
                            "(?, ?, ?, ?, 'pending')", (run_id, tid, i, summary))
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    return get(run_id)


def set_run(run_id: str, status: str, *, reason: str | None = None, text: str | None = None,
            ended: bool = False) -> None:
    now = queue._now()
    with queue.connect() as con:
        con.execute("UPDATE plan_runs SET status = ?, stop_reason = ?, stop_text = ?, updated_at = ?, "
                    "ended_at = ? WHERE id = ?", (status, reason, text, now, now if ended else None, run_id))


def set_task(plan_run_id: str, task_id: str, **fields: Any) -> None:
    """Update a task row. ``baseline`` is stored once: a task that already has one keeps it."""
    if not fields:
        return
    if "baseline" in fields:
        current = task(plan_run_id, task_id).baseline
        if current is not None:
            raise RunError(f"task {task_id} of run {plan_run_id} already has its baseline; it's measured once, never again")
        fields["baseline"] = json.dumps(fields["baseline"], sort_keys=True)
    unknown = set(fields) - set(_TASK_COLS)
    if unknown:
        raise ValueError(f"unknown plan_tasks columns {sorted(unknown)}")
    cols = ", ".join(f"{k} = ?" for k in fields)
    with queue.connect() as con:
        con.execute(f"UPDATE plan_tasks SET {cols} WHERE plan_run_id = ? AND task_id = ?",
                    (*fields.values(), plan_run_id, task_id))


@contextmanager
def claim(project: str, run_id: str) -> Iterator[None]:
    """Hold this run's liveness lock for the block, after checking (under the write lock) that no other plan run
    is running in the project. One /run at a time per project."""
    path = queue.plan_run_lock_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        with write_lock():
            others = [r for r in runs(project) if r.status == "running" and r.id != run_id]  # reconciled by connect
            if others:
                raise RunError(f"plan run {others[0].id} ({others[0].slug} v{others[0].version}) is already running "
                               f"in {project}; one /run at a time per project")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RunError(f"plan run {run_id} is already running in another terminal") from None
            set_run(run_id, "running")
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def summary_for(project: str, plan_change_id: str | None) -> dict[str, Any] | None:
    """The run status plan_status includes: None if the plan has never run."""
    if plan_change_id is None:
        return None
    run = for_plan(project, plan_change_id)
    if run is None:
        return None
    return {"run_id": run.id, "status": run.status, "stop_reason": run.stop_reason, "branch": run.branch,
            "tasks": [{"id": t.task_id, "status": t.status, "stop_reason": t.stop_reason} for t in tasks(run.id)]}
