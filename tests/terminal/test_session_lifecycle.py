"""Commit 6 in the REPL: /vault, /pull, /drop, /vaults, /new, and /end, against a fake DeepSeek."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json

import pytest
from fakes import TEST_KEY, reply_chunks, usage
from core_conftest import git, git_init, head, note_text, write_raw
from rich.console import Console

from daedalus.core import queue
from daedalus.core.state import load_session
from daedalus.terminal.config import Config
from daedalus.terminal.repl import Repl


class Script:
    """Scripted input. An item may be a callable, run for its side effect, returning the line to read."""

    def __init__(self, *lines) -> None:
        self.lines = list(lines)
        self.prompts: list[str] = []

    def __call__(self, prompt: str = "") -> str:
        self.prompts.append(prompt)
        if not self.lines:
            raise EOFError
        item = self.lines.pop(0)
        return item() if callable(item) else item


@pytest.fixture
def world(gworld):
    state = gworld.root / "state.json"
    state.write_text(json.dumps({"active": "alpha", "pulled": []}))
    gworld.state_path = state
    write_raw(gworld.beta, "CONTEXT.md", "# CONTEXT\n\n## Log\n\nbeta log\n\n## Decisions index\n\n"
                                         "- beta decides things\n\n## Where things live\n\n- beta lives here\n")
    for repo in (gworld.beta.parent,):
        git(repo, "commit", "-q", "-am", "context")
    return gworld


def make_repl(w, fake) -> tuple[Repl, io.StringIO]:
    buf = io.StringIO()
    out = Console(file=buf, width=160, force_terminal=False, color_system=None)
    session = load_session(w.registry_path, w.state_path)
    return Repl(session, Config(), fake.client(), out, registry_path=w.registry_path, state_path=w.state_path), buf


def tool_reply(name, args, call_id="c1"):
    return reply_chunks(reasoning="Thinking.", tool_calls=[{"name": name, "args": args, "id": call_id}],
                        use=usage(prompt_miss=2000, completion=30, reasoning=5))


def state(w) -> dict:
    return json.loads(w.state_path.read_text())


# Switching ---------------------------------------------------------------------------------

def test_vault_switch_clears_history_and_keeps_the_session_id(world, fake) -> None:
    fake.responses += [tool_reply("get_scope", {}), reply_chunks("alpha it is")]
    repl, buf = make_repl(world, fake)
    sid = repl.session_id
    repl.run(Script("which project?", "/vault gamma"))
    assert state(world) == {"active": "gamma", "pulled": []}
    assert repl.agent.history == []
    assert repl.agent.session.active == "gamma" and "# Active project (gamma): AGENTS.md" in repl.agent.system
    assert repl.session_id == sid and repl.agent.session_id == sid  # the session cap carries over
    assert "Fresh conversation: history cleared" in buf.getvalue()


def test_pull_keeps_history_and_adds_the_pulled_blocks(world, fake) -> None:
    fake.responses += [reply_chunks("hello")]
    repl, buf = make_repl(world, fake)
    repl.run(Script("hi", "/pull beta"))
    assert state(world) == {"active": "alpha", "pulled": ["beta"]}
    assert [m["role"] for m in repl.agent.history] == ["user", "assistant"]
    assert "# Pulled project (beta, read-only)" in repl.agent.system and "beta decides things" in repl.agent.system
    assert "beta log" not in repl.agent.system
    repl.run(Script("/drop beta"))
    assert state(world)["pulled"] == [] and "Pulled project (beta" not in repl.agent.system
    assert len(repl.agent.history) == 2


def test_writes_into_a_pulled_project_are_refused_with_rule_3(world, fake) -> None:
    draft = note_text("concept", "# x\n", source="deepseek", shared_refs=["[[red-test-first]]"])
    fake.responses += [tool_reply("propose_note", {"vault": "beta", "rel_path": "inbox/x.md", "content": draft,
                                                   "summary": "s"}),
                       reply_chunks("refused, as expected")]
    repl, buf = make_repl(world, fake)
    repl.run(Script("/pull beta", "write into beta"))
    tool_msg = [m for m in fake.bodies()[1]["messages"] if m["role"] == "tool"][0]
    assert tool_msg["content"].startswith("Refused by rule 3:")
    assert queue.statuses() == {}


def test_mcp_follows_the_switch_without_a_restart(world, fake) -> None:
    """The plan's commit-6 red test: after /vault in the REPL, the same MCP server refuses vault-a paths."""
    from fastmcp import Client

    from daedalus.mcp_server import build_server

    server = build_server(world.registry_path, world.state_path)
    repl, _ = make_repl(world, fake)

    async def go():
        async with Client(server) as c:
            before = await c.call_tool("read_note", {"ref": "alpha:decisions/storage-engine.md"}, raise_on_error=False)
            repl.run(Script("/vault gamma"))  # the switch happens in "another terminal"
            after = await c.call_tool("read_note", {"ref": "alpha:decisions/storage-engine.md"}, raise_on_error=False)
            scope = await c.call_tool("get_scope", {})
            return before, after, scope

    before, after, scope = asyncio.run(go())
    assert not before.is_error
    assert after.is_error and "'alpha' is not in this session" in after.content[0].text
    assert scope.structured_content["active"] == "gamma"


