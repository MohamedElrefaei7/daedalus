"""/plans, /diff on a plan, /reject (commit 11); /approve on a plan asks first (commit 12)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "core"))

import pytest  # noqa: E402
from fakes import TEST_KEY  # noqa: E402
from test_plans import BODY, good, plan_world  # noqa: E402
from test_repl import run_repl  # noqa: E402

from daedalus import toolset  # noqa: E402
from daedalus.core import plans, queue  # noqa: E402


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", TEST_KEY)


def test_plans_diff_reject_and_approve_refused(gworld, fake, keyed) -> None:
    plan_world(gworld)
    v1 = plans.submit(gworld.session, "claude", "mathx", good(), BODY, session_cap_usd=1.0).proposal
    before = gworld.snapshot()
    reason = "make the CLI task protect a CLI test I'll write"
    code, out = run_repl(gworld, fake, "/plans", f"/diff {v1.id}", f"/approve {v1.id}", "n",
                         f"/reject {v1.id} {reason}", "/plans")
    assert code == 0 and fake.requests == []

    # /plans: id, slug and version, task count, decisions marked cited or NEW.
    assert f"{v1.id}  mathx v1  2 tasks  decisions: data_source cited, interface NEW" in out
    # /diff: the spec, the decisions table first with the new one highlighted, then the tasks, then the body.
    assert "spec: [[mathx-cli-spec]]" in out
    assert "Decisions (2; 1 new)" in out and "cited [[storage-engine]]" in out and "NEW: binding once approved" in out
    assert "Approving this plan makes 1 new decision binding." in out
    assert out.index("Decisions (2; 1 new)") < out.index("Tasks (2)") < out.index("argparse-free")
    assert "t1  add multiply(a, b)" in out and "done when:  pytest -q tests/test_mathx.py" in out
    assert "caps:       3 iterations, $0.25" in out and "protected:  tests/test_mathx.py" in out
    assert "---\ntype: plan" not in out  # not raw YAML
    # /approve asks first; "n" writes nothing.
    assert "Accept 1 new decision as precedent? [y/N]" not in out  # the prompt goes to read_line, not the console
    assert f"not approved; {v1.id} stays pending. Nothing was written." in out
    # /reject works as before, and plan_status returns the reason.
    assert f"rejected {v1.id}: {reason}" in out
    assert toolset.plan_status(gworld.session, "mathx")["rejection_reason"] == reason
    assert "Rejected, not yet revised:" in out and f"mathx v1  rejected: {reason}" in out
    after = gworld.snapshot()
    assert after["digests"] == before["digests"] and after["heads"] == before["heads"]
    assert queue.get(v1.id).status == "rejected"


def test_plans_shows_v2_with_v1_superseded(gworld, fake, keyed) -> None:
    plan_world(gworld)
    v1 = plans.submit(gworld.session, "claude", "mathx", good(), BODY, session_cap_usd=1.0).proposal
    v2 = plans.revise(gworld.session, "claude", "mathx", good(), BODY, session_cap_usd=1.0).proposal
    code, out = run_repl(gworld, fake, "/plans", f"/diff {v1.id}", f"/diff {v2.id}")
    assert f"{v2.id}  mathx v2  2 tasks" in out
    assert f"    v1 superseded  ({v1.id})" in out
    assert f"{v1.id}  plan mathx v1  (claude, superseded)" in out  # v1 stays readable
    assert "supersedes: [[mathx-v1]]" in out
    assert "Approved plans:\n  none" in out


# Approval asks first (commit 12) ------------------------------------------------------------

def test_approve_writes_nothing_without_an_explicit_y(gworld, fake, keyed, monkeypatch) -> None:
    """Break 5 target: approval without y must write nothing."""
    plan_world(gworld)
    v1 = plans.submit(gworld.session, "claude", "mathx", good(), BODY, session_cap_usd=1.0).proposal
    before = gworld.snapshot()
    asked = []
    import test_repl

    real_script = test_repl.script

    def recording(*lines):
        inner = real_script(*lines)

        def read(prompt="› "):
            asked.append(prompt)
            return inner(prompt)
        return read

    monkeypatch.setattr(test_repl, "script", recording)
    for answer in ("", "n", "yes", "yy", "N"):
        code, out = run_repl(gworld, fake, f"/approve {v1.id}", answer)
        assert f"not approved; {v1.id} stays pending. Nothing was written." in out, answer
        assert "  decision 2 → decisions/interface-python-m-scratch2-mathx-add-2-3.md" in out
        after = gworld.snapshot()
        assert after["digests"] == before["digests"] and after["heads"] == before["heads"], answer
        assert queue.get(v1.id).status == "pending"
    assert "Accept 1 new decision as precedent? [y/N] " in asked
    code, out = run_repl(gworld, fake, f"/approve {v1.id}", "y")
    assert f"approved plan mathx v1 in commit" in out and "alpha:decisions/interface-python-m-scratch2-mathx-add-2-3.md" in out
    assert queue.get(v1.id).status == "applied" and (gworld.alpha / "plans" / "mathx-v1.md").is_file()


def test_a_plan_with_no_new_decisions_asks_plainly(gworld, fake, keyed, monkeypatch) -> None:
    plan_world(gworld)
    fm = good()
    fm["decisions"] = fm["decisions"][:1]
    v1 = plans.submit(gworld.session, "claude", "mathx", fm, BODY, session_cap_usd=1.0).proposal
    asked = []
    import test_repl

    real_script = test_repl.script
    monkeypatch.setattr(test_repl, "script",
                        lambda *lines: (lambda inner: (lambda prompt="› ": asked.append(prompt) or inner(prompt)))(
                            real_script(*lines)))
    run_repl(gworld, fake, f"/approve {v1.id}", "")
    assert "Approve plan mathx-v1? [y/N] " in asked
    assert queue.get(v1.id).status == "pending"


def test_approve_shows_every_revalidation_error(gworld, fake, keyed) -> None:
    plan_world(gworld)
    v1 = plans.submit(gworld.session, "claude", "mathx", good(), BODY, session_cap_usd=1.0).proposal
    (gworld.alpha / "decisions" / "storage-engine.md").unlink()
    code, out = run_repl(gworld, fake, f"/approve {v1.id}")  # refused before any question is asked
    assert "no longer validates, so it stays pending" in out and "decisions[1].basis:" in out
    assert queue.get(v1.id).status == "pending"


def test_queue_cli_needs_accept_decisions_for_a_plan(gworld, capsys) -> None:
    from daedalus import queue_cli

    plan_world(gworld)
    v1 = plans.submit(gworld.session, "claude", "mathx", good(), BODY, session_cap_usd=1.0).proposal
    before = gworld.snapshot()
    argv = ["--vault", "alpha", "--registry", str(gworld.registry_path), "apply", v1.id]
    assert queue_cli.main(argv) == 1
    assert "pass --accept-decisions" in capsys.readouterr().err
    assert gworld.snapshot() == before
    import daedalus.core.plans as core_plans

    monkeypatch_cap = core_plans.default_session_cap
    core_plans.default_session_cap = lambda: 1.0
    try:
        assert queue_cli.main([*argv, "--accept-decisions"]) == 0
    finally:
        core_plans.default_session_cap = monkeypatch_cap
    assert "approved plan mathx v1" in capsys.readouterr().out
    assert queue.get(v1.id).status == "applied"


def test_end_review_never_approves_a_plan(gworld, fake, keyed) -> None:
    from test_session_lifecycle import make_repl

    plan_world(gworld)
    gworld.state_path = gworld.root / "state.json"
    gworld.state_path.write_text('{"active": "alpha", "pulled": ["beta"]}')
    v1 = plans.submit(gworld.session, "claude", "mathx", good(), BODY, session_cap_usd=1.0).proposal
    repl, buf = make_repl(gworld, fake)
    repl.read_line = lambda prompt="": "a"  # approve everything it offers
    repl.review([v1.id])
    repl.plans_waiting()
    out = buf.getvalue()
    assert "No proposals to review." in out
    assert "plans waiting — use /approve <id>:" in out and f"{v1.id}  mathx v1" in out
    assert queue.get(v1.id).status == "pending"
