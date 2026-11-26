"""Tests 6, 8, 9: reasoning is passed back, the exact tool set, the tool-round limit.
Plus refusals as tool results, result caps, schema errors surfaced, source fixed to deepseek."""

from __future__ import annotations

import asyncio
import json

import pytest
from conftest import reply_chunks, usage
from core_conftest import note_text, write_raw

from daedalus import toolset
from daedalus.core import queue
from daedalus.terminal import agent as agent_mod
from daedalus.terminal.agent import MAX_RESULT_CHARS, MAX_TOOL_ROUNDS, TOOLS, Agent, Events

EIGHT = {"get_scope", "list_notes", "read_note", "search", "backlinks", "list_files", "read_code", "propose_note"}


class Recorder(Events):
    def __init__(self) -> None:
        self.tools: list = []
        self.schema: list = []
        self.stops: list[str] = []

    def tool(self, outcome) -> None:
        self.tools.append(outcome)

    def schema_errors(self, ref, errors) -> None:
        self.schema.append((ref, errors))

    def stopped(self, message) -> None:
        self.stops.append(message)


@pytest.fixture
def agent(gworld, fake, cfg) -> Agent:
    return Agent(gworld.session, fake.client(), cfg, "s1")


def tool_reply(name: str, args: dict, reasoning: str = "", call_id: str = "c1") -> list[dict]:
    return reply_chunks(reasoning=reasoning, tool_calls=[{"name": name, "args": args, "id": call_id}],
                        use=usage(completion=20, reasoning=5))


# 6. Reasoning is passed back ---------------------------------------------------------------

def test_reasoning_content_is_passed_back(agent, fake) -> None:
    fake.responses += [
        tool_reply("read_note", {"ref": "red-test-first"}, reasoning="I should read the practice first."),
        reply_chunks("It says: test first.", reasoning="Now I can answer."),
        tool_reply("backlinks", {"ref": "red-test-first"}, reasoning="Second turn: who links to it?", call_id="c2"),
        reply_chunks("storage-engine links to it.", reasoning="Done."),
    ]
    agent.turn("What does red-test-first say?")
    agent.turn("Which note links to it?")

    bodies = fake.bodies()
    assert len(bodies) == 4
    second = bodies[1]["messages"]
    assistants = [m for m in second if m["role"] == "assistant"]
    assert [m["reasoning_content"] for m in assistants] == ["I should read the practice first."]
    assert assistants[0]["tool_calls"][0]["function"]["name"] == "read_note"
    fourth = [m for m in bodies[3]["messages"] if m["role"] == "assistant"]
    assert [m["reasoning_content"] for m in fourth] == [
        "I should read the practice first.", "Now I can answer.", "Second turn: who links to it?"]
    for body in bodies:  # every request carries tools, so every earlier assistant message must carry reasoning
        assert body["tools"]
        assert all("reasoning_content" in m for m in body["messages"] if m["role"] == "assistant")