def test_vaults_lists_and_marks(world, fake) -> None:
    repl, buf = make_repl(world, fake)
    repl.run(Script("/pull beta", "/vaults", "/vault alpha", "/vault nope"))
    out = buf.getvalue()
    assert "* alpha" in out and "(active)" in out and "+ beta" in out and "(pulled, read-only)" in out
    assert "  gamma" in out
    assert "alpha is already the active project." in out
    assert "error:" in out and "not registered" in out
    assert state(world)["active"] == "alpha"


def test_new_from_the_repl(world, fake, tmp_path) -> None:
    repo = tmp_path / "delta"
    repo.mkdir()
    git_init(repo)
    write_raw(repo, "README.md", "# delta\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-q", "-m", "first")
    repl, buf = make_repl(world, fake)
    repl.run(Script(f"/new delta --repo {repo}", "/vault delta", "/new bad"))
    out = " ".join(buf.getvalue().split())  # long temp paths wrap
    assert 'as "init: daedalus vault from template"' in out
    assert "Open folder as vault" in out and str(repo / "vault") in buf.getvalue()
    assert state(world)["active"] == "delta"
    assert "usage: /new <name> --repo <path>" in out
    assert fake.requests == []


# /end ----------------------------------------------------------------------------------------

LOG_ENTRY = "\n### 2026-11-26 — read the storage decision\n\nAsked about storage; read storage-engine.\n"


def end_script(world, *after):
    """A session with one tool call, then /end, then the review answers in ``after``."""
    context = (world.alpha / "CONTEXT.md").read_text()
    new_context = context.replace("## Log\n", "## Log\n" + LOG_ENTRY, 1)
    return context, new_context, Script("what's the storage decision?", "/end", *after)


def end_responses(new_context, lesson: dict | None = None):
    out = [tool_reply("read_note", {"ref": "storage-engine"}), reply_chunks("SQLite."),
           tool_reply("propose_edit", {"rel_path": "CONTEXT.md", "content": new_context,
                                       "summary": "session log: read the storage decision"}, call_id="e1")]
    if lesson:
        out.append(tool_reply("propose_lesson", lesson, call_id="l1"))
    out.append(reply_chunks("Proposed the log entry."))
    return out


