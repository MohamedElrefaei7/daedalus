"""Honest baselines (commit 12b): the whole suite's test IDs, empty baselines named as empty, red for the
right reason, the counterfactual red test, the final full-suite gate, and a config change checking in
before done. Scripted DeepSeek and checks, real worktrees and commits, as in test_loop.py."""

from __future__ import annotations

import io
import json

import pytest
from core_conftest import git
from rich.console import Console
from test_loop import UI, Runner, goal, lessons_reply, make_loop, plain, proj, pytest_out, turn  # noqa: F401
from test_planrun import make_plan, runner_for, statuses, task

from daedalus.core import goals, planruns, plans, worktree
from daedalus.core.planruns import RunError
from daedalus.core.state import load_session
from daedalus.sandbox.runner import RunResult
from daedalus.terminal import loop, planrun
from daedalus.terminal.config import Config
from daedalus.terminal.repl import Repl

NOT_FOUND = RunResult(4, "\nno tests ran in 0.01s\n", "ERROR: file or directory not found: tests/test_new.py\n",
                      0.2, False)
IMPORT_ERROR = RunResult(2, "E   ImportError: cannot import name 'X'\n1 error in 0.01s\n", "", 0.1, False)
FIX_X = ("write_code", {"rel_path": "tests/test_x.py", "content": "def test_x():\n    pass\n"})
NEW_TASK = dict(files=["src/x.py", "tests/test_new.py"], red_test=["tests/test_new.py::test_new"],
                done_when="pytest -q tests/test_new.py", max_iterations=1)


def real_test(content="from x import X\n\n\ndef test_new():\n    assert X == 1\n"):
    return ("write_code", {"rel_path": "tests/test_new.py", "content": content})


IMPL = ("write_code", {"rel_path": "src/x.py", "content": "X = 1\n"})


def counterfactual(tmp):
    """What the red test does in the counterfactual worktree, judged from the files really there."""
    test = (tmp / "tests" / "test_new.py").read_text()
    if "assert True" in test:
        return pytest_out(passed=1)  # it passes whatever the code is
    return pytest_out(passed=1) if (tmp / "src" / "x.py").is_file() else IMPORT_ERROR


# A1: the whole suite's test IDs ---------------------------------------------------------------

def test_a_task_cant_delete_tests_outside_done_when(proj, fake) -> None:
    """Break 1 target: done_when names only tests/test_x.py; deleting tests/test_y.py must still miss done."""
    make_plan(proj, task("t1", done_when="pytest -q tests/test_x.py", max_iterations=1))
    fake.responses += [*turn(("delete_code", {"rel_path": "tests/test_y.py"}), FIX_X, done="fixed x, dropped y")]
    # red test, baseline, the check; a final-gate result is scripted so a missing guard reaches the assertions
    runner = Runner(pytest_out(failed=1), pytest_out(failed=1), pytest_out(passed=1), pytest_out(passed=1))
    run = runner_for(proj, fake, UI(), runner).run("mathx")
    t1 = planruns.task(run.id, "t1")
    assert sorted(t1.baseline["suite_node_ids"]) == ["tests/test_x.py::test_x", "tests/test_y.py::test_y"]
    assert t1.baseline["node_ids"] == ["tests/test_x.py::test_x"]  # done_when's own selection doesn't see test_y
    assert (t1.status, t1.stop_reason) == ("failed", "honest_done_refused"), t1.text
    assert "1 test in the suite at the start no longer exists: tests/test_y.py::test_y" in t1.text


def test_a_goal_cant_delete_tests_outside_done_when_either(proj, fake) -> None:
    """The guard is in the shared loop, so /goal runs have it too."""
    goal(proj, done_when="pytest -q tests/test_x.py", max_iterations=1)
    fake.responses += [*turn(("delete_code", {"rel_path": "tests/test_y.py"}), FIX_X, done="fixed x, dropped y")]
    state = make_loop(proj, fake, UI(), Runner(pytest_out(failed=1), pytest_out(passed=1))).start("hello")
    assert not state["done"]
    assert "tests/test_y.py::test_y" in state["iterations"][-1]["verdict"]
    assert "whole suite at the start: 2 tests" in state["report"]


