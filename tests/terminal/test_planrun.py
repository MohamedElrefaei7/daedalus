"""/run (commit 12): an approved plan's tasks as coding-loop runs, in order, on one branch.

As in test_loop.py, the worktree, edit tools, logs, and commits are real; DeepSeek and the sandboxed
checks are scripted (Runner answers each non-collect run in order: the red test, the baseline, then
each iteration's check).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from core_conftest import git, write_note
from fakes import reply_chunks
from test_loop import ASK, UI, Runner, lessons_reply, plain, proj, pytest_out, turn  # noqa: F401

from daedalus.core import accept, goals, planruns, plans, queue, worktree
from daedalus.core.goals import GoalError
from daedalus.core.planruns import RunError
from daedalus.terminal import ledger, loop, planrun
from daedalus.terminal.config import Config


def task(tid="t1", **kw) -> dict:
    base = {"id": tid, "summary": f"task {tid}", "files": ["src/x.py", "tests/test_x.py"],
            "red_test": "tests/test_x.py::test_x fails, then passes", "done_when": "pytest -q",
            "max_iterations": 2, "budget_usd": 0.5}
    return {**base, **kw}


def make_plan(w, *tasks_, slug="mathx", approve=True):
    write_note(w.alpha, "concepts/spec.md", "concept", "# spec\n", related=["[[usgs-fetcher]]"])
    git(w.alpha_repo, "add", "-A")
    git(w.alpha_repo, "commit", "-q", "-m", "spec")
    fm = {"spec": "[[spec]]", "decisions": [{"category": "interface", "choice": "a tiny CLI", "basis": "new"}],
          "tasks": list(tasks_) or [task()]}
    p = plans.submit(plain(w), "claude", slug, fm, "Why it's shaped this way.", session_cap_usd=1.0).proposal
    if approve:
        plans.approve_plan(plain(w), p.id, session_cap_usd=1.0)
    return p


def runner_for(w, fake, ui, runner, cfg=None) -> planrun.PlanRunner:
    return planrun.PlanRunner(plain(w), fake.client(), cfg or Config(), "s-plan", ui, runner=runner)


def statuses(run_id):
    return [(t.task_id, t.status, t.stop_reason) for t in planruns.tasks(run_id)]


RED = pytest_out(passed=1, failed=1)  # the red test run, and a baseline with test_x failing and test_y passing


# The launch rule ------------------------------------------------------------------------------

def test_a_pending_plans_provenance_is_refused(proj) -> None:
    """Break 10 target: the core launch function itself refuses anything but an approved plan."""
    p = make_plan(proj, approve=False)
    with pytest.raises(GoalError, match="is pending, not approved"):
        goals.load(plain(proj), f"plan:{p.id}#t1")
    plans.approve_plan(plain(proj), p.id, session_cap_usd=1.0)
    g = goals.load(plain(proj), f"plan:{p.id}#t1")
    assert (g.ref, g.slug, g.argv, g.max_iterations, g.budget_usd) == \
        (f"plan:{p.id}#t1", "plan-mathx-v1-t1", ("pytest", "-q"), 2, 0.5)
    assert "Task t1 of plan mathx v1" in g.text and "interface: a tiny CLI ([[interface-a-tiny-cli]])" in g.text


def test_an_approved_plan_whose_note_is_no_longer_accepted_is_refused(proj) -> None:
    p = make_plan(proj)
    note = proj.alpha / "plans" / "mathx-v1.md"
    note.write_text(note.read_text().replace("status: accepted", "status: proposed"))
    with pytest.raises(GoalError, match="isn't an accepted plan note"):
        goals.load(plain(proj), f"plan:{p.id}#t1")


def test_run_refuses_a_plan_that_isnt_approved(proj, fake) -> None:
    make_plan(proj, approve=False)
    with pytest.raises(RunError, match="not approved"):
        runner_for(proj, fake, UI(), Runner()).run("mathx")
    assert worktree.records("alpha") == {}


# Tasks in order on one branch -----------------------------------------------------------------

def test_tasks_run_in_order_on_one_branch_and_t2_sees_t1s_work(proj, fake) -> None:
    make_plan(proj, task("t1"), task("t2", files=["src/y.py", "tests/test_y2.py"],
                                     red_test="tests/test_y2.py::test_y2 fails first"))
    fake.responses += [*turn(("write_code", {"rel_path": "src/x.py", "content": "X = 1\n"}),
                             ("write_code", {"rel_path": "tests/test_x.py", "content": "def test_x():\n    pass\n"}),
                             done="t1 done"),
                       *turn(("read_code", {"rel_path": "src/x.py"}),
                             ("write_code", {"rel_path": "tests/test_y2.py", "content": "def test_y2():\n    pass\n"}),
                             done="t2 done")]
    # t1: red test, baseline, check. t2: no red run (its test file is one it writes), baseline, check.
    runner = Runner(RED, RED, pytest_out(passed=2), pytest_out(passed=2), pytest_out(passed=3),
                    pytest_out(passed=3))  # … and the final full-suite gate
    ui = UI()
    run = runner_for(proj, fake, ui, runner).run("mathx")
    assert run.status == "done" and run.branch == "agent/plan-mathx-v1"
    assert statuses(run.id) == [("t1", "done", None), ("t2", "done", None)]
    t1, t2 = planruns.tasks(run.id)
    commits = git(proj.alpha_repo, "log", "--format=%H %s", f"{t1.base}..{run.branch}").splitlines()[::-1]
    assert [c.split(" ", 1)[1] for c in commits] == ["iter 01: t1 done", "iter 01: t2 done"]
    assert t2.base == commits[0].split()[0], "t2 started from t1's last commit"
    t2_tools = [m["content"] for m in fake.bodies()[3]["messages"] if m["role"] == "tool"]
    assert any("X = 1" in c for c in t2_tools), "t2 read t1's file on the shared branch"
    assert sorted(p.name for p in (proj.alpha / "logs").glob("plan-mathx-v1-*")) == \
        ["plan-mathx-v1-t1-01.md", "plan-mathx-v1-t2-01.md"]
    assert "t1 · task t1 · 0/2 iterations · $0.00 of $0.50" in ui.said
    assert "every task is done and the full suite passes" in ui.said[-1]
    # Only Mohamed merges: nothing was merged, and dropping the unmerged branch still needs force.
    assert git(proj.alpha_repo, "branch", "--merged", "HEAD").count("plan-mathx-v1") == 0
    with pytest.raises(worktree.WorktreeError, match="not merged"):
        worktree.drop(proj.registry, "alpha", run.wt_slug)
    with pytest.raises(RunError, match="already ran to done on agent/plan-mathx-v1; /wt diff"):
        runner_for(proj, fake, UI(), Runner()).run("mathx")
    worktree.drop(proj.registry, "alpha", run.wt_slug, force=True)
    with pytest.raises(RunError, match="its worktree is gone .* nothing more to run"):
        runner_for(proj, fake, UI(), Runner()).run("mathx")


def test_stops_at_the_first_unfinished_task(proj, fake) -> None:
    """Break 8 target: /run must not go on to t2 after t1 fails."""
    make_plan(proj, task("t1"), task("t2", files=["tests/test_y2.py"], red_test="tests/test_y2.py fails"))
    fake.responses += [*turn(done="try one"), *turn(done="try two"), *turn(done="t2 one"), *turn(done="t2 two")]
    # red test, baseline, two failing checks for t1; then (scripted, so a run that wrongly goes on reaches the
    # assertions) a baseline and two checks t2 must never use
    runner = Runner(RED, RED, RED, RED, RED, pytest_out(passed=3), pytest_out(passed=3))
    run = runner_for(proj, fake, UI(), runner).run("mathx")
    assert statuses(run.id) == [("t1", "failed", "max_iterations"), ("t2", "pending", None)]
    assert (planruns.get(run.id).status, planruns.get(run.id).stop_reason) == ("failed", "max_iterations")
    assert planruns.task(run.id, "t2").run_id is None and len(runner.results) == 3
    with pytest.raises(RunError, match="isn't resumable.*revise the plan"):
        runner_for(proj, fake, UI(), Runner()).run("mathx")


def test_a_check_that_passes_dishonestly_is_honest_done_refused(proj, fake) -> None:
    make_plan(proj, task("t1", max_iterations=1))
    fake.responses += [*turn(("delete_code", {"rel_path": "tests/test_y.py"}), done="deleted a test")]
    runner = Runner(RED, RED, pytest_out(passed=1))
    run = runner_for(proj, fake, UI(), runner).run("mathx")
    assert statuses(run.id) == [("t1", "failed", "honest_done_refused")]
    assert "no longer exists" in planruns.task(run.id, "t1").text


# Preflight ------------------------------------------------------------------------------------

def test_a_red_test_that_already_passes_stops_with_preflight(proj, fake) -> None:
    """Break 9 target: a task whose red test is green before it starts has no honest done-condition."""
    make_plan(proj, task("t1", red_test="tests/test_y.py::test_y fails first"))
    fake.responses += [*turn(done="nothing to do")]  # scripted so a skipped preflight reaches the assertions
    runner = Runner(pytest_out(passed=1), pytest_out(passed=1), pytest_out(passed=2))
    run = runner_for(proj, fake, UI(), runner).run("mathx")
    assert statuses(run.id) == [("t1", "failed", "preflight")]
    assert "red test already passes" in planruns.task(run.id, "t1").text
    assert runner.calls[0][0] == ["pytest", "-q", "tests/test_y.py::test_y"]
    assert planruns.task(run.id, "t1").baseline is None and fake.bodies() == [], "nothing ran after the preflight"
    assert len(runner.calls) == 1


@pytest.mark.parametrize("change, words", [
    ({"protected": ["tests/test_nope.py"]}, None),
    ({"red_test": "tests/test_other.py::test_o fails"}, "lists it in files"),
    ({"red_test": "the CLI prints 5"}, "names no test file"),
    ({"done_when": "ruff check"}, "rule 7"),
])
def test_preflight_stops(proj, fake, change, words) -> None:
    if "protected" in change:  # protected on main when approved, gone by the time the branch is made
        (proj.alpha_repo / "tests" / "test_nope.py").write_text("def test_nope():\n    pass\n")
        make_plan(proj, task("t1", **change))
        git(proj.alpha_repo, "rm", "-q", "tests/test_nope.py")
        git(proj.alpha_repo, "commit", "-q", "-m", "drop test_nope")
        words = "doesn't exist on agent/plan-mathx-v1"
    else:
        make_plan(proj, task("t1", **change))
    run = runner_for(proj, fake, UI(), Runner()).run("mathx")
    t1 = planruns.task(run.id, "t1")
    assert (t1.status, t1.stop_reason) == ("failed", "preflight") and words in t1.text, t1.text
    assert fake.bodies() == []


def test_a_task_too_big_for_todays_room_stops_with_daily_cap(proj, fake) -> None:
    make_plan(proj, task("t1", budget_usd=0.5))
    run = runner_for(proj, fake, UI(), Runner(), cfg=Config(daily_cap_usd=0.30)).run("mathx")
    assert (run.status, run.stop_reason) == ("stopped", "daily_cap")
    assert planruns.task(run.id, "t1").resumable and "less than t1's budget" in planruns.task(run.id, "t1").text
    assert worktree.records("alpha")  # the branch exists; the task just didn't start
    # Resumable: with room again, /run starts it.
    fake.responses += [*turn(("write_code", {"rel_path": "tests/test_x.py", "content": "def test_x():\n    pass\n"}))]
    again = runner_for(proj, fake, UI(), Runner(RED, RED, pytest_out(passed=2), pytest_out(passed=2))).run("mathx")
    assert again.id == run.id and again.status == "done"


# Stopping and resuming ------------------------------------------------------------------------

class ProcessKilled(BaseException):
    """Stands in for a kill -9 mid-call: nothing in the process catches it."""


def killed(_request):
    raise ProcessKilled()


def test_resume_uses_the_stored_baseline(proj, fake) -> None:
    """Break 7 target: an iteration deletes a test, the process is killed, /run resumes. Re-measuring the
    baseline then would lower the bar to what's left; the stored one keeps it."""
    make_plan(proj, task("t1", max_iterations=2))
    fake.responses += [reply_chunks(tool_calls=[{"name": "delete_code", "args": {"rel_path": "tests/test_y.py"},
                                                 "id": "d1"}]), killed]
    runner = Runner(RED, RED)
    with pytest.raises(ProcessKilled):
        runner_for(proj, fake, UI(), runner).run("mathx")
    run = planruns.latest("alpha")
    t1 = planruns.task(run.id, "t1")
    assert (run.status, t1.status, t1.stop_reason) == ("stopped", "stopped", "interrupted")
    stored = t1.baseline
    assert stored["counts"]["passed"] == 1 and stored["counts"]["failed"] == 1
    assert sorted(stored["node_ids"]) == ["tests/test_x.py::test_x", "tests/test_y.py::test_y"]

    fake.responses += [*turn(("write_code", {"rel_path": "tests/test_x.py", "content": "def test_x():\n    pass\n"}),
                             done="fixed test_x")]
    # One check; a second result is scripted so a resume that wrongly re-measures reaches the verdict.
    resumed = Runner(pytest_out(passed=1), pytest_out(passed=1))
    again = runner_for(proj, fake, UI(), resumed).run("mathx")
    t1 = planruns.task(again.id, "t1")
    # Only the check's own collections (done_when's, then the whole suite's at done): none for a new baseline.
    assert resumed.collect_calls == [["pytest", "--collect-only", "-q"]] * 2
    assert "tests/test_y.py::test_y" in stored["suite_node_ids"]
    assert (t1.status, t1.stop_reason) == ("failed", "honest_done_refused"), t1.text
    assert "tests/test_y.py::test_y" in t1.text and t1.baseline == stored
    state = loop.load_state(t1.run_id)
    assert state["baseline"] == stored and [i.get("interrupted", False) for i in state["iterations"]] == [True, False]


