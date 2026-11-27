"""/plans, /diff on a plan, /reject, and /approve refused (commit 11)."""

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
    code, out = run_repl(gworld, fake, "/plans", f"/diff {v1.id}", f"/approve {v1.id}", f"/reject {v1.id} {reason}",
                         "/plans")
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
    # /approve is refused, and nothing is written.
    assert "plan approval arrives in commit 12" in out
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