# A2: an empty baseline is recorded as empty ---------------------------------------------------

def _measure(proj, fake, argv, *results):
    lp = loop.Loop(plain(proj), fake.client(), Config(), "s-b", UI(), runner=Runner(*results))
    made = worktree.create(plain(proj), f"b{len(worktree.records('alpha')) + 1}")
    g = goals.Goal("alpha:goals/x.md", "x", None, "", tuple(argv), 1, 0.5, 3, ())
    state = lp.new_state(g, made.record)
    baseline, _ = lp.measure_baseline(state, g)
    return baseline, lp.runner


def test_an_empty_baseline_is_recorded_as_empty(proj, fake) -> None:
    """Break 2 target: commit 12's live-check shape, done_when naming only a file the task creates."""
    baseline, runner = _measure(proj, fake, ["pytest", "-q", "tests/test_new.py"], NOT_FOUND)
    assert (baseline["empty"], baseline["reason"], baseline["exit_code"], baseline["counts"]) == \
        (True, "all named paths are created by the task", None, None)
    assert baseline["dropped"] == ["tests/test_new.py"] and runner.calls == [], "nothing ran for done_when"
    assert sorted(baseline["suite_node_ids"]) == ["tests/test_x.py::test_x", "tests/test_y.py::test_y"]
    assert loop.baseline_text(baseline).startswith("done_when baseline: empty (all named paths are created by the task)")


def test_exit_5_is_empty_and_a_usage_error_is_refused(proj, fake) -> None:
    baseline, _ = _measure(proj, fake, ["pytest", "-q", "-k", "nothing"], RunResult(5, "\nno tests ran in 0.01s\n", "",
                                                                                     0.1, False))
    assert baseline["empty"] and baseline["reason"] == "pytest ran nothing (exit 5: no tests collected)"
    with pytest.raises(goals.GoalError, match="printed no pytest summary line"):
        _measure(proj, fake, ["pytest", "-q", "--bogus"], RunResult(4, "", "ERROR: usage: unrecognized arguments\n",
                                                                     0.1, False))


def test_a_path_that_exists_stays_and_a_missing_one_is_left_out(proj, fake) -> None:
    baseline, runner = _measure(proj, fake, ["pytest", "-q", "tests/test_x.py", "tests/test_new.py"],
                                pytest_out(failed=1))
    assert baseline["argv"] == ["pytest", "-q", "tests/test_x.py"] and baseline["dropped"] == ["tests/test_new.py"]
    assert not baseline["empty"] and baseline["counts"]["failed"] == 1
    assert runner.calls[0][0] == ["pytest", "-q", "tests/test_x.py"]


# A3: red for the right reason -----------------------------------------------------------------

def test_an_absent_red_test_no_task_creates_is_a_preflight_stop(proj, fake) -> None:
    """Break 3 target: exit 4 for a test no task creates is absent, not red."""
    make_plan(proj, task("t1", files=["src/x.py"], red_test=["tests/test_y.py::test_nope"]))
    fake.responses += [*turn(IMPL)]  # scripted so a wrongly passed preflight reaches the assertions
    runner = Runner(RunResult(4, "\nno tests ran in 0.01s\n", "ERROR: not found: tests/test_y.py::test_nope\n", 0.1,
                              False), pytest_out(passed=1, failed=1), pytest_out(passed=2), pytest_out(passed=2))
    run = runner_for(proj, fake, UI(), runner).run("mathx")
    t1 = planruns.task(run.id, "t1")
    assert (t1.status, t1.stop_reason) == ("failed", "preflight")
    assert "isn't red, it's absent" in t1.text and "exited 4" in t1.text
    assert len(runner.calls) == 1 and fake.bodies() == []