def test_a_question_stops_the_run_and_resumes_after_the_answer(proj, fake) -> None:
    make_plan(proj, task("t1"), task("t2", files=["tests/test_y2.py"], red_test="tests/test_y2.py fails"))
    fake.responses += [reply_chunks(tool_calls=[{"name": "ask_user", "args": ASK, "id": "q1"}])]
    run = runner_for(proj, fake, UI(), Runner(RED, RED)).run("mathx")
    t1 = planruns.task(run.id, "t1")
    assert (run.status, t1.status, t1.stop_reason) == ("stopped", "stopped", "question")
    qid = loop.load_state(t1.run_id)["question"]["id"]
    assert qid in t1.text and "SQLite or Postgres" in t1.text
    with pytest.raises(RunError, match=f"waiting for your answer to {qid}"):
        runner_for(proj, fake, UI(), Runner()).run("mathx")
    assert statuses(run.id)[0] == ("t1", "stopped", "question") and planruns.get(run.id).stop_reason == "question"

    accept.answer_question(plain(proj), qid, "1")
    fake.responses += [*turn(("write_code", {"rel_path": "tests/test_x.py", "content": "def test_x():\n    pass\n"})),
                       *turn(("write_code", {"rel_path": "tests/test_y2.py", "content": "def test_y2():\n    pass\n"}))]
    runner = Runner(pytest_out(passed=2), pytest_out(passed=2), pytest_out(passed=3), pytest_out(passed=3))
    done = runner_for(proj, fake, UI(), runner).run("mathx")
    assert done.status == "done" and statuses(run.id) == [("t1", "done", None), ("t2", "done", None)]
    assert len(runner.calls) == 4, "t1 resumed without a new baseline or red run; t2 started fresh; the final gate"


