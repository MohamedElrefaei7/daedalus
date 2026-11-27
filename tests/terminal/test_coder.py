"""Commit 10b in the agent and the REPL: the coder profile, /wt, /code, and leaving coder mode.

The sandboxed run is faked here (the live sandbox has its own tests); everything
else is real: the worktree is a real git worktree of the world's alpha repo.
"""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import pytest
from core_conftest import git, head
from fakes import reply_chunks, usage
from rich.console import Console

from daedalus import toolset
from daedalus.core import worktree
from daedalus.core.state import load_session
from daedalus.sandbox.runner import RunResult
from daedalus.terminal.agent import PROFILES, TOOLSETS
from daedalus.terminal.config import Config
from daedalus.terminal.repl import Repl

CODER = ("get_scope", "list_notes", "read_note", "search", "backlinks", "list_files", "read_code", "propose_note",
         "edit_code", "write_code", "delete_code", "run", "find_precedent", "ask_user")
CHAT = ("get_scope", "list_notes", "read_note", "search", "backlinks", "list_files", "read_code", "propose_note",
        "find_precedent", "propose_edit", "propose_lesson", "ask_user")
MCP = ("get_scope", "list_notes", "read_note", "search", "backlinks", "list_files", "read_code", "propose_note",
       "find_precedent")
MATHX = "def add(a, b):\n    return a + b\n"
TEST_MATHX = "from mathx import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"


def calls(*items):
    return reply_chunks(reasoning="Thinking.", use=usage(prompt_miss=2000, completion=30),
                        tool_calls=[{"name": n, "args": a, "id": f"c{i}"} for i, (n, a) in enumerate(items)])


class Script:
    """Feeds lines to the REPL and records every prompt it showed."""

    def __init__(self, *lines: str) -> None:
        self.lines, self.prompts = list(lines), []

    def __call__(self, prompt: str = "") -> str:
        self.prompts.append(prompt)
        if not self.lines:
            raise EOFError
        return self.lines.pop(0)


