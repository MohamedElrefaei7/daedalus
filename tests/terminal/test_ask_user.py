"""Commit 8 in the agent and the REPL: find_precedent, ask_user ending the turn, /answer, /promote."""

from __future__ import annotations

import io
import json

import yaml
from core_conftest import git, head
from fakes import reply_chunks, usage
from rich.console import Console

from daedalus.core import notes, queue
from daedalus.core.state import load_session
from daedalus.terminal.agent import NOT_RUN
from daedalus.terminal.config import Config
from daedalus.terminal.repl import Repl

QUESTION = "Should alpha store its data in SQLite or Postgres?"
ASK = {"question": QUESTION, "category": "data_source", "options": ["SQLite", "Postgres"],
       "context_refs": ["[[storage-engine]]"]}


def calls(*items):
    """One reply holding these tool calls: (name, args) pairs."""
    return reply_chunks(reasoning="Thinking.", use=usage(prompt_miss=2000, completion=30),
                        tool_calls=[{"name": n, "args": a, "id": f"c{i}"} for i, (n, a) in enumerate(items)])


def script(*lines):
    items = list(lines)

    def read(prompt: str = "") -> str:
        if not items:
            raise EOFError
        return items.pop(0)

    return read


def make_repl(w, fake):
    state = w.root / "state.json"
    state.write_text(json.dumps({"active": "alpha", "pulled": []}))
    buf = io.StringIO()
    out = Console(file=buf, width=200, force_terminal=False, color_system=None)
    return Repl(load_session(w.registry_path, state), Config(), fake.client(), out, registry_path=w.registry_path,
                state_path=state), buf


def test_ask_user_ends_the_turn_with_zero_further_requests(gworld, fake) -> None:
    """The model asks, then would keep going; no request follows the ask_user result this turn."""
    fake.responses += [
        calls(("find_precedent", {"topic": "storage database", "category": "data_source"})),
        calls(("ask_user", ASK)),
        calls(("propose_note", {"rel_path": "inbox/store.md", "content": "# I decided anyway\n", "summary": "s"})),
        reply_chunks("SQLite it is."),
    ]
    repl, buf = make_repl(gworld, fake)
    repl.run(script("Should we use SQLite or Postgres? Draft the storage component."))

    assert len(fake.bodies()) == 2, "the agent made a model call after ask_user"
    find_result = json.loads([m for m in fake.bodies()[1]["messages"] if m["role"] == "tool"][0]["content"])
    assert find_result["precedents"] == [] or all(p["status"] == "accepted" for p in find_result["precedents"])
    (pid,) = queue.statuses()
    p = queue.get(pid)
    assert (p.rel_path.startswith("questions/"), p.status, p.source) == (True, "pending", "deepseek")
    meta = yaml.safe_load(notes.split_frontmatter(p.content)[0])
    assert meta["type"] == "question" and meta["related"] == ["[[storage-engine]]"] and meta["answered"] is False
    assert not (gworld.alpha / "inbox" / "store.md").exists()
    out = buf.getvalue()
    assert f"? {QUESTION}" in out and "  1. SQLite" in out and "  2. Postgres" in out
    assert f"/answer {pid} <number>" in out
    assert repl.agent.history[-1]["role"] == "tool"  # the turn ended on the tool result


def test_calls_after_ask_user_in_the_same_reply_are_not_run(gworld, fake) -> None:
    fake.responses += [
        calls(("ask_user", ASK), ("propose_note", {"rel_path": "inbox/x.md", "content": "# x\n", "summary": "s"})),
        reply_chunks("ok"),
    ]
    repl, _ = make_repl(gworld, fake)
    repl.run(script("decide the database", "thanks"))
    assert len(fake.bodies()) == 2  # turn 1: one request; turn 2: one request
    tools = [m for m in fake.bodies()[1]["messages"] if m["role"] == "tool"]
    assert [t["tool_call_id"] for t in tools] == ["c0", "c1"]  # every call answered, so the history stays valid
    assert tools[1]["content"] == NOT_RUN
    assert [queue.get(i).rel_path.split("/")[0] for i in queue.statuses()] == ["questions"]


def test_a_refused_ask_user_does_not_end_the_turn(gworld, fake) -> None:
    fake.responses += [calls(("ask_user", {**ASK, "context_refs": ["[[CONTEXT]]"]})), calls(("ask_user", ASK)),
                       reply_chunks("never sent")]
    repl, _ = make_repl(gworld, fake)
    repl.run(script("decide the database"))
    assert len(fake.bodies()) == 2  # the refusal went back; the second ask_user ended the turn
    refusal = [m for m in fake.bodies()[1]["messages"] if m["role"] == "tool"][0]["content"]
    assert "AGENTS.md and CONTEXT.md are never link targets" in refusal


def test_answer_then_promote_from_the_repl(gworld, fake) -> None:
    fake.responses += [calls(("ask_user", ASK))]
    repl, buf = make_repl(gworld, fake)
    repl.run(script("decide the database"))
    (pid,) = queue.statuses()
    base = head(gworld.alpha_repo)

    repl.run(script(f"/answer {pid} 1"))
    out = buf.getvalue()
    assert "approved the question alpha:questions/" in out and "answered alpha:questions/" in out
    assert "accepted alpha:decisions/should-alpha-store-its-data-in-sqlite-or-postgres.md (source: human)" in out
    assert int(git(gworld.alpha_repo, "rev-list", "--count", f"{base}..HEAD")) == 2

    draft = ("---\ntype: component\nstatus: draft\ncreated: \"2026-11-26\"\n"
             "related: [\"[[decisions/should-alpha-store-its-data-in-sqlite-or-postgres]]\"]\n"
             "shared_refs: []\nproject_refs: []\nsource: deepseek\ncode: []\n---\n\n# Storage layer\n")
    fake.responses += [calls(("propose_note", {"rel_path": "inbox/storage-layer.md", "content": draft,
                                               "summary": "storage layer component"})), reply_chunks("drafted")]
    repl.run(script("now draft the component note"))
    (cid,) = [i for i in queue.statuses() if queue.get(i).rel_path == "inbox/storage-layer.md"]
    repl.run(script(f"/approve {cid}", "/promote storage-layer components", "/promote", "/answer nosuch 1"))
    out = buf.getvalue()
    assert "promoted alpha:inbox/storage-layer.md → alpha:components/storage-layer.md" in out
    assert "usage: /promote" in out and "no proposal 'nosuch'" in out
    assert (gworld.alpha / "components" / "storage-layer.md").exists()
    assert git(gworld.alpha_repo, "status", "--porcelain") == ""


def test_answer_usage(gworld, fake) -> None:
    repl, buf = make_repl(gworld, fake)
    repl.run(script("/answer", "/answer abc"))
    assert buf.getvalue().count("usage: /answer") == 2