def test_done_tasks_are_never_rerun(proj, fake) -> None:
    make_plan(proj, task("t1"), task("t2", files=["tests/test_y2.py"], red_test="tests/test_y2.py fails"))
    fake.responses += [*turn(("write_code", {"rel_path": "tests/test_x.py", "content": "def test_x():\n    pass\n"})),
                       reply_chunks(tool_calls=[{"name": "ask_user", "args": ASK, "id": "q1"}])]
    run = runner_for(proj, fake, UI(), Runner(RED, RED, pytest_out(passed=2), pytest_out(passed=2))).run("mathx")
    assert statuses(run.id) == [("t1", "done", None), ("t2", "stopped", "question")]
    accept.answer_question(plain(proj), loop.load_state(planruns.task(run.id, "t2").run_id)["question"]["id"], "1")
    fake.responses += [*turn(("write_code", {"rel_path": "tests/test_y2.py", "content": "def test_y2():\n    pass\n"}))]
    runner = Runner(pytest_out(passed=3), pytest_out(passed=3))
    runner_for(proj, fake, UI(), runner).run("mathx")
    assert statuses(run.id)[0] == ("t1", "done", None) and len(runner.calls) == 2, "t1 ran again"


def test_a_running_row_with_no_live_process_is_reconciled(proj) -> None:
    p = make_plan(proj)
    run = planruns.create(plan_change_id=p.id, project="alpha", slug="mathx", version=1, branch="agent/x",
                          worktree_path="/nowhere", wt_slug="x", task_rows=[("t1", "task")])
    with planruns.claim("alpha", run.id):
        planruns.set_task(run.id, "t1", status="running")
        assert planruns.get(run.id).status == "running", "a live run is left alone"
        other = planruns.create(plan_change_id=p.id, project="alpha", slug="mathx", version=1, branch="agent/y",
                                worktree_path="/nowhere", wt_slug="y", task_rows=[("t1", "task")])
        with pytest.raises(RunError, match="already running in alpha; one /run at a time"):
            with planruns.claim("alpha", other.id):
                pass
    # The lock is gone (as when the process dies): the next queue open settles it.
    assert (planruns.get(run.id).status, planruns.get(run.id).stop_reason) == ("stopped", "interrupted")
    assert statuses(run.id) == [("t1", "stopped", "interrupted")]