def test_an_absent_red_test_this_task_creates_is_recorded_as_created_by_task(proj, fake) -> None:
    make_plan(proj, task("t1", **NEW_TASK))
    fake.responses += [*turn(real_test(), IMPL)]
    run = runner_for(proj, fake, UI(), Runner(pytest_out(passed=1), pytest_out(passed=3), cf=counterfactual)).run("mathx")
    t1 = planruns.task(run.id, "t1")
    assert (t1.baseline["red_test"], t1.baseline["red_exit"], t1.baseline["empty"]) == ("created_by_task", None, True)
    assert run.status == "done"
    out = planrun.read_runs(plain(proj))
    assert out["tasks"][0]["baseline"]["red_test"] == "created_by_task"
    text = planrun.format_runs(out)
    assert "done_when baseline: empty (all named paths are created by the task)" in text and "red test: created_by_task" in text


# A4: the counterfactual -----------------------------------------------------------------------

def test_the_red_test_must_fail_without_the_change(proj, fake) -> None:
    """Break 4 target: a red test that passes on the old code proves nothing, so the iteration isn't done."""
    make_plan(proj, task("t1", **{**NEW_TASK, "max_iterations": 2}))
    fake.responses += [*turn(real_test("def test_new():\n    assert True\n"), IMPL, done="trivial test"),
                       *turn(real_test(), done="a real test"), lessons_reply()]
    runner = Runner(pytest_out(passed=1), pytest_out(passed=1), pytest_out(passed=3), cf=counterfactual)
    run = runner_for(proj, fake, UI(), runner).run("mathx")
    t1 = planruns.task(run.id, "t1")
    state = loop.load_state(t1.run_id)
    first, second = state["iterations"]
    cf1 = next(g for g in first["guards"] if g["name"] == "counterfactual")
    assert not first["done"] and not cf1["held"] and "your red test passes against the code before your change" in cf1["detail"]
    nxt = fake.bodies()[2]["messages"][-1]["content"]
    assert "your red test passes against the code before your change" in nxt, "the miss is in the next feedback"
    assert second["done"] and (t1.status, run.status) == ("done", "done")
    assert len(runner.cf_calls) == 2 and "cf-" not in git(proj.alpha_repo, "worktree", "list")
    assert not runner.cf_calls[0][1].exists(), "the temporary worktree is removed"


def test_the_counterfactual_runs_against_the_code_before_the_change(proj, fake) -> None:
    """Break 5 target: only the red-test files are copied in, so a real test fails there (the code isn't)."""
    make_plan(proj, task("t1", **NEW_TASK))
    fake.responses += [*turn(real_test(), IMPL, done="X and its test")]
    runner = Runner(pytest_out(passed=1), pytest_out(passed=3), cf=counterfactual)
    run = runner_for(proj, fake, UI(), runner).run("mathx")
    t1 = planruns.task(run.id, "t1")
    assert (t1.status, run.status) == ("done", "done"), t1.text
    (argv, tmp), = runner.cf_calls
    assert argv == ["pytest", "-q", "tests/test_new.py::test_new"]
    cf = next(g for g in loop.load_state(t1.run_id)["iterations"][-1]["guards"] if g["name"] == "counterfactual")
    assert cf["held"] and "with only tests/test_new.py copied in" in cf["detail"]


def test_an_unchanged_red_test_that_was_red_needs_no_counterfactual(proj, fake) -> None:
    make_plan(proj, task("t1", files=["src/x.py"], red_test=["tests/test_x.py::test_x"]))
    fake.responses += [*turn(IMPL)]
    runner = Runner(pytest_out(failed=1), pytest_out(passed=1, failed=1), pytest_out(passed=2), pytest_out(passed=2))
    run = runner_for(proj, fake, UI(), runner).run("mathx")
    assert planruns.task(run.id, "t1").baseline["red_test"] == "red" and runner.cf_calls == []
    assert run.status == "done"


# A5: the final full-suite gate ----------------------------------------------------------------

