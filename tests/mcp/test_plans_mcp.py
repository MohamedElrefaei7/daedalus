"""Plans over daedalus-mcp (commit 11): thirteen tools, no source, queue-only, and the reject → revise loop."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "core"))  # test_plans' fixtures live with the core tests

from mcp_helpers import PLAN_TOOLS, TOOLS, run  # noqa: E402
from test_plans import BODY, good, plan_world  # noqa: E402

from daedalus import toolset  # noqa: E402
from daedalus.core import queue  # noqa: E402
from daedalus.core.apply import reject  # noqa: E402


def test_exactly_thirteen_tools_and_none_takes_source(mcp) -> None:
    async def go():
        async with mcp:
            return await mcp.client.list_tools()

    tools = run(go())
    assert {t.name for t in tools} == TOOLS and len(TOOLS) == 13
    assert tuple(t.name for t in tools) == toolset.MCP_NAMES
    for t in tools:
        assert "source" not in t.input_schema.get("properties", {}), t.name
        if t.name in PLAN_TOOLS:
            assert t.input_schema.get("additionalProperties") is False, t.name


def test_neither_deepseek_profile_has_a_plan_tool() -> None:
    from daedalus.terminal.agent import PROFILES, TOOLSETS

    for profile in ("chat", "coder"):
        names = {t["function"]["name"] for t in TOOLSETS[profile]}
        assert not names & PLAN_TOOLS, profile
        assert not set(PROFILES[profile]) & PLAN_TOOLS
    assert not set(toolset.NAMES) & PLAN_TOOLS  # the shared nine stay shared


def test_nothing_lands_before_approval(mcp, mworld) -> None:
    """Break 3 target: a submit_plan that writes into plans/ must fail here."""
    plan_world(mworld)
    before = mworld.snapshot()

    async def go():
        async with mcp:
            v1 = await mcp.ok("submit_plan", {"slug": "mathx", "frontmatter": good(), "body": BODY})
            v2 = await mcp.ok("revise_plan", {"plan_ref": "mathx", "frontmatter": good(), "body": "Second go."})
            return v1, v2

    v1, v2 = run(go())
    after = mworld.snapshot()
    assert after["digests"] == before["digests"], "a plan reached a vault before approval"
    assert after["heads"] == before["heads"]
    assert after["queue"] == {v1["id"]: "superseded", v2["id"]: "pending"}  # the plans exist only in the queue
    assert (v1["source"], v1["rel_path"], v2["rel_path"]) == ("claude", "plans/mathx-v1.md", "plans/mathx-v2.md")
    assert v2["superseded"] == v1["id"]
    assert not list((mworld.alpha / "plans").glob("*.md"))


def test_an_invalid_plan_returns_every_error_and_queues_nothing(mcp, mworld) -> None:
    plan_world(mworld)
    fm = good()
    fm["source"] = "human"
    fm["tasks"][0]["max_iterations"] = 11
    fm["decisions"][0]["category"] = "storage"

    async def go():
        async with mcp:
            bad = await mcp.ok("submit_plan", {"slug": "mathx", "frontmatter": fm, "body": BODY})
            forced = await mcp.refused("submit_plan", {"slug": "mathx", "frontmatter": good(), "body": BODY,
                                                       "source": "human"})
            return bad, forced

    bad, forced = run(go())
    assert bad["status"] == "invalid" and "Nothing was queued" in bad["message"]
    assert [e.split(":", 1)[0] for e in bad["errors"]] == ["frontmatter.source", "decisions[1].category",
                                                         "tasks[t1].max_iterations"]
    assert "source" in forced
    assert queue.statuses() == {}


def test_a_rejection_reaches_claude_and_the_revision_supersedes(mcp, mworld) -> None:
    plan_world(mworld)

    async def go():
        async with mcp:
            v1 = await mcp.ok("submit_plan", {"slug": "mathx", "frontmatter": good(), "body": BODY})
            reject(v1["id"], "make the CLI task protect a CLI test I'll write")  # what /reject calls
            status = await mcp.ok("plan_status", {"plan_ref": "mathx"})
            listed = await mcp.ok("list_plans")
            v2 = await mcp.ok("revise_plan", {"plan_ref": v1["id"], "frontmatter": good(), "body": BODY})
            after = await mcp.ok("plan_status", {"plan_ref": v1["id"]})
            unknown = await mcp.refused("plan_status", {"plan_ref": "nope"})
            return v1, status, listed, v2, after, unknown

    v1, status, listed, v2, after, unknown = run(go())
    assert status["status"] == "rejected"
    assert status["rejection_reason"] == "make the CLI task protect a CLI test I'll write"
    assert listed["rejected"][0]["rejection_reason"] == status["rejection_reason"]
    assert v2["version"] == 2 and "superseded" not in v2  # a rejected v1 stays rejected
    assert after["rejection_reason"] == status["rejection_reason"] and after["latest_version"] == 2
    assert [c["status"] for c in after["chain"]] == ["rejected", "pending"]
    assert "no plan 'nope'" in unknown


def test_find_precedent_tells_claude_to_mark_new_not_to_use_ask_user(mcp) -> None:
    """Found live: Claude was told to "ask with ask_user and stop", a tool it doesn't have."""
    async def go():
        async with mcp:
            return await mcp.ok("find_precedent", {"topic": "argparse subcommands"})

    result = run(go())
    assert result["precedents"] == []
    assert "ask_user" not in result["note"] and 'basis "new"' in result["note"]
    assert "ask_user" in toolset.NO_PRECEDENT["agent"]  # the DeepSeek agent keeps its own advice