@pytest.fixture(autouse=True)
def wt_root(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "worktrees"
    monkeypatch.setattr(worktree, "WORKTREE_ROOT", root)
    return root


@pytest.fixture
def runs(monkeypatch) -> list:
    """Fake the sandboxed runner: record each call, answer like pytest."""
    seen: list = []

    def fake_run(session, argv, wt, **_kw):
        seen.append((list(argv), Path(wt)))
        return RunResult(0, "..\n2 passed in 0.01s\n", "", 0.42, False)

    monkeypatch.setattr(toolset.runner, "run", fake_run)
    return seen


def make_repl(w, fake):
    state = w.root / "state.json"
    state.write_text(json.dumps({"active": "alpha", "pulled": []}))
    buf = io.StringIO()
    out = Console(file=buf, width=200, force_terminal=False, color_system=None)
    return Repl(load_session(w.registry_path, state), Config(), fake.client(), out, registry_path=w.registry_path,
                state_path=state), buf


# 7. Tool sets ---------------------------------------------------------------------------------

def test_the_coder_profile_has_exactly_its_tools_and_no_source() -> None:
    names = tuple(t["function"]["name"] for t in TOOLSETS["coder"])
    assert names == CODER == PROFILES["coder"] == toolset.CODER_NAMES
    for t in TOOLSETS["coder"]:
        params = t["function"]["parameters"]
        assert "source" not in params["properties"] and params["additionalProperties"] is False


def test_the_chat_profile_and_daedalus_mcp_are_unchanged() -> None:
    assert tuple(t["function"]["name"] for t in TOOLSETS["chat"]) == CHAT == toolset.AGENT_NAMES
    assert toolset.NAMES == MCP
    assert not set(toolset.CODE_TOOLS) & set(toolset.NAMES), "Claude never writes code"


# The REPL: /wt and /code --------------------------------------------------------------------

def test_coder_mode_end_to_end(gworld, fake, runs) -> None:
    repl, buf = make_repl(gworld, fake)
    base = head(gworld.alpha_repo)
    fake.responses += [
        calls(("write_code", {"rel_path": "mathx.py", "content": MATHX}),
              ("write_code", {"rel_path": "tests/test_mathx.py", "content": TEST_MATHX})),
        calls(("run", {"argv": ["pytest", "-q"]})),
        reply_chunks("Added add() with a test; pytest passes."),
        reply_chunks("Add mathx.add with a test"),  # the one-shot commit message
    ]
    script = Script("/wt new hello", "/code hello", "add an add function with a test, then run pytest", "/code off",
                    "y", "", "/wt list", "/wt diff hello", "/wt merge hello", "/wt drop hello")
    repl.run(script)
    out = buf.getvalue()

    rec_path = Path(out.split("created ", 1)[1].split("\n", 1)[0])
    assert "Next: /code hello" in out and "Coding alpha on agent/hello" in out
    # The prompt shows coder mode while it's on, and goes back after /code off.
    assert script.prompts[:3] == ["› ", "› ", "[coding alpha · agent/hello] › "]
    assert script.prompts[script.prompts.index("commit them to agent/hello? [Y/n] ") + 2] == "› "

    bodies = fake.bodies()
    assert [t["function"]["name"] for t in bodies[0]["tools"]] == list(CODER)
    assert bodies[0]["messages"][0]["content"].startswith("You are the Daedalus coding agent.")
    assert "tools" not in bodies[3], "the commit message comes from a tool-less one-shot call"
    assert runs == [(["pytest", "-q"], rec_path)], "run used the session's worktree"
    assert "→ run pytest -q  · sandboxed · exit 0 in 0.42 s" in out and "│ 2 passed in 0.01s" in out

    assert "message [Add mathx.add with a test]: " in script.prompts
    assert "committed " in out and "on agent/hello: Add mathx.add with a test" in out
    assert "merged agent/hello into" in out and "(--no-ff)" in out
    assert f"dropped {rec_path} and agent/hello" in out
    assert (gworld.alpha_repo / "mathx.py").read_text() == MATHX
    log = git(gworld.alpha_repo, "log", "--format=%s", f"{base}..HEAD").splitlines()
    assert log == ["merge: agent/hello", "Add mathx.add with a test"]
    assert not rec_path.exists() and git(gworld.alpha_repo, "status", "--porcelain") == ""


def test_declining_the_commit_leaves_the_changes_in_the_worktree(gworld, fake, runs) -> None:
    repl, buf = make_repl(gworld, fake)
    fake.responses += [calls(("write_code", {"rel_path": "wip.py", "content": "x = 1\n"})), reply_chunks("ok")]
    repl.run(Script("/wt new hello", "/code hello", "start something", "/code off", "n", "/wt drop hello"))
    out = buf.getvalue()
    assert "Left them uncommitted in the worktree; /code hello picks them up again." in out
    assert "the hello worktree has uncommitted changes" in out, "drop refuses to lose them"
    assert (Path(worktree.get("alpha", "hello").path) / "wip.py").read_text() == "x = 1\n"


def test_vault_while_coding_leaves_coder_mode_first(gworld, fake, runs) -> None:
    repl, buf = make_repl(gworld, fake)
    fake.responses += [calls(("write_code", {"rel_path": "a.py", "content": "a = 1\n"})), reply_chunks("ok"),
                       reply_chunks("Add a")]
    repl.run(Script("/wt new hello", "/code hello", "make a", "/vault beta", "y", ""))
    out = buf.getvalue()
    assert "committed " in out and "on agent/hello: Add a" in out
    assert out.index("on agent/hello: Add a") < out.index("Switched to beta")
    assert repl.coding is None and repl.session.worktree is None and repl.agent.profile == "chat"


def test_end_while_coding_commits_then_proposes_the_context_entry(gworld, fake, runs) -> None:
    repl, buf = make_repl(gworld, fake)
    context = (gworld.alpha / "CONTEXT.md").read_text()
    new_context = context.rstrip("\n") + "\n\n### 2026-11-26 — coded add\n\nAdded add().\n"
    fake.responses += [
        calls(("write_code", {"rel_path": "a.py", "content": "a = 1\n"})), reply_chunks("ok"),
        reply_chunks("Add a"),
        calls(("propose_edit", {"rel_path": "CONTEXT.md", "content": new_context, "summary": "session log: add"})),
        reply_chunks("Proposed the log entry."),
    ]
    repl.run(Script("/wt new hello", "/code hello", "make a", "/end", "y", "", "s"))
    out = buf.getvalue()
    assert "on agent/hello: Add a" in out
    end_request = fake.bodies()[3]
    assert "propose_edit" in [t["function"]["name"] for t in end_request["tools"]], "a chat agent writes the entry"
    asked = end_request["messages"][-1]["content"]
    assert "This session included coder mode" in asked and "agent/hello" in asked and "create a.py" in asked
    assert "Add a" in asked
    assert "[1/1]" in out and "CONTEXT.md" in out


def test_wt_refusals_and_usage(gworld, fake) -> None:
    repl, buf = make_repl(gworld, fake)
    repl.run(Script("/wt", "/wt new ../x", "/code nosuch", "/code off", "/wt new hello", "/code hello",
                    "/wt drop hello", "/code hello"))
    out = buf.getvalue()
    assert "usage: /wt new <slug>" in out
    assert "must follow the project name rules" in out
    assert "no worktree 'nosuch' for alpha" in out
    assert "you're not in coder mode" in out
    assert "you're coding in hello; /code off first" in out
    assert "already coding in hello" in out


def test_run_is_logged_as_metadata_only(gworld, fake, runs, caplog) -> None:
    repl, _ = make_repl(gworld, fake)
    fake.responses += [calls(("run", {"argv": ["pytest", "-q", "-k", "secret_name"]})), reply_chunks("ran")]
    with caplog.at_level(logging.INFO, logger="daedalus.code"):
        repl.run(Script("/wt new hello", "/code hello", "run the tests"))
    lines = [r.getMessage() for r in caplog.records if r.name == "daedalus.code"]
    assert lines == ["run pytest (3 args): exit 0, 0.42 s, timed out False, stdout 21 chars, stderr 0 chars"]
    assert not any("passed" in line or "secret_name" in line for line in lines)


# 12. The commit-message call goes through the ledger and the caps ---------------------------

def test_the_commit_message_call_is_ledgered_and_capped(gworld, fake, runs) -> None:
    from daedalus.terminal import ledger

    repl, _ = make_repl(gworld, fake)
    repl.run(Script("/wt new hello", "/code hello"))
    fake.responses += [reply_chunks("Add a thing")]
    before = len(ledger.rows(repl.session_id))
    assert repl.commit_message(["?? a.py"]) == "Add a thing"
    rows = ledger.rows(repl.session_id)
    assert len(rows) == before + 1 and rows[-1]["estimated"] == 0, "a settled ledger row for the call"

    sent = len(fake.bodies())
    repl.agent.cfg = Config(session_cap_usd=0.0001)  # below any call's worst case
    assert repl.commit_message(["?? a.py"]).startswith("agent: "), "refused: the edit log's fallback is used"
    assert len(fake.bodies()) == sent, "nothing was sent"
    assert len(ledger.rows(repl.session_id)) == before + 1, "a refused call writes no row"