def test_a_baseline_is_stored_once(proj) -> None:
    p = make_plan(proj)
    run = planruns.create(plan_change_id=p.id, project="alpha", slug="mathx", version=1, branch="agent/x",
                          worktree_path="/nowhere", wt_slug="x", task_rows=[("t1", "task")])
    planruns.set_task(run.id, "t1", baseline={"exit_code": 1})
    with pytest.raises(RunError, match="measured once, never again"):
        planruns.set_task(run.id, "t1", baseline={"exit_code": 0})


# read_runs and /runs --------------------------------------------------------------------------

def test_read_runs_reports_every_task(proj, fake) -> None:
    make_plan(proj, task("t1"), task("t2", files=["tests/test_y2.py"], red_test="tests/test_y2.py fails"))
    fake.responses += [*turn(("write_code", {"rel_path": "tests/test_x.py", "content": "def test_x():\n    pass\n"}),
                             done="made test_x pass"), *turn(done="nope"), *turn(done="still no")]
    runner = Runner(RED, RED, pytest_out(passed=2), pytest_out(passed=2), pytest_out(passed=2, failed=1),
                    pytest_out(passed=2, failed=1))
    run = runner_for(proj, fake, UI(), runner).run("mathx")
    out = planrun.read_runs(plain(proj))
    assert (out["plan"], out["branch"], out["status"], out["stop_reason"]) == \
        ("mathx-v1", "agent/plan-mathx-v1", "failed", "max_iterations")
    assert out["head"] == git(proj.alpha_repo, "rev-parse", run.branch).strip()
    assert "tests/test_x.py" in out["diffstat_against_main"]
    t1, t2 = out["tasks"]
    assert (t1["status"], t1["iterations"], t2["status"], t2["iterations"]) == ("done", 1, "failed", 2)
    assert t1["cost_usd"] > 0 and t1["tokens_in"] > 0 and t1["tokens_out"] > 0
    assert t1["honest_done"]["baseline"] == {"collected": 2, "skipped": 0, "xfailed": 0}
    assert t1["honest_done"]["last_check"] == {"exit_code": 0, "collected": 2, "skipped": 0, "xfailed": 0}
    assert t1["honest_done"]["missing_node_ids"] == [] and t1["honest_done"]["protected_changed"] == []
    assert t1["last_iteration"].startswith("made test_x pass") and t1["log"] == "[[logs/plan-mathx-v1-t1-01]]"
    assert t2["log"] == "[[logs/plan-mathx-v1-t2-02]]" and "the check failed" in t2["honest_done"]["verdict"]
    assert planrun.read_runs(plain(proj), "mathx")["run_id"] == run.id
    status = plans.status(plain(proj), "mathx")
    assert status["run"]["status"] == "failed" and status["run"]["tasks"][1] == \
        {"id": "t2", "status": "failed", "stop_reason": "max_iterations"}
    text = planrun.format_runs(out)
    assert "t2 · task t2 · failed (max_iterations) · 2 iteration(s)" in text


