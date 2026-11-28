"""Run integrity (commit 12c): limits counted across resumes, merging a plan branch, goals that already hold,
and the end-of-task collection failing closed. Scripted DeepSeek and checks; real worktrees, commits, merges."""

from __future__ import annotations

import io
import json

import pytest
from core_conftest import git, head
from fakes import reply_chunks
from rich.console import Console
from test_baselines import IMPL, IMPORT_ERROR, NEW_TASK, real_test
from test_loop import UI, Runner, goal, lessons_reply, make_loop, plain, proj, pytest_out, turn  # noqa: F401
from test_planrun import RED, make_plan, runner_for, statuses, task

from daedalus import toolset
from daedalus.core import planruns, queue
from daedalus.core.planruns import RunError
from daedalus.core.state import load_session
from daedalus.sandbox.errors import SandboxError
from daedalus.sandbox.runner import RunResult
from daedalus.terminal import ledger, loop, planrun
from daedalus.terminal.config import Config
from daedalus.terminal.repl import Repl

CONFTEST = ("write_code", {"rel_path": "tests/conftest.py", "content": "# a config edit: checks in at once\n"})


def repl_for(proj, fake, runner=None):
    state_file = proj.root / "state.json"
    state_file.write_text(json.dumps({"active": "alpha", "pulled": []}))
    buf = io.StringIO()
    r = Repl(load_session(proj.registry_path, state_file), Config(), fake.client(),
             Console(file=buf, width=200, force_terminal=False, color_system=None),
             registry_path=proj.registry_path, state_path=state_file)
    r.runner = runner or Runner()
    return r, buf


def drive(repl, *lines):
    queue_ = list(lines)
    repl.run(lambda prompt="": queue_.pop(0) if queue_ else (_ for _ in ()).throw(EOFError))


# Part A: limits count across resumes ----------------------------------------------------------

def test_iterations_are_counted_across_resumes(proj, fake) -> None:
    """Break 1 target: a resume mustn't grant a fresh max_iterations."""
    make_plan(proj, task("t1", max_iterations=2))
    fake.responses += [*turn(CONFTEST, done="tweaked conftest")]
    run = runner_for(proj, fake, UI(("stop", None)), Runner(RED, RED, RED)).run("mathx")
    t1 = planruns.task(run.id, "t1")
    assert (t1.status, t1.stop_reason, t1.iterations) == ("stopped", "checkin", 1)
    # Scripted for two more iterations, so a resume that wrongly grants another batch reaches the assertions.
    fake.responses += [*turn(done="try two"), *turn(done="try three")]
    ui = UI()
    again = runner_for(proj, fake, ui, Runner(RED, RED, RED)).run("mathx")
    t1 = planruns.task(again.id, "t1")
    assert (t1.status, t1.stop_reason, t1.iterations) == ("failed", "max_iterations", 2), t1.text
    assert "t1 · task t1 · 1 of 2 iterations used · $" in ui.said[1]
    assert any(s.startswith("\n── iteration 2 of 2 · $") for s in ui.said)
    assert [b["stop"] for b in t1.batches] == ["checkin", "max_iterations"]
    out = planrun.read_runs(plain(proj))["tasks"][0]
    assert (out["iterations"], out["max_iterations"], out["resumed"]) == (2, 2, 1)
    assert "resumed 1 time; each run stopped: checkin, max_iterations" in planrun.format_runs(planrun.read_runs(plain(proj)))


def _spend(run_id: str, usd: float) -> None:
    with ledger.connect() as con:
        con.execute("INSERT INTO calls (ts_utc, model, peak, prompt_cache_hit, prompt_cache_miss, completion, reasoning, "
                    "cost_usd, estimated, session_id, run_id) VALUES (?, 'deepseek-flash', 0, 0, 1, 1, 0, ?, 0, 'other', ?)",
                    (ledger._iso(ledger.utc_now()), usd, run_id))