def test_tool_results_follow_their_calls(agent, fake) -> None:
    fake.responses += [tool_reply("get_scope", {}, call_id="scope-1"), reply_chunks("alpha")]
    agent.turn("scope?")
    msgs = fake.bodies()[1]["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "tool"]
    assert msgs[3]["tool_call_id"] == "scope-1"
    assert json.loads(msgs[3]["content"])["active"] == "alpha"


# 8. Exact agent tool set ---------------------------------------------------------------------

def test_exact_agent_tool_set_and_no_source_parameter() -> None:
    names = [t["function"]["name"] for t in TOOLS]
    assert set(names) == EIGHT and len(names) == 8
    for t in TOOLS:
        params = t["function"]["parameters"]
        assert "source" not in params["properties"], t["function"]["name"]
        assert params["additionalProperties"] is False


def test_agent_parameters_match_daedalus_mcp(mcp_schemas) -> None:
    ours = {t["function"]["name"]: set(t["function"]["parameters"]["properties"]) for t in TOOLS}
    assert ours == mcp_schemas


@pytest.fixture
def mcp_schemas(gworld, tmp_path):
    from fastmcp import Client

    from daedalus.mcp_server import build_server

    state = tmp_path / "state.json"
    state.write_text('{"active": "alpha"}')

    async def go():
        async with Client(build_server(gworld.registry_path, state)) as c:
            return {t.name: set(t.input_schema["properties"]) for t in await c.list_tools()}

    return asyncio.run(go())


def test_source_is_deepseek_and_model_cannot_set_it(agent, fake, gworld) -> None:
    draft = note_text("concept", "# Hi\n", source="deepseek", shared_refs=["[[red-test-first]]"])
    fake.responses += [
        tool_reply("propose_note", {"rel_path": "inbox/hi.md", "content": draft, "summary": "hi", "source": "human"}),
        tool_reply("propose_note", {"rel_path": "inbox/hi.md", "content": draft, "summary": "hi"}, call_id="c2"),
        reply_chunks("queued"),
    ]
    rec = Recorder()
    agent.turn("propose", rec)
    forced, ok = rec.tools
    assert not forced.ok and "unknown argument(s) source" in forced.text
    assert ok.ok
    (pid,) = queue.statuses()
    assert queue.get(pid).source == "deepseek"


def test_human_source_in_draft_is_refused(agent, fake) -> None:
    fake.responses += [tool_reply("propose_note", {"rel_path": "inbox/h.md", "summary": "s",
                                                   "content": note_text("concept", "# x\n", source="human")}),
                       reply_chunks("ok")]
    rec = Recorder()
    agent.turn("x", rec)
    assert not rec.tools[0].ok and "'deepseek'" in rec.tools[0].text
    assert queue.statuses() == {}


# 9. The tool-round limit holds ---------------------------------------------------------------

def test_tool_rounds_stop_at_twelve(agent, fake) -> None:
    fake.responses.append(tool_reply("get_scope", {}))
    fake.repeat_last = True  # a model that asks for tools forever
    rec = Recorder()
    result = agent.turn("loop forever", rec)
    assert result.stopped and result.rounds == MAX_TOOL_ROUNDS == 12
    assert len(fake.bodies()) == 12
    assert len(rec.tools) == 12
    assert rec.stops and "12 tool rounds" in rec.stops[0]


# Tool errors and caps ------------------------------------------------------------------------

def test_refusal_goes_back_to_the_model_naming_the_rule(agent, fake) -> None:
    fake.responses += [tool_reply("propose_note", {"vault": "shared", "rel_path": "practices/tabs.md",
                                                   "content": "x\n", "summary": "s"}),
                       reply_chunks("I can't write there; I can propose into shared inbox/ instead.")]
    rec = Recorder()
    result = agent.turn("add a practice", rec)
    assert rec.tools[0].text.startswith("Refused by rule 2:")
    tool_msg = [m for m in fake.bodies()[1]["messages"] if m["role"] == "tool"][0]
    assert tool_msg["content"].startswith("Refused by rule 2:")
    assert "inbox" in result.answer


def test_bad_arguments_go_back_too(agent, fake) -> None:
    fake.responses += [reply_chunks(tool_calls=[{"name": "read_note", "args": {}, "id": "c1"}]),
                       reply_chunks(tool_calls=[{"name": "write_log", "args": {}, "id": "c2"}]),
                       reply_chunks("gave up")]
    rec = Recorder()
    agent.turn("x", rec)
    assert "missing ref" in rec.tools[0].text
    assert "Unknown tool 'write_log'" in rec.tools[1].text


def test_tool_results_capped_at_60000_chars(agent, fake, gworld) -> None:
    write_raw(gworld.alpha_repo, "big.txt", ("y" * 99 + "\n") * 2000)  # 200,000 chars: read_code's own cap
    fake.responses += [tool_reply("read_code", {"rel_path": "big.txt"}), reply_chunks("big")]
    rec = Recorder()
    agent.turn("read it", rec)
    text = rec.tools[0].text
    assert len(text) > MAX_RESULT_CHARS and text.startswith('{"path"')
    assert "[truncated: showing 60,000 of" in text
    assert len(text) < MAX_RESULT_CHARS + 300


def test_schema_errors_are_surfaced_to_the_user(agent, fake, gworld) -> None:
    write_raw(gworld.alpha, "concepts/broken.md", "---\ntype: concept\n---\n# Broken\n")
    fake.responses += [tool_reply("read_note", {"ref": "alpha:concepts/broken.md"}), reply_chunks("it's broken")]
    rec = Recorder()
    agent.turn("read broken", rec)
    ((ref, errors),) = rec.schema
    assert ref == "alpha:concepts/broken.md"
    assert any("status" in e for e in errors)


def test_failed_turn_is_rolled_back(agent, fake) -> None:
    from daedalus.terminal.deepseek import DeepSeekError
    import httpx2

    fake.responses += [tool_reply("get_scope", {}), httpx2.Response(400, json={"error": {"message": "bad"}})]
    with pytest.raises(DeepSeekError):
        agent.turn("x")
    assert agent.history == []


def test_tool_label() -> None:
    assert agent_mod.tool_label("read_note", {"ref": "shared:practices/red-test-first.md"}) == \
        "→ read_note shared:practices/red-test-first.md"
    assert agent_mod.tool_label("get_scope", {}) == "→ get_scope"
    assert agent_mod.tool_label("propose_note", {"rel_path": "inbox/x.md"}) == "→ propose_note inbox/x.md"
    assert set(toolset.NAMES) == EIGHT