def test_end_proposes_context_edit_with_base_hash_and_writes_nothing(world, fake) -> None:
    context, new_context, script = end_script(world, "s")
    fake.responses += end_responses(new_context)
    base_head = head(world.alpha_repo)
    repl, buf = make_repl(world, fake)
    assert repl.run(script) == 0

    (pid,) = [i for i, s in queue.statuses().items()]
    p = queue.get(pid)
    assert (p.kind, p.vault, p.rel_path, p.status, p.source) == ("edit", "alpha", "CONTEXT.md", "pending", "deepseek")
    assert p.base_sha256 == hashlib.sha256(context.encode()).hexdigest()
    assert p.content == new_context
    assert (world.alpha / "CONTEXT.md").read_text() == context, "/end wrote CONTEXT.md before approval"
    assert head(world.alpha_repo) == base_head
    out = buf.getvalue()
    assert "[1/1]" in out and "+### 2026-11-26 — read the storage decision" in out
    assert "it stays pending" in out
    # The /end request carried the current CONTEXT.md and today's date in the user message, not the system prompt.
    end_msg = [m for m in fake.bodies()[2]["messages"] if m["role"] == "user"][-1]["content"]
    assert "----- begin CONTEXT.md -----" in end_msg and context in end_msg


def test_end_approve_applies_and_commits(world, fake) -> None:
    context, new_context, script = end_script(world, "a")
    fake.responses += end_responses(new_context)
    repl, buf = make_repl(world, fake)
    repl.run(script)
    assert (world.alpha / "CONTEXT.md").read_text() == new_context
    assert git(world.alpha_repo, "log", "-1", "--format=%s").strip() == \
        "agent(deepseek): session log: read the storage decision"
    assert "applied " in buf.getvalue()


def test_end_approval_refuses_as_stale_after_a_human_edit(world, fake) -> None:
    def human_edits_then_approves():
        write_raw(world.alpha, "CONTEXT.md", (world.alpha / "CONTEXT.md").read_text() + "\nmy own line\n")
        return "a"

    context, new_context, script = end_script(world, human_edits_then_approves)
    fake.responses += end_responses(new_context)
    repl, buf = make_repl(world, fake)
    repl.run(script)
    (pid,) = queue.statuses()
    assert queue.get(pid).status == "stale"
    assert (world.alpha / "CONTEXT.md").read_text().endswith("\nmy own line\n")
    assert "error:" in buf.getvalue()


def test_end_lesson_lands_in_the_shared_inbox_queue(world, fake) -> None:
    lesson = {"rel_path": "hooks-path.md", "summary": "lesson: hooks path",
              "content": note_text("lesson", "# Hooks path\n\nWhat failed, why, the fix.\n", source="deepseek",
                                   applies_to=["git"], related=["[[red-test-first]]"])}
    context, new_context, script = end_script(world, "s", "r", "", "not a real lesson")
    fake.responses += end_responses(new_context, lesson)
    repl, buf = make_repl(world, fake)
    repl.run(script)
    lessons = [queue.get(i) for i in queue.statuses() if queue.get(i).vault == "shared"]
    assert [(p.rel_path, p.status, p.status_reason) for p in lessons] == [
        ("inbox/hooks-path.md", "rejected", "not a real lesson")]
    assert not (world.shared / "practices" / "hooks-path.md").exists()
    assert not (world.shared / "inbox" / "hooks-path.md").exists()


def test_lesson_tool_cannot_reach_practices(world, fake) -> None:
    lesson = note_text("lesson", "# L\n", source="deepseek", applies_to=["git"], related=["[[red-test-first]]"])
    fake.responses += [tool_reply("propose_lesson", {"rel_path": "../practices/l.md", "content": lesson,
                                                     "summary": "s"}),
                       reply_chunks("refused")]
    repl, _ = make_repl(world, fake)
    repl.run(Script("propose a lesson into practices"))
    tool_msg = [m for m in fake.bodies()[1]["messages"] if m["role"] == "tool"][0]
    assert tool_msg["content"].startswith("Refused by rule 2:")
    assert queue.statuses() == {}


def test_end_with_nothing_to_log_just_exits(world, fake) -> None:
    fake.responses += [reply_chunks("hi")]
    repl, buf = make_repl(world, fake)
    assert repl.run(Script("hello", "/end", "never read")) == 0
    assert len(fake.bodies()) == 1  # no summary request
    assert "Nothing happened in this session that needs a log entry" in buf.getvalue()
    assert queue.statuses() == {}
