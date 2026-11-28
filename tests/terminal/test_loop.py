"""The coding loop (commit 10c), with a scripted model and a scripted check runner.

The worktree, the edit tools, the logs, and the commits are real (a real git
checkout of the world's alpha repo); only DeepSeek and the sandboxed check are
faked. The live sandboxed baseline is test_live.py::test_pytest_counts_from_a_real_run.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from core_conftest import git, head, write_note, write_raw
from fakes import reply_chunks, usage
from rich.console import Console

from daedalus.core import accept, queue, worktree
from daedalus.core.goals import GoalError
from daedalus.core.session import Session
from daedalus.sandbox import venv
from daedalus.sandbox.runner import RunResult
from daedalus.terminal import ledger, loop
from daedalus.terminal.agent import Events
from daedalus.terminal.config import Config
from daedalus.terminal.repl import Repl
from daedalus.core.state import load_session

FAILING = "def test_x():\n    assert False\n"


# The scripted check -------------------------------------------------------------------------

def pytest_out(passed=0, failed=0, skipped=0, xfailed=0, exit_code=None) -> RunResult:
    parts = [f"{n} {w}" for n, w in ((failed, "failed"), (passed, "passed"), (skipped, "skipped"),
                                      (xfailed, "xfailed")) if n]
    summary = ", ".join(parts) or "no tests ran"
    code = exit_code if exit_code is not None else (1 if failed else 0)
    return RunResult(code, f"{'F' * failed}{'.' * passed}\n{summary} in 0.01s\n", "", 0.3, False)


def collected(wt: Path, argv=("pytest", "--collect-only", "-q")) -> RunResult:
    """What `pytest --collect-only -q` would list: every test_* function in the worktree's test_*.py files, or
    only in the files argv names."""
    import re

    named = {a.split("::", 1)[0] for a in argv[1:] if not a.startswith("-")}
    ids = sorted(f"{f.relative_to(wt).as_posix()}::{name}" for f in Path(wt).rglob("test_*.py")
                 if not named or f.relative_to(wt).as_posix() in named
                 for name in re.findall(r"^def (test_\w+)", f.read_text(), re.M))
    return RunResult(0 if ids else 5, "\n".join(ids) + f"\n\n{len(ids)} tests collected in 0.01s\n", "", 0.2, False)


class Runner:
    """Answers each sandboxed check with the next scripted result; records what was run, and where.

    `pytest --collect-only` runs (the node-ID baseline and check) are answered from ``collects`` if scripted,
    else from the worktree's real files, and recorded apart in ``collect_calls``.
    """

    def __init__(self, *results: RunResult, collects: list[RunResult] | None = None, cf=None) -> None:
        self.results, self.calls = list(results), []
        self.collects, self.collect_calls = list(collects or []), []
        self.cf, self.cf_calls = cf, []  # /run's counterfactual runs (a cf-* worktree); red unless scripted

    def __call__(self, session, argv, wt, **_kw) -> RunResult:
        if Path(wt).name.startswith("cf-"):
            self.cf_calls.append((list(argv), Path(wt)))
            return self.cf(Path(wt)) if self.cf else RunResult(1, "F\n1 failed in 0.01s\n", "", 0.1, False)
        if "--collect-only" in argv:
            self.collect_calls.append(list(argv))
            return self.collects.pop(0) if self.collects else collected(Path(wt), argv)
        self.calls.append((list(argv), Path(wt), session.protected))
        if not self.results:
            raise AssertionError("an unscripted check ran")
        return self.results.pop(0)


class UI:
    def __init__(self, *choices) -> None:
        self.events = Events()
        self.said: list[str] = []
        self.checkins: list[loop.CheckIn] = []
        self.choices = list(choices)

    def say(self, text: str, style: str | None = None) -> None:
        self.said.append(text)

    def checkin(self, info):
        self.checkins.append(info)
        return self.choices.pop(0) if self.choices else ("continue", None)


# The scripted model -------------------------------------------------------------------------

def turn(*tool_calls, done="did the step", nxt="nothing", reasoning="Thinking.", ids=None):
    """One iteration's responses: a reply with these tool calls (if any), then the closing PLAN/DONE/NEXT."""
    out = []
    if tool_calls:
        out.append(reply_chunks(reasoning=reasoning, use=usage(prompt_miss=2000, completion=30), tool_calls=[
            {"name": n, "args": a, "id": (ids or {}).get(k, f"c{k}")} for k, (n, a) in enumerate(tool_calls)]))
    out.append(reply_chunks(f"Working.\nPLAN: the plan\nDONE: {done}\nNEXT: {nxt}"))
    return out