def test_final_suite_gates_plan_done(proj, fake) -> None:
    """Break 6 target: every task done, but the whole suite fails on the branch: the plan isn't done."""
    make_plan(proj, task("t1", **{**NEW_TASK, "done_when": "pytest -q tests/test_new.py"}),
              task("t2", files=["src/x.py"], red_test=["tests/test_x.py::test_x"],
                   done_when="pytest -q tests/test_x.py"))
    fake.responses += [*turn(real_test(), IMPL), *turn(("write_code", {"rel_path": "src/x.py", "content": "X = 2\n"}))]
    failing = RunResult(1, "..F.\nFAILED tests/test_new.py::test_new - assert 2 == 1\n1 failed, 3 passed in 0.02s\n",
                        "", 0.2, False)
    runner = Runner(pytest_out(passed=1),                                    # t1: check
                    pytest_out(failed=1), pytest_out(failed=1), pytest_out(passed=1),  # t2: red, baseline, check
                    failing, cf=counterfactual)                              # the final gate
    run = runner_for(proj, fake, UI(), runner).run("mathx")
    assert statuses(run.id) == [("t1", "done", None), ("t2", "done", None)]
    assert (run.status, run.stop_reason) == ("failed", "final_suite"), run
    assert "the full suite failed on agent/plan-mathx-v1 (exit 1 (1 failed, 0 errors))" in run.stop_text
    out = planrun.read_runs(plain(proj))
    assert out["final_suite"]["passed"] is False and "FAILED tests/test_new.py::test_new" in out["final_suite"]["output"]
    assert "final suite: FAILED" in planrun.format_runs(out)
    with pytest.raises(RunError, match="isn't resumable"):
        runner_for(proj, fake, UI(), Runner()).run("mathx")


# A config change checks in before done ----------------------------------------------------------

PYPROJECT_EDIT = ("write_code", {"rel_path": "pyproject.toml", "content": "[tool.pytest.ini_options]\ntestpaths = [\"tests\"]\n"})


def test_a_config_change_checks_in_even_when_the_iteration_is_done(proj, fake) -> None:
    goal(proj, max_iterations=3, checkin_every=10)
    fake.responses += [*turn(PYPROJECT_EDIT, FIX_X, done="testpaths")]
    ui = UI(("stop", None))
    state = make_loop(proj, fake, ui, Runner(pytest_out(passed=1, failed=1), pytest_out(passed=2))).start("hello")
    (info,) = ui.checkins
    assert info.config_changed == ["pyproject.toml"] and "+testpaths" in info.config_diff
    assert state["stop_reason"] == "stopped at a check-in" and not state["done"]
    # Resuming with nothing changed since finishes it as done, with no new iteration.
    again = make_loop(proj, fake, UI(), Runner()).resume()
    assert again["done"] and again["stop_reason"] == "done" and len(again["iterations"]) == 1
    assert len(fake.bodies()) == 2, "no model call on the resume"


def test_a_plan_task_stops_at_a_config_check_in_and_resumes_with_its_stored_baseline(proj, fake) -> None:
    make_plan(proj, task("t1", files=["pyproject.toml", "tests/test_x.py"]))
    fake.responses += [*turn(PYPROJECT_EDIT, FIX_X, done="testpaths")]
    runner = Runner(pytest_out(failed=1), pytest_out(passed=1, failed=1), pytest_out(passed=2))
    run = runner_for(proj, fake, UI(("stop", None)), runner).run("mathx")
    t1 = planruns.task(run.id, "t1")
    assert (run.status, t1.status, t1.stop_reason) == ("stopped", "stopped", "checkin")
    assert "config changed: pyproject.toml" in t1.text and "that iteration was otherwise done" in t1.text
    before = planrun.read_runs(plain(proj))
    assert before["tasks"][0]["checkin"] == t1.text and before["stop_reason"] == "checkin"
    collects = len(runner.collect_calls)
    resumed = Runner(pytest_out(passed=2))  # only the final gate runs
    done = runner_for(proj, fake, UI(), resumed).run("mathx")
    assert done.status == "done" and planruns.task(run.id, "t1").baseline == t1.baseline
    assert resumed.collect_calls == [] and collects == 4, "the stored baseline (suite IDs included) isn't re-collected"
    assert planrun.read_runs(plain(proj))["tasks"][0]["baseline"] == before["tasks"][0]["baseline"]


# B1: file names print whole at 80 columns ----------------------------------------------------------