def test_budget_is_counted_across_resumes(proj, fake) -> None:
    """Break 2 target: a resume mustn't reset the spend the budget is measured against."""
    make_plan(proj, task("t1", max_iterations=3, budget_usd=0.5))
    fake.responses += [*turn(CONFTEST)]
    run = runner_for(proj, fake, UI(("stop", None)), Runner(RED, RED, RED)).run("mathx")
    _spend(planruns.task(run.id, "t1").run_id, 0.6)  # later calls of this task's run, from another terminal
    calls = len(fake.bodies())
    # Scripted so a resume that wrongly resets the spend reaches the assertions.
    fake.responses += [*turn(done="should not run"), *turn(done="nor this"), *turn(done="nor this")]
    again = runner_for(proj, fake, UI(), Runner(RED, RED, RED)).run("mathx")
    t1 = planruns.task(again.id, "t1")
    assert (t1.status, t1.stop_reason) == ("failed", "budget"), t1.text
    assert "no budget left" in t1.text and len(fake.bodies()) == calls, "nothing was launched"


class ProcessKilled(BaseException):
    pass


def test_a_crash_refunds_nothing(proj, fake) -> None:
    """Break 3 target: iteration 2 is killed mid-call; the resume counts it, so no iterations are left."""
    make_plan(proj, task("t1", max_iterations=2))
    fake.responses += [*turn(done="try one"),
                       reply_chunks(tool_calls=[{"name": "write_code", "args": {"rel_path": "src/x.py",
                                                                                "content": "X = 1\n"}, "id": "w1"}]),
                       lambda _req: (_ for _ in ()).throw(ProcessKilled())]
    with pytest.raises(ProcessKilled):
        runner_for(proj, fake, UI(), Runner(RED, RED, RED)).run("mathx")
    run = planruns.latest("alpha")
    t1 = planruns.task(run.id, "t1")
    assert t1.iterations == 1
    spent_with_the_killed_call = ledger.spent(run_id=t1.run_id)
    calls = len(fake.bodies())
    fake.responses += [*turn(done="try two again"), *turn(done="three")]  # for a resume that wrongly refunds
    again = runner_for(proj, fake, UI(), Runner(RED, RED)).run("mathx")
    t1 = planruns.task(again.id, "t1")
    assert (t1.status, t1.stop_reason, t1.iterations) == ("failed", "max_iterations", 2), t1.text
    assert len(fake.bodies()) == calls, "the killed iteration was counted, so nothing more was launched"
    assert t1.cost_usd >= spent_with_the_killed_call > 0
    state = loop.load_state(t1.run_id)
    assert [i.get("interrupted", False) for i in state["iterations"]] == [False, True]


# Part B: merging a plan branch ------------------------------------------------------------------

def _stopped_with_a_commit(proj, fake):
    make_plan(proj, task("t1", max_iterations=2))
    fake.responses += [*turn(CONFTEST, ("write_code", {"rel_path": "src/x.py", "content": "X = 1\n"}))]
    return runner_for(proj, fake, UI(("stop", None)), Runner(RED, RED, RED)).run("mathx")


def test_merging_an_unfinished_plan_run_asks_first(proj, fake) -> None:
    """Break 4 target."""
    run = _stopped_with_a_commit(proj, fake)
    before = head(proj.alpha_repo)
    repl, buf = repl_for(proj, fake)
    # The answers are "anything but y", and harmless commands if no question is asked.
    drive(repl, f"/wt merge {run.wt_slug}", "/plans", f"/wt merge {run.wt_slug}", "/pending")
    out = buf.getvalue()
    assert "plan mathx v1 · run" in out and "t1 · task t1 · stopped (checkin) · not finished" in out
    assert "final gate (the full suite on the branch): hasn't run" in out
    assert out.count("not merged; agent/plan-mathx-v1 and the run are as they were.") == 2
    assert head(proj.alpha_repo) == before and not planruns.get(run.id).closed
    repl, buf = repl_for(proj, fake)
    drive(repl, f"/wt merge {run.wt_slug}", "y", "/run mathx")
    merged = head(proj.alpha_repo)
    assert merged != before and planruns.get(run.id).merged_sha == merged
    assert f"plan mathx v1: branch merged at {merged[:7]}; revise the plan for further work" in buf.getvalue()