def lessons_reply(*items) -> list:
    return reply_chunks(json.dumps({"lessons": list(items)}))


LESSON = {"file": "red-before-green.md", "title": "Write the module after the failing test",
          "applies_to": ["testing"], "broke": "The first check failed: add() didn't exist.",
          "why": "The test came first, on purpose.", "fix": "Wrote add() in mathx.py; the check passed."}


# The project ----------------------------------------------------------------------------------

@pytest.fixture
def proj(gworld, tmp_path, monkeypatch):
    monkeypatch.setattr(worktree, "WORKTREE_ROOT", tmp_path / "worktrees")
    monkeypatch.setattr(venv, "VENV_ROOT", tmp_path / "venvs")
    repo = gworld.alpha_repo
    (repo / "daedalus.toml").write_text('[sandbox]\nallow = ["pytest", "ruff"]\n')
    (repo / "uv.lock").write_text("version = 1\n")
    write_raw(repo, "tests/test_x.py", FAILING)
    write_raw(repo, "tests/test_y.py", "def test_y():\n    assert True\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "a project with tests")
    built = venv.venv_path("alpha", repo)
    (built / "bin").mkdir(parents=True)
    (built / "bin" / "pytest").write_text("#!/bin/sh\n")
    (built / "bin" / "pytest").chmod(0o755)
    (built / venv.READY).write_text("x\n")
    return gworld


def goal(w, slug="hello", commit=True, **fm) -> str:
    fields = {"done_when": "pytest -q", "max_iterations": 3, "budget_usd": 0.5, "checkin_every": 10,
              "protected": [], "related": ["[[storage-engine]]"], **fm}
    write_note(w.alpha, f"goals/{slug}.md", "goal", f"# {slug}\n\n## Goal\n\nMake test_x pass.\n", **fields)
    if commit:
        git(w.alpha_repo, "add", "-A")
        git(w.alpha_repo, "commit", "-q", "-m", f"goal: {slug}")
    return slug


def plain(w) -> Session:
    return Session(w.registry, "alpha", pulled=("beta",))


def make_loop(w, fake, ui, runner, cfg=None) -> loop.Loop:
    return loop.Loop(plain(w), fake.client(), cfg or Config(), "s-loop", ui, runner=runner)


def logs(w, slug="hello") -> list[str]:
    return sorted(p.name for p in (w.alpha / "logs").glob(f"{slug}-*.md"))


def branch_log(w, state) -> list[str]:
    return git(w.alpha_repo, "log", "--format=%s", f"{state['base']}..{state['branch']}").splitlines()[::-1]


# 1. Reaches done ------------------------------------------------------------------------------

def test_the_run_reaches_done_on_iteration_two(proj, fake) -> None:
    goal(proj)
    fake.responses += [*turn(("write_code", {"rel_path": "src/x.py", "content": "def x():\n    return 1\n"}),
                             done="add x", nxt="make test_x pass"),
                       *turn(("write_code", {"rel_path": "tests/test_x.py", "content": "def test_x():\n    assert 1\n"}),
                             done="fix test_x"),
                       lessons_reply(LESSON)]
    runner = Runner(pytest_out(passed=1, failed=1), pytest_out(passed=1, failed=1), pytest_out(passed=2))
    ui = UI()
    state = make_loop(proj, fake, ui, runner).start("hello")

    assert state["stop_reason"] == "done" and state["done"] is True
    assert logs(proj) == ["hello-01.md", "hello-02.md"]
    assert branch_log(proj, state) == ["iter 01: add x", "iter 02: fix test_x"]
    assert len(fake.bodies()) == 5, "two calls per iteration, then the lessons call, and nothing more"
    assert "tools" not in fake.bodies()[-1], "the lessons call has no tools"
    report = state["report"]
    assert "ended: done — the goal is honestly done." in report and "iterations: 2 of 3" in report
    assert "✓ collected" in report and "Next: /wt diff hello, then /wt merge hello" in report
    assert [c[0] for c in runner.calls] == [["pytest", "-q"]] * 3, "the baseline, then the loop's own checks"
    assert all(c[1] == Path(state["wt_path"]) for c in runner.calls)
    log2 = (proj.alpha / "logs" / "hello-02.md").read_text()
    assert "`pytest -q` → exit 0 · collected 2, skipped 0, xfailed 0" in log2 and "done: the check passed" in log2
    (p,) = [queue.get(i) for i in queue.statuses() if queue.get(i).vault == "shared"]
    assert p.rel_path == "inbox/red-before-green.md" and p.source == "deepseek"
    assert ledger.spent(run_id=state["run_id"]) > 0 and all(r["run_id"] == state["run_id"]
                                                           for r in ledger.rows("s-loop"))
    assert git(proj.alpha_repo, "status", "--porcelain") == ""


# 2. max_iterations ----------------------------------------------------------------------------

def test_a_check_that_never_passes_stops_at_max_iterations(proj, fake) -> None:
    goal(proj, max_iterations=2)
    fake.responses += [*turn(done="try one"), *turn(done="try two")]
    runner = Runner(*[pytest_out(passed=1, failed=1)] * 3)
    state = make_loop(proj, fake, UI(), runner).start("hello")
    assert state["stop_reason"] == "max_iterations" and not state["done"]
    assert logs(proj) == ["hello-01.md", "hello-02.md"]
    assert "the check failed (exit 1)" in state["report"]


# 3. Budget ------------------------------------------------------------------------------------

def test_the_goal_budget_stops_the_run_before_the_call_is_sent(proj, fake) -> None:
    goal(proj, budget_usd=0.0001)  # below any call's worst case
    state = make_loop(proj, fake, UI(), Runner(pytest_out(passed=1, failed=1))).start("hello")
    assert fake.bodies() == [], "nothing was sent"
    assert state["stop_reason"] == "budget_usd" and logs(proj) == []
    assert "budget_usd: ended" not in state["report"] and "ended: budget_usd" in state["report"]


# 4. Check-ins ---------------------------------------------------------------------------------

def test_checkins_continue_redirect_and_stop(proj, fake) -> None:
    goal(proj, checkin_every=1, max_iterations=5)
    fake.responses += [*turn(done="one"), *turn(done="two"), *turn(done="three")]
    ui = UI(("continue", None), ("redirect", "REDIRECT-MARKER: try tests/test_y.py"), ("stop", None))
    state = make_loop(proj, fake, ui, Runner(*[pytest_out(passed=1, failed=1)] * 4)).start("hello")
    assert [c.iteration for c in ui.checkins] == [1, 2, 3]
    assert ui.checkins[0].budget == 0.5 and ui.checkins[0].last_summary == "one"
    bodies = fake.bodies()
    third = bodies[2]["messages"][-1]["content"]  # one request per iteration here (no tool calls)
    assert "REDIRECT-MARKER" in third and "REDIRECT-MARKER" not in bodies[1]["messages"][-1]["content"]
    assert state["stop_reason"] == "stopped at a check-in" and len(state["iterations"]) == 3


# 5. ask_user pauses; /answer, then resume, after a restart ------------------------------------

ASK = {"question": "Should alpha store its data in SQLite or Postgres?", "category": "data_source",
       "options": ["SQLite", "Postgres"], "context_refs": ["[[storage-engine]]"]}


def test_ask_user_pauses_and_resume_continues_after_a_restart(proj, fake) -> None:
    goal(proj)
    fake.responses += [reply_chunks(tool_calls=[{"name": "ask_user", "args": ASK, "id": "q1"}])]
    runner = Runner(pytest_out(passed=1, failed=1), pytest_out(passed=2))
    state = make_loop(proj, fake, UI(), runner).start("hello")
    assert (state["status"], state["pause"]) == ("paused", "ask_user")
    qid = state["question"]["id"]
    assert "Not run: the agent asked" in (proj.alpha / "logs" / "hello-01.md").read_text()
    assert len(runner.calls) == 1, "no check ran for the paused iteration"

    with pytest.raises(GoalError, match=f"waiting for your answer to {qid}"):
        make_loop(proj, fake, UI(), runner).resume()
    accept.answer_question(plain(proj), qid, "1")

    fake.responses += [*turn(("write_code", {"rel_path": "tests/test_x.py", "content": "def test_x():\n    pass\n"}),
                             done="fix it with SQLite in mind"), lessons_reply()]
    again = make_loop(proj, fake, UI(), runner).resume()  # a new Loop: state comes from disk
    assert again["stop_reason"] == "done" and [i["n"] for i in again["iterations"]] == [1, 2]
    asked = fake.bodies()[1]["messages"][-1]["content"]
    assert f"Mohamed's answer to your question {qid}" in asked and "answered: true" in asked


# 6. Fresh context -----------------------------------------------------------------------------

def test_each_iteration_starts_fresh_with_only_the_logs(proj, fake) -> None:
    goal(proj, max_iterations=3)
    fake.responses += [
        *turn(("write_code", {"rel_path": "src/one.py", "content": "CONTENT-ONE = 1\n"}), done="one",
              reasoning="REASONING-ONE", ids={0: "call-one"}),
        *turn(("write_code", {"rel_path": "src/two.py", "content": "two = 2\n"}), done="two"),
        *turn(done="three"),
    ]
    make_loop(proj, fake, UI(), Runner(*[pytest_out(passed=1, failed=1)] * 4)).start("hello")
    third = fake.bodies()[4]  # iterations 1 and 2 made two requests each
    assert [m["role"] for m in third["messages"]] == ["system", "user"]
    text = json.dumps(third, ensure_ascii=False)
    assert "# hello — iteration 01" in text and "# hello — iteration 02" in text
    for leaked in ("REASONING-ONE", "call-one", "CONTENT-ONE", "reasoning_content", '"role": "tool"'):
        assert leaked not in text, leaked
    assert "src/one.py" in text, "the diff --stat and the logs carry what changed"


# 7. Anti-gaming -------------------------------------------------------------------------------

def run_one(proj, fake, check, *calls, **fm):
    goal(proj, max_iterations=1, **fm)
    fake.responses += [*turn(*calls, done="made it pass")]
    return make_loop(proj, fake, UI(), Runner(pytest_out(passed=1, failed=1), check)).start("hello")


def failed_guards(state) -> dict:
    return {g["name"]: g["detail"] for g in state["iterations"][-1]["guards"] if not g["held"]}


def test_deleting_the_failing_test_is_not_done(proj, fake) -> None:
    state = run_one(proj, fake, pytest_out(passed=1), ("delete_code", {"rel_path": "tests/test_x.py"}))
    assert not state["done"] and state["stop_reason"] == "max_iterations"
    assert failed_guards(state) == {"collected": "1 fewer test was collected than at the start (2 → 1)",
                                    "node_ids": "1 test from the start no longer exists: tests/test_x.py::test_x",
                                    "suite": "1 test in the suite at the start no longer exists: tests/test_x.py::test_x"}
    assert "the check passed, but 1 fewer test was collected than at the start" in state["iterations"][-1]["verdict"]
    assert "✗ collected" in state["report"]


def test_swapping_the_failing_test_for_a_trivial_one_is_not_done(proj, fake) -> None:
    """The counts match (2 collected, as at the start), but test_x is gone: not done, naming its node ID."""
    state = run_one(proj, fake, pytest_out(passed=2), ("delete_code", {"rel_path": "tests/test_x.py"}),
                    ("write_code", {"rel_path": "tests/test_z.py", "content": "def test_z():\n    assert True\n"}))
    assert not state["done"], "deleting a failing test and adding a trivial one counted as done"
    assert failed_guards(state) == {"node_ids": "1 test from the start no longer exists: tests/test_x.py::test_x",
                                    "suite": "1 test in the suite at the start no longer exists: tests/test_x.py::test_x"}
    assert "tests/test_x.py::test_x" in state["report"]


COLLECTION_ERROR = RunResult(2, "E   ImportError: cannot import name 'x'\nERROR tests/test_x.py\n!!!!! Interrupted: 1 error "
                                 "during collection !!!!!\n1 error in 0.05s\n", "", 0.2, False)
COLLECT_WITH_ERROR = RunResult(2, "tests/test_y.py::test_y\n\n==== ERRORS ====\n____ ERROR collecting tests/test_x.py ____\n"
                                  "ImportError\n==== short test summary info ====\nERROR tests/test_x.py\n"
                                  "!!!!! Interrupted: 1 error during collection !!!!!\n1 test collected, 1 error in 0.05s\n",
                               "", 0.2, False)


def test_a_collection_error_baseline_whose_test_file_is_deleted_is_not_done(proj, fake) -> None:
    """At the start test_x.py can't be imported, so it has no node IDs; the file itself is what's checked."""
    goal(proj, max_iterations=1)
    fake.responses += [*turn(("delete_code", {"rel_path": "tests/test_x.py"}), done="removed the broken test")]
    runner = Runner(COLLECTION_ERROR, pytest_out(passed=1), collects=[COLLECT_WITH_ERROR])
    state = make_loop(proj, fake, UI(), runner).start("hello")
    assert state["baseline"]["collection_errors"] == ["tests/test_x.py"]
    assert state["baseline"]["node_ids"] == ["tests/test_y.py::test_y"]
    assert not state["done"]
    assert failed_guards(state) == {"test_files": "1 test file from the start no longer exists: tests/test_x.py "
                                                  "(the start had collection errors, so files are checked)",
                                    "suite": "1 test in the suite at the start no longer exists: tests/test_x.py::test_x"}
    # done_when's and the whole suite's collection, at the start and again at the check
    assert [c for c in runner.collect_calls] == [["pytest", "--collect-only", "-q"]] * 4


def test_skipping_the_failing_test_is_not_done(proj, fake) -> None:
    skip = "import pytest\n\n\n@pytest.mark.skip\ndef test_x():\n    assert False\n"
    state = run_one(proj, fake, pytest_out(passed=1, skipped=1),
                    ("write_code", {"rel_path": "tests/test_x.py", "content": skip}))
    assert not state["done"]
    assert failed_guards(state) == {"skipped": "1 more test was skipped than at the start (0 → 1)"}


def test_xfailing_the_failing_test_is_not_done(proj, fake) -> None:
    xfail = "import pytest\n\n\n@pytest.mark.xfail\ndef test_x():\n    assert False\n"
    state = run_one(proj, fake, pytest_out(passed=1, xfailed=1),
                    ("write_code", {"rel_path": "tests/test_x.py", "content": xfail}))
    assert not state["done"]
    assert failed_guards(state) == {"xfailed": "1 more test was marked xfail than at the start (0 → 1)"}


def test_a_protected_path_cant_be_edited(proj, fake) -> None:
    state = run_one(proj, fake, pytest_out(passed=1, failed=1),
                    ("edit_code", {"rel_path": "tests/test_x.py", "old": "assert False", "new": "assert True"}),
                    protected=["tests/test_x.py"])
    body = fake.bodies()[1]["messages"]
    refusal = next(m["content"] for m in body if m["role"] == "tool")
    assert refusal.startswith("Refused by rule 6:") and "protected by the goal" in refusal
    assert (Path(state["wt_path"]) / "tests" / "test_x.py").read_text() == FAILING
    assert state["iterations"][-1]["edits"] == []
    assert not failed_guards(state).get("protected")
    assert "these protected paths must not change: tests/test_x.py" in body[1]["content"]


def test_a_protected_change_behind_the_tools_back_fails_the_guard(proj, fake) -> None:
    goal(proj, max_iterations=1, protected=["tests/test_x.py"])
    fake.responses += [*turn(done="nothing")]
    runner = Runner(pytest_out(passed=1, failed=1))

    def check(session, argv, wt, **kw):  # someone else changes the protected file before the check
        if "--collect-only" in argv:
            return runner(session, argv, wt)
        if len(runner.calls) == 1:
            (Path(wt) / "tests" / "test_x.py").write_text("def test_x():\n    pass\n")
            runner.calls.append(None)
            return pytest_out(passed=2)
        return runner(session, argv, wt)

    state = make_loop(proj, fake, UI(), check).start("hello")
    assert failed_guards(state) == {"protected": "protected path changed: tests/test_x.py"}


# 8. Config edits check in at once -----------------------------------------------------------

def test_a_conftest_edit_checks_in_at_once_with_its_diff(proj, fake) -> None:
    goal(proj, checkin_every=10, max_iterations=3)
    fake.responses += [*turn(("write_code", {"rel_path": "tests/conftest.py", "content": "collect_ignore = ['x']\n"}),
                             done="tweak collection"),
                       *turn(("edit_code", {"rel_path": "uv.lock", "old": "version = 1", "new": "version = 2"}),
                             done="bump the lock")]
    ui = UI(("continue", None), ("stop", None))
    make_loop(proj, fake, ui, Runner(*[pytest_out(passed=1, failed=1)] * 3)).start("hello")
    first, second = ui.checkins
    assert first.iteration == 1 and first.config_changed == ["tests/conftest.py"]
    assert "+collect_ignore = ['x']" in first.config_diff and first.lockfile_note is None
    assert second.config_changed == ["uv.lock"] and "changes nothing that runs or gets installed until you merge" \
        in second.lockfile_note


# 9. Only human goals launch -------------------------------------------------------------------

def test_an_agents_draft_goal_is_refused_and_launches_once_accepted(proj, fake) -> None:
    goal(proj, source="deepseek", status="draft")
    with pytest.raises(GoalError, match="Only you launch goals"):
        make_loop(proj, fake, UI(), Runner()).start("hello")
    assert worktree.list_worktrees(proj.registry, "alpha") == [] and loop.all_runs() == []
    goal(proj, source="deepseek", status="accepted")
    fake.responses += [*turn(done="fixed")]
    state = make_loop(proj, fake, UI(), Runner(pytest_out(passed=1, failed=1), pytest_out(passed=2))).start("hello")
    assert state["stop_reason"] == "done"


@pytest.mark.parametrize("entry, match", [
    ('["tests/test_x.py"]', "a whole list typed into one item"),  # what Obsidian wrote: the schema refuses it
    ("tests/nope.py", "doesn't exist in .* protects nothing"),
])
def test_a_protected_path_that_doesnt_exist_is_refused_before_any_worktree(proj, fake, entry, match) -> None:
    """Obsidian's Properties panel can store '["a.py"]' as one item: that would protect nothing."""
    goal(proj, protected=[entry])
    with pytest.raises(GoalError, match=match):
        make_loop(proj, fake, UI(), Runner()).start("hello")
    assert worktree.list_worktrees(proj.registry, "alpha") == [] and loop.all_runs() == []


# 10. A bad done_when fails before anything exists --------------------------------------------

@pytest.mark.parametrize("done_when, match", [("rm -rf /", "isn't in this project's daedalus.toml allow list"),
                                              ("ruff check", "isn't installed in the project venv"),
                                              ("/tmp/evil/pytest", "bare program name"),
                                              ("pytest 'unclosed", "can't be parsed")])
def test_a_bad_done_when_is_refused_before_any_worktree(proj, fake, done_when, match) -> None:
    goal(proj, done_when=done_when)
    runner = Runner()
    with pytest.raises(GoalError, match=match):
        make_loop(proj, fake, UI(), runner).start("hello")
    assert worktree.list_worktrees(proj.registry, "alpha") == [] and worktree.records("alpha") == {}
    assert loop.all_runs() == [] and not loop.runs_dir().exists() and runner.calls == []
    assert "agent/hello" not in git(proj.alpha_repo, "branch")


def test_a_check_with_no_pytest_summary_is_refused_and_cleaned_up(proj, fake) -> None:
    goal(proj)
    with pytest.raises(GoalError, match="printed no pytest summary line"):
        make_loop(proj, fake, UI(), Runner(RunResult(4, "ERROR: usage\n", "", 0.1, False))).start("hello")
    assert worktree.list_worktrees(proj.registry, "alpha") == [] and loop.all_runs() == []


# 11. Interrupt --------------------------------------------------------------------------------

class CtrlC(Events):
    def tool(self, outcome) -> None:
        raise KeyboardInterrupt


def test_ctrl_c_pauses_and_resume_commits_the_edits_as_interrupted(proj, fake) -> None:
    goal(proj)
    fake.responses += [*turn(("write_code", {"rel_path": "src/wip.py", "content": "wip = 1\n"}))][:1]
    ui = UI()
    ui.events = CtrlC()
    state = make_loop(proj, fake, ui, Runner(pytest_out(passed=1, failed=1))).start("hello")
    assert (state["status"], state["pause"]) == ("paused", "interrupted")
    wt = Path(state["wt_path"])
    assert (wt / "src" / "wip.py").read_text() == "wip = 1\n"
    assert git(wt, "status", "--porcelain", "--untracked-files=all").strip() == "?? src/wip.py", \
        "the edit stays uncommitted"

    fake.responses += [*turn(("write_code", {"rel_path": "src/wip.py", "content": "wip = 2\n"}), done="finish"),
                       lessons_reply()]
    done = make_loop(proj, fake, UI(), Runner(pytest_out(passed=2))).resume()
    assert branch_log(proj, done) == ["iter 01 (interrupted)", "iter 02: finish"]
    assert "Interrupted with Ctrl-C" in (proj.alpha / "logs" / "hello-01.md").read_text()
    assert done["stop_reason"] == "done"


# The REPL: /goal, /goal list, /review lessons ------------------------------------------------

def test_goal_through_the_repl_then_review_lessons(proj, fake) -> None:
    goal(proj, checkin_every=1, max_iterations=3)
    state_file = proj.root / "state.json"
    state_file.write_text(json.dumps({"active": "alpha", "pulled": []}))
    buf = io.StringIO()
    out = Console(file=buf, width=200, force_terminal=False, color_system=None)
    repl = Repl(load_session(proj.registry_path, state_file), Config(), fake.client(), out,
                registry_path=proj.registry_path, state_path=state_file)
    repl.runner = Runner(pytest_out(passed=1, failed=1), pytest_out(passed=1, failed=1), pytest_out(passed=2))
    fake.responses += [*turn(done="one"), *turn(done="two"), lessons_reply(LESSON)]
    lines = ["/goal hello", "c", "/goal list", "/review lessons", "a"]
    repl.run(lambda prompt="": lines.pop(0) if lines else (_ for _ in ()).throw(EOFError))
    text = buf.getvalue()
    assert "── iteration 1 of 3 ──" in text and "── check-in (scheduled) ── iteration 1 of 3" in text
    assert "ended: done — the goal is honestly done." in text
    assert "→ proposed lesson" in text and "/review lessons walks them." in text
    assert "1 iteration(s)" not in text and "2 iteration(s)  stopped: done" in text
    assert "To accept it as a lesson: /promote shared:inbox/red-before-green.md" in text
    assert (proj.shared / "inbox" / "red-before-green.md").is_file()


def test_goal_is_refused_in_coder_mode(proj, fake) -> None:
    goal(proj)
    state_file = proj.root / "state.json"
    state_file.write_text(json.dumps({"active": "alpha", "pulled": []}))
    buf = io.StringIO()
    repl = Repl(load_session(proj.registry_path, state_file), Config(), fake.client(),
                Console(file=buf, width=200, color_system=None), registry_path=proj.registry_path,
                state_path=state_file)
    lines = ["/wt new other", "/code other", "/goal hello"]
    repl.run(lambda prompt="": lines.pop(0) if lines else (_ for _ in ()).throw(EOFError))
    assert "leave coder mode (/code off) before running a goal" in buf.getvalue()


# Existing tests modified, and spend across resumes (fix after commit 10c) -------------------

def test_edits_to_existing_tests_are_flagged_in_checkins_and_the_report(proj, fake) -> None:
    goal(proj, checkin_every=1, max_iterations=2)
    fake.responses += [
        *turn(("edit_code", {"rel_path": "tests/test_y.py", "old": "assert True", "new": "assert 1 == 1"}),
              done="reword test_y"),
        *turn(("write_code", {"rel_path": "tests/test_new.py", "content": "def test_new():\n    pass\n"}),
              done="add a new test"),
    ]
    ui = UI(("continue", None))
    state = make_loop(proj, fake, ui, Runner(*[pytest_out(passed=1, failed=1)] * 3)).start("hello")
    (checkin,) = ui.checkins
    assert checkin.tests_modified == ["tests/test_y.py"], "a new test file isn't an existing test"
    assert "-    assert True" in checkin.tests_diff and "+    assert 1 == 1" in checkin.tests_diff
    assert len(state["iterations"]) == 2, "flagging doesn't pause the run"
    assert "existing tests modified: tests/test_y.py" in state["report"]
    assert "+    assert 1 == 1" in state["report"]


def test_no_existing_tests_modified_says_none(proj, fake) -> None:
    goal(proj, max_iterations=1)
    fake.responses += [*turn(done="nothing")]
    state = make_loop(proj, fake, UI(), Runner(*[pytest_out(passed=1, failed=1)] * 2)).start("hello")
    assert "existing tests modified: none" in state["report"]


def test_resuming_a_stopped_run_shows_the_total_spend_across_batches(proj, fake) -> None:
    """Each resume of a stopped run is a batch with its own budget; check-ins and the report show the total too."""
    goal(proj, checkin_every=1, max_iterations=2)
    fake.responses += [*turn(done="first"), *turn(done="second")]
    ui1 = UI(("continue", None))
    first = make_loop(proj, fake, ui1, Runner(*[pytest_out(passed=1, failed=1)] * 3)).start("hello")
    assert first["stop_reason"] == "max_iterations" and "in total across" not in first["report"]
    assert ui1.checkins[0].batches == 1
    after_first = ledger.spent(run_id=first["run_id"])

    fake.responses += [*turn(done="third"), *turn(done="fourth")]
    ui2 = UI(("continue", None))
    again = make_loop(proj, fake, ui2, Runner(*[pytest_out(passed=1, failed=1)] * 2)).resume(guidance="try harder")
    total = ledger.spent(run_id=again["run_id"])
    (checkin,) = ui2.checkins
    assert checkin.iteration == 3 and checkin.batches == 2 and checkin.total_spent > after_first
    assert checkin.spent == pytest.approx(checkin.total_spent - after_first, abs=1e-6)
    assert again["stop_reason"] == "max_iterations" and len(again["iterations"]) == 4
    assert "try harder" in fake.bodies()[2]["messages"][-1]["content"], "the guidance reached iteration 3"
    batch = round(total - after_first, 6)
    assert f"spent ${batch:.4f} of $0.50 this batch; ${total:.4f} in total across 2 batches" in again["report"]