LONG = "CLI lives in src/scratch2/mathx.py as main(argv: list[str] | None = None) -> int built on stdlib argparse"


def test_approve_never_cuts_a_file_name_at_80_columns(proj, fake) -> None:
    """Break 7 target."""
    p = make_plan(proj, task("t1"), approve=False)
    fm = plans._fm_of(p.content)
    fm["decisions"] = [{"category": "interface", "choice": LONG, "basis": "new"}]
    fm = {k: fm[k] for k in ("spec", "decisions", "tasks")}
    v2 = plans.revise(plain(proj), "claude", "mathx", fm, "Why.", session_cap_usd=1.0).proposal
    state_file = proj.root / "state.json"
    state_file.write_text(json.dumps({"active": "alpha", "pulled": []}))
    buf = io.StringIO()
    repl = Repl(load_session(proj.registry_path, state_file), Config(), fake.client(),
                Console(file=buf, width=80, force_terminal=False, color_system=None),
                registry_path=proj.registry_path, state_path=state_file)
    lines = [f"/approve {v2.id}", "y"]
    repl.run(lambda prompt="": lines.pop(0) if lines else (_ for _ in ()).throw(EOFError))
    out = buf.getvalue()
    stem = "interface-cli-lives-in-src-scratch2-mathx-py"
    shown, approved = out.split("approved plan", 1)
    assert f"decisions/{stem}.md" in shown, "the note's name was cut before the question"
    assert f"alpha:decisions/{stem}.md" in approved and "alpha:plans/mathx-v2.md" in approved


# B2 and B3 ------------------------------------------------------------------------------------------

def test_stems_drop_trailing_stop_words() -> None:
    a = plans.decision_stem("interface", LONG)
    b = plans.decision_stem("interface", "CLI parses each operand as int when it is a valid int, else float, and "
                                         "prints str(result) on one line")
    assert (a, b) == ("interface-cli-lives-in-src-scratch2-mathx-py", "interface-cli-parses-each-operand-as-int")
    assert not a.endswith(("-as", "-it")) and not b.endswith(("-as", "-it")) and max(len(a), len(b)) <= 48


def test_consequences_are_written_or_said_to_be_missing(proj) -> None:
    p = make_plan(proj, task("t1"), approve=False)
    fm = {k: plans._fm_of(p.content)[k] for k in ("spec", "decisions", "tasks")}
    fm["decisions"] = [{"category": "interface", "choice": "a tiny CLI", "basis": "new",
                        "consequences": "Every subcommand is another argparse parser."},
                       {"category": "schema", "choice": "results are JSON lines", "basis": "new"},
                       {"category": "schema", "choice": "nothing", "basis": "new", "consequences": "  "}]
    with pytest.raises(plans.PlanInvalid) as exc:
        plans.revise(plain(proj), "claude", "mathx", fm, "Why.", session_cap_usd=1.0)
    assert exc.value.errors == ["decisions[3].consequences: must be non-empty text saying what follows from the "
                                "choice, or left out"]
    fm["decisions"].pop()
    v2 = plans.revise(plain(proj), "claude", "mathx", fm, "Why.", session_cap_usd=1.0).proposal
    plans.approve_plan(plain(proj), v2.id, session_cap_usd=1.0)
    with_c = (proj.alpha / "decisions" / "interface-a-tiny-cli.md").read_text()
    without = (proj.alpha / "decisions" / "schema-results-are-json-lines.md").read_text()
    assert with_c.rstrip().endswith("## Consequences\n\nEvery subcommand is another argparse parser.")
    assert without.rstrip().endswith("## Consequences\n\nNot stated in [[mathx-v2]].")


# pytest's exit 4 for a named test whose module fails to import is red, not absent ---------------------

IMPORT_ERROR_4 = RunResult(4, "E   ImportError: cannot import name 'main'\n=== short test summary info ===\n"
                              "ERROR tests/test_x.py\n1 error in 0.02s\n",
                           "ERROR: found no collectors for /wt/tests/test_x.py::test_x\n", 0.2, False)