def test_a_done_plan_run_merges_without_asking(proj, fake) -> None:
    make_plan(proj, task("t1"))
    fake.responses += [*turn(("write_code", {"rel_path": "tests/test_x.py", "content": "def test_x():\n    pass\n"}))]
    run = runner_for(proj, fake, UI(), Runner(RED, RED, pytest_out(passed=2), pytest_out(passed=2))).run("mathx")
    assert run.status == "done"
    repl, buf = repl_for(proj, fake)
    drive(repl, f"/wt merge {run.wt_slug}")  # no answer scripted: a question would end the script
    assert "Merge an unfinished plan run?" not in buf.getvalue() and "merged agent/plan-mathx-v1" in buf.getvalue()
    assert planruns.get(run.id).merged_sha == head(proj.alpha_repo)


def test_a_merged_plan_branch_closes_its_run(proj, fake) -> None:
    """Break 5 target: merged by hand with git, the run is closed at the next startup, and /run refuses it."""
    run = _stopped_with_a_commit(proj, fake)
    git(proj.alpha_repo, "merge", "-q", "--no-ff", "-m", "merge by hand", run.branch)
    merge = head(proj.alpha_repo)
    repl, buf = repl_for(proj, fake)
    repl.reconcile_plan_runs()  # what startup does
    assert f"closed plan run {run.id} (mathx v1): its branch agent/plan-mathx-v1 was merged at {merge[:7]}" \
        in buf.getvalue()
    assert planruns.get(run.id).merged_sha == merge
    with pytest.raises(RunError, match=f"branch merged at {merge[:7]}; revise the plan for further work"):
        runner_for(proj, fake, UI(), Runner()).run("mathx")
    assert planrun.read_runs(plain(proj))["merged_sha"] == merge
    repl.command("/plans")
    assert f"run stopped (checkin) · t1 stopped · closed: merged at {merge[:7]}" in buf.getvalue()


def test_a_dropped_plan_branch_is_still_found_merged(proj, fake) -> None:
    run = _stopped_with_a_commit(proj, fake)
    git(proj.alpha_repo, "merge", "-q", "--no-ff", "-m", "merge by hand", run.branch)
    from daedalus.core import worktree

    worktree.drop(proj.registry, "alpha", run.wt_slug)
    closed = planrun.reconcile_merged(plain(proj))
    assert [(r.id, sha) for r, sha in closed] == [(run.id, head(proj.alpha_repo))]


# Part C: goals that already hold --------------------------------------------------------------------

def claim(evidence="the test passes at the start commit without any change"):
    return ("report_already_satisfied", {"evidence": evidence})


PASSES = pytest_out(passed=1)


def claim_turn(*calls):
    """A reply whose last tool call is the claim: the claim ends the turn, so there's no closing reply."""
    return [reply_chunks(tool_calls=[{"name": n, "args": a, "id": f"k{i}"} for i, (n, a) in enumerate(calls)])]