def test_read_runs_is_capped_trimming_the_oldest_iterations_first() -> None:
    out = {"plan": "p-v1", "diffstat_against_main": "x", "tasks": [
        {"id": "t1", "iteration_summaries": [{"n": i, "summary": "a" * 400, "verdict": "v"} for i in range(1, 21)]},
        {"id": "t2", "iteration_summaries": [{"n": i, "summary": "b" * 400, "verdict": "v"} for i in range(1, 21)]}]}
    capped = planrun.cap_output(out)
    import json

    assert len(json.dumps(capped)) <= 12_000
    kept1 = [i["n"] for i in capped["tasks"][0]["iteration_summaries"]]
    assert kept1 == [] or kept1[0] > 1, "t1's oldest went first"
    assert len(capped["tasks"][1]["iteration_summaries"]) >= len(kept1)
    assert "oldest iteration summaries" in capped["trimmed"]


def test_red_test_refs() -> None:
    refs = planrun.red_test_refs("Your tests/test_cli.py::test_add_prints_the_result (protected) fails; "
                                 "tests/test_cli_module.py::test_module_invocation_prints_sum runs 'python -m x'")
    assert refs == [("tests/test_cli.py", "tests/test_cli.py::test_add_prints_the_result"),
                    ("tests/test_cli_module.py", "tests/test_cli_module.py::test_module_invocation_prints_sum")]