ABSENT_4 = RunResult(4, "\nno tests ran in 0.01s\n", "ERROR: not found: /wt/tests/test_x.py::test_nope\n"
                                                    "(no match in any of [<Module test_x.py>])\n", 0.1, False)


def test_classify_run_tells_an_import_error_from_an_absent_test() -> None:
    """Both outputs are real sandboxed pytest runs in scratch2 (commit 12b), trimmed."""
    assert goals.classify_run(IMPORT_ERROR_4.exit_code, IMPORT_ERROR_4.stdout_tail, IMPORT_ERROR_4.stderr_tail) == "red"
    assert goals.classify_run(ABSENT_4.exit_code, ABSENT_4.stdout_tail, ABSENT_4.stderr_tail) == "absent"
    assert goals.classify_run(5, "no tests ran in 0.01s\n") == "absent"
    assert [goals.classify_run(c, "") for c in (0, 1, 2, 3)] == ["pass", "red", "red", "error"]
    assert goals.classify_run(4, "", "ERROR: usage: unrecognized arguments: --bogus\n") == "error"


def test_a_red_test_that_fails_to_import_is_red_at_preflight(proj, fake) -> None:
    """commit 12's t2 shape: a protected test, named by node ID, that can't import what the task will write."""
    make_plan(proj, task("t1", files=["src/x.py"], red_test=["tests/test_x.py::test_x"]))
    fake.responses += [*turn(IMPL)]
    runner = Runner(IMPORT_ERROR_4, pytest_out(passed=1, failed=1), pytest_out(passed=2), pytest_out(passed=2))
    run = runner_for(proj, fake, UI(), runner).run("mathx")
    t1 = planruns.task(run.id, "t1")
    assert (t1.baseline["red_test"], t1.baseline["red_exit"]) == ("red", 4) and run.status == "done"


def test_an_import_error_in_the_counterfactual_counts_as_failing(proj, fake) -> None:
    make_plan(proj, task("t1", **NEW_TASK))
    fake.responses += [*turn(real_test(), IMPL)]
    runner = Runner(pytest_out(passed=1), pytest_out(passed=3), cf=lambda tmp: IMPORT_ERROR_4)
    run = runner_for(proj, fake, UI(), runner).run("mathx")
    assert run.status == "done"


def test_a_config_file_edited_and_restored_says_so(proj, fake) -> None:
    """The commit 12b live check: the agent set pyproject.toml wrong, then put it back; the check-in and read_runs
    said "config changed" over an empty diff."""
    original = (proj.alpha_repo / "pyproject.toml").read_text() if (proj.alpha_repo / "pyproject.toml").exists() else None
    if original is None:
        (proj.alpha_repo / "pyproject.toml").write_text("[project]\nname = 'alpha'\n")
        git(proj.alpha_repo, "add", "pyproject.toml")
        git(proj.alpha_repo, "commit", "-q", "-m", "pyproject")
        original = (proj.alpha_repo / "pyproject.toml").read_text()
    make_plan(proj, task("t1", **{**NEW_TASK, "files": ["pyproject.toml", "tests/test_new.py"]}))
    wrong = ("write_code", {"rel_path": "pyproject.toml", "content": original + "# wrong\n"})
    back = ("write_code", {"rel_path": "pyproject.toml", "content": original})
    fake.responses += [*turn(real_test(), IMPL, wrong, back, done="tried a value, put it back")]
    ui = UI(("stop", None))
    run = runner_for(proj, fake, ui, Runner(pytest_out(passed=1), cf=counterfactual)).run("mathx")
    (info,) = ui.checkins
    assert info.config_changed == ["pyproject.toml"] and info.config_diff.strip() == ""
    assert "was edited during this iteration and restored: no net change" in info.lockfile_note
    t1 = planruns.task(run.id, "t1")
    assert "config edited and restored, no net change: pyproject.toml" in t1.text
    text = planrun.format_runs(planrun.read_runs(plain(proj)))
    assert "collected 1 (start empty)" in text
    log = (proj.alpha / "logs" / "plan-mathx-v1-t1-01.md").read_text()
    assert "(at the start: the done_when baseline is empty)" in log