def test_a_verified_claim_is_accepted_and_the_run_goes_on(proj, fake) -> None:
    make_plan(proj, task("t1", **NEW_TASK),
              task("t2", files=["src/y.py", "tests/test_y2.py"], red_test=["tests/test_y2.py::test_y2"]))
    fake.responses += [*claim_turn(real_test("def test_new():\n    assert 1 + 1 == 2\n"), claim()),
                       *turn(("write_code", {"rel_path": "tests/test_y2.py",
                                             "content": "from y import Y\n\n\ndef test_y2():\n    assert Y\n"}),
                             ("write_code", {"rel_path": "src/y.py", "content": "Y = 1\n"}))]

    def at_start(tmp):  # t1's red test passes at its start; t2's can't import y there
        return IMPORT_ERROR if (tmp / "tests" / "test_y2.py").exists() else PASSES

    ui = UI()
    runner = Runner(PASSES,                                      # t1: its check (a claim still runs done_when)
                    RED, pytest_out(passed=3),                    # t2: baseline, check (its red test is new)
                    pytest_out(passed=4),                         # the final gate
                    cf=at_start)
    run = runner_for(proj, fake, ui, runner).run("mathx")
    (info,) = ui.satisfied_checkins
    assert (info.task, info.evidence) == ("t1", "the test passes at the start commit without any change")
    assert statuses(run.id) == [("t1", "satisfied_at_base", None), ("t2", "done", None)]
    assert run.status == "done", "satisfied counts as finished for the final gate"
    t1 = planruns.task(run.id, "t1")
    assert t1.satisfied_sha == t1.base and t1.text.startswith(f"the goal already held at {t1.base[:12]}")
    assert "tests/test_new.py" in git(proj.alpha_repo, "show", "--name-only", "--format=", run.branch + "~1"), \
        "t1's test files are committed on the branch, then t2's work"
    out = planrun.read_runs(plain(proj))
    assert out["tasks"][0]["satisfied_at_base"]["sha"] == t1.base


def test_stopping_at_a_satisfied_check_in_stops_the_run(proj, fake) -> None:
    make_plan(proj, task("t1", **NEW_TASK))
    fake.responses += [*claim_turn(real_test("def test_new():\n    pass\n"), claim())]
    ui = UI()
    ui.satisfied_choices.append("stop")
    run = runner_for(proj, fake, ui, Runner(PASSES, cf=lambda tmp: PASSES)).run("mathx")
    assert statuses(run.id) == [("t1", "stopped", "checkin")]


def test_an_unverified_satisfied_claim_is_refused(proj, fake) -> None:
    """Break 6 target: the goal does NOT hold at the start commit (the red test fails there)."""
    make_plan(proj, task("t1", **{**NEW_TASK, "max_iterations": 2}))
    fake.responses += [*claim_turn(real_test(), claim()), *turn(IMPL, done="X"), lessons_reply()]
    ui = UI()
    runner = Runner(PASSES, PASSES, pytest_out(passed=3), cf=lambda tmp: IMPORT_ERROR)
    run = runner_for(proj, fake, ui, runner).run("mathx")
    t1 = planruns.task(run.id, "t1")
    first = loop.load_state(t1.run_id)["iterations"][0]
    assert first.get("satisfied") is False and ui.satisfied_checkins == []
    assert "the claim that the goal already holds didn't verify" in first["verdict"]
    assert "the red test doesn't pass at the start commit" in first["verdict"]
    nxt = fake.bodies()[1]["messages"][-1]["content"]
    assert "didn't verify" in nxt, "the reason reaches the next iteration"


def test_a_satisfied_claim_with_code_changes_is_refused(proj, fake) -> None:
    """Break 7 target: the red test passes at the start, but the agent also changed src/."""
    make_plan(proj, task("t1", **NEW_TASK))
    fake.responses += [*claim_turn(real_test("def test_new():\n    pass\n"), IMPL, claim())]
    ui = UI()
    # The second result is a final gate, scripted so a claim wrongly verified reaches the assertions.
    run = runner_for(proj, fake, ui, Runner(PASSES, PASSES, cf=lambda tmp: PASSES)).run("mathx")
    first = loop.load_state(planruns.task(run.id, "t1").run_id)["iterations"][0]
    assert ui.satisfied_checkins == [] and not first.get("satisfied")
    assert "the task also changed src/x.py" in first["verdict"]