# The REPL -------------------------------------------------------------------------------------

def test_run_and_runs_and_plans_in_the_repl(proj, fake) -> None:
    import io
    import json

    from rich.console import Console

    from daedalus.core.state import load_session
    from daedalus.terminal.repl import Repl

    make_plan(proj, task("t1"))
    state_file = proj.root / "state.json"
    state_file.write_text(json.dumps({"active": "alpha", "pulled": []}))
    buf = io.StringIO()
    repl = Repl(load_session(proj.registry_path, state_file), Config(), fake.client(),
                Console(file=buf, width=200, force_terminal=False, color_system=None),
                registry_path=proj.registry_path, state_path=state_file)
    repl.runner = Runner(RED, RED, pytest_out(passed=2), pytest_out(passed=2))
    fake.responses += [*turn(("write_code", {"rel_path": "tests/test_x.py", "content": "def test_x():\n    pass\n"}),
                             done="made test_x pass")]
    lines = ["/plans", "/run mathx", "/runs", "/plans", "/runs nothing-here"]
    repl.run(lambda prompt="": lines.pop(0) if lines else (_ for _ in ()).throw(EOFError))
    text = buf.getvalue()
    assert "mathx v1  plans/mathx-v1.md  not run yet (/run mathx-v1)" in text
    assert "t1 · task t1 · 0/2 iterations · $0.00 of $0.50" in text
    assert "── iteration 1 of 2 ──" in text and "every task is done and the full suite passes on agent/plan-mathx-v1" in text
    assert "plan mathx-v1 · run " in text and "t1 · task t1 · done · 1 iteration(s)" in text
    assert "mathx v1  plans/mathx-v1.md  run done · t1 done" in text
    assert "error: no plan 'nothing-here' in alpha" in text


def test_red_test_refs_ignore_dotted_names_that_arent_files() -> None:
    """The live check's red test (commit 12b): `[tool.pytest.ini_options]` once yielded a file `tool.py`."""
    refs = planrun.red_test_refs("tests/test_pyproject_config.py::test_pytest_testpaths_is_tests reads pyproject.toml "
                                 "and asserts [tool.pytest.ini_options].testpaths == [\"tests\"]; fails before the "
                                 "change, passes after")
    assert refs == [("tests/test_pyproject_config.py",
                     "tests/test_pyproject_config.py::test_pytest_testpaths_is_tests")]
    assert planrun.red_test_refs("see tests/test_x.py.") == [("tests/test_x.py", "tests/test_x.py")]