def test_a_test_file_edited_and_restored_doesnt_count_as_a_change(proj, fake) -> None:
    make_plan(proj, task("t1", **{**NEW_TASK, "files": ["src/x.py", "tests/test_new.py", "tests/test_y.py"]}))
    original = (proj.alpha_repo / "tests" / "test_y.py").read_text()
    touch = ("write_code", {"rel_path": "tests/test_y.py", "content": original + "# x\n"})
    back = ("write_code", {"rel_path": "tests/test_y.py", "content": original})
    fake.responses += [*claim_turn(real_test("def test_new():\n    pass\n"), touch, back, claim())]
    ui = UI()
    run = runner_for(proj, fake, ui, Runner(PASSES, pytest_out(passed=3), cf=lambda tmp: PASSES)).run("mathx")
    assert len(ui.satisfied_checkins) == 1 and planruns.task(run.id, "t1").status == "satisfied_at_base"


def test_a_claim_outside_a_plan_task_is_refused() -> None:
    with pytest.raises(queue.ProposalError, match="works only while running a plan's task"):
        toolset.report_already_satisfied(None, "it holds")


# E1: the end-of-task collection fails closed ------------------------------------------------------------

BASE_IDS = "tests/test_x.py::test_x\ntests/test_y.py::test_y\n\n2 tests collected in 0.01s\n"
WITH_ERROR = RunResult(2, "tests/test_x.py::test_x\ntests/test_y.py::test_y\n\n==== ERRORS ====\n"
                          "ERROR tests/test_helpers.py - ImportError\n2 tests collected, 1 error in 0.01s\n", "", 0.1,
                       False)
COLLECTED = RunResult(0, BASE_IDS, "", 0.1, False)


def test_a_new_collection_error_at_done_is_refused(proj, fake) -> None:
    """Break 9 target: every test ID from the start is listed, but a file that collected cleanly now errors."""
    goal(proj, max_iterations=1)
    fake.responses += [*turn(("write_code", {"rel_path": "tests/test_x.py", "content": "def test_x():\n    pass\n"}))]
    runner = Runner(pytest_out(passed=1, failed=1), pytest_out(passed=2),
                    collects=[COLLECTED, COLLECTED, COLLECTED, WITH_ERROR])
    state = make_loop(proj, fake, UI(), runner).start("hello")
    assert not state["done"]
    assert "collection errors that it didn't have at the start: tests/test_helpers.py" in state["iterations"][-1]["verdict"]


class FailsToCollectAtDone(Runner):
    def __call__(self, session, argv, wt, **kw):
        if argv == ["pytest", "--collect-only", "-q"] and len(self.collect_calls) >= 3:
            raise SandboxError("sandbox unavailable — Daedalus won't run commands unsandboxed")
        return super().__call__(session, argv, wt, **kw)


def test_a_collection_that_cant_run_at_done_is_refused(proj, fake) -> None:
    """Already true in commit 12b; proved here with its own break."""
    goal(proj, max_iterations=1)
    fake.responses += [*turn(("write_code", {"rel_path": "tests/test_x.py", "content": "def test_x():\n    pass\n"}))]
    state = make_loop(proj, fake, UI(), FailsToCollectAtDone(pytest_out(passed=1, failed=1), pytest_out(passed=2))) \
        .start("hello")
    assert not state["done"] and "the whole suite's tests couldn't be listed now" in state["iterations"][-1]["verdict"]


def test_the_satisfied_check_in_in_the_repl(proj, fake) -> None:
    make_plan(proj, task("t1", **NEW_TASK))
    fake.responses += [*claim_turn(real_test("def test_new():\n    pass\n"), claim("subtract already handles it"))]
    repl, buf = repl_for(proj, fake, Runner(PASSES, pytest_out(passed=3), cf=lambda tmp: PASSES))
    asked = []
    lines = ["/run mathx", "a", "/runs"]

    def read(prompt=""):
        asked.append(prompt)
        if not lines:
            raise EOFError
        return lines.pop(0)

    repl.run(read)
    out = buf.getvalue()
    t1 = planruns.tasks(planruns.latest("alpha").id)[0]
    assert f"── check-in ── t1 says the goal already holds at {t1.base[:12]}: subtract already handles it The red " \
           "test passes there." in out
    assert "[a]ccept as satisfied / [s]top? " in asked
    assert "satisfied_at_base at" in out and "every task is done and the full suite passes" in out
