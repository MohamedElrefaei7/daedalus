"""Clients only hear about tools they have (commit 11 follow-up A3).

Every fixed string a client can receive is collected: tool descriptions, the
server instructions or system-prompt preambles, the notes tools return, and the
refusal and invalid-plan texts the scenarios below trigger. Any known tool name
in a client's text must be one of that client's tools. Slash commands are
Mohamed's: in Claude's text one may appear only in a sentence that names him
("Mohamed approves it in the terminal (/plans, …)").

`run` is also an English word, so it counts as the tool only as `run` in
backticks or as run( … ).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "core"))

from conftest import write_note, write_raw  # noqa: E402
from mcp_helpers import run  # noqa: E402
from test_plans import BODY, good, plan_world  # noqa: E402

from daedalus import toolset  # noqa: E402
from daedalus.core import gate  # noqa: E402
from daedalus.terminal import agent, loop, prompts  # noqa: E402
from daedalus.terminal.repl import COMMANDS  # noqa: E402

AGENT_ONLY = set(agent.PROFILES["chat"]) | set(agent.PROFILES["coder"])
ALL_TOOLS = set(toolset.MCP_NAMES) | AGENT_ONLY | set(getattr(toolset, "RUNS_TOOLS", ()))
WORDY = {"run"}  # tool names that are also words


def named_tools(text: str) -> set[str]:
    found = set()
    for name in ALL_TOOLS:
        if name in WORDY:
            if re.search(rf"`{name}`|\b{name}\(", text):
                found.add(name)
        elif re.search(rf"(?<![\w/]){re.escape(name)}(?!\w)", text):
            found.add(name)
    return found


COMMAND_RE = re.compile(r"(?<![\w/.:<>~-])(" + "|".join(re.escape(c) for c in sorted(COMMANDS, key=len,
                                                                                         reverse=True)) + r")(?![\w-])")


def sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z\"'(\[])|\n\n", text) if s.strip()]


def leaks(client: str, texts: list[str], allowed: set[str]) -> list[str]:
    problems = []
    for text in texts:
        for name in sorted(named_tools(text) - allowed):
            problems.append(f"{client} is told about {name}, which it doesn't have: {text[:300]!r}")
        if client == "claude":
            for sentence in sentences(text):
                if COMMAND_RE.search(sentence) and "Mohamed" not in sentence:
                    problems.append(f"claude is told a slash command outside a sentence addressed to Mohamed: "
                                    f"{sentence[:300]!r}")
    return problems


# Claude ---------------------------------------------------------------------------------

def bad_plan() -> dict:
    """A plan whose every decision basis is refused a different way, plus task errors."""
    fm = good()
    fm["source"] = "human"
    fm["decisions"] = [
        {"category": "interface", "choice": "a", "basis": "[[maybe-postgres]]"},   # proposed
        {"category": "interface", "choice": "b", "basis": "[[old-way]]"},          # superseded
        {"category": "interface", "choice": "c", "basis": "[[components/cache]]"},  # not a precedent folder
        {"category": "interface", "choice": "d", "basis": "[[beta-cli]]"},          # a pulled project's
        {"category": "interface", "choice": "e", "basis": "[[nowhere]]"},           # missing
        {"category": "interface", "choice": "f", "basis": "[[cache]]"},             # ambiguous
        {"category": "interface", "choice": "g", "basis": "cache"},                 # not a wikilink
        {"category": "storage", "choice": "h", "basis": "new"},                     # unknown category
    ]
    fm["tasks"][0].update(max_iterations=99, protected=["tests/nope.py"], files=["vault/x.md"])
    return fm


def claude_texts(mcp, mworld) -> list[str]:
    plan_world(mworld)
    write_note(mworld.alpha, "decisions/old-way.md", "decision", "# Old way\n", status="superseded")
    write_raw(mworld.alpha, "inbox/existing.md", "# Existing\n")

    async def go():
        texts: list[str] = [mworld.server.instructions or ""]
        async with mcp:
            for t in await mcp.client.list_tools():
                texts.append(t.description or "")
                for prop in t.input_schema.get("properties", {}).values():
                    texts.append(prop.get("description", ""))
            texts.append((await mcp.ok("find_precedent", {"topic": "zebra unicorn"}))["note"])
            texts.append((await mcp.ok("find_precedent", {"topic": "storage"}))["note"])
            invalid = await mcp.ok("submit_plan", {"slug": "mathx", "frontmatter": bad_plan(), "body": ""})
            texts += [invalid["message"], *invalid["errors"]]
            ok = await mcp.ok("submit_plan", {"slug": "mathx", "frontmatter": good(), "body": BODY})
            texts.append(ok["message"])
            texts.append(str(await mcp.ok("plan_status", {"plan_ref": "mathx"})))
            texts.append(str(await mcp.ok("list_plans")))
            texts.append((await mcp.ok("submit_plan", {"slug": "mathx", "frontmatter": good(), "body": BODY}))
                         ["errors"][0])
            texts.append(await mcp.refused("plan_status", {"plan_ref": "nothing-here"}))
            texts.append(await mcp.refused("revise_plan", {"plan_ref": "mathx-v0", "frontmatter": good(),
                                                           "body": BODY}))
            note = "# x\n\nSee [[decisions/cache]].\n"
            texts.append((await mcp.ok("propose_note", {"rel_path": "inbox/x.md", "content": note,
                                                        "summary": "x"}))["message"])
            texts.append(await mcp.refused("propose_note", {"rel_path": "inbox/existing.md", "content": note,
                                                            "summary": "x"}))
            texts.append(await mcp.refused("propose_note", {"rel_path": "decisions/x.md", "content": note,
                                                            "summary": "x"}))
            for extra in getattr(toolset, "CLAUDE_FIXED_TEXTS", ()):
                texts.append(extra)
        return texts

    return run(go())


def test_claude_hears_only_about_its_own_tools(mcp, mworld) -> None:
    texts = claude_texts(mcp, mworld)
    assert any("Mohamed approves" in t for t in texts)  # the scenarios ran
    assert sum("basis" in t for t in texts) >= 8
    problems = leaks("claude", texts, set(toolset.MCP_NAMES))
    assert not problems, "\n".join(problems)


def test_the_check_catches_the_commit_11_bug(mcp, mworld, monkeypatch) -> None:
    """The bite: put 'ask with ask_user and stop' back in Claude's no-precedent note."""
    monkeypatch.setitem(toolset.NO_PRECEDENT, "claude", toolset.NO_PRECEDENT["agent"])
    problems = leaks("claude", claude_texts(mcp, mworld), set(toolset.MCP_NAMES))
    assert any("ask_user" in p for p in problems), problems


def test_a_slash_command_not_addressed_to_mohamed_is_caught() -> None:
    assert leaks("claude", ["It becomes precedent once accepted (by /answer or /promote)."], set())
    assert not leaks("claude", ["Ask Mohamed to run /answer q-x 1."], set())
    assert not leaks("claude", ["Notes are addressed as shared:practices/red-test-first.md or plans/<slug>-v1.md."],
                     set())


# DeepSeek -------------------------------------------------------------------------------

def deepseek_texts(profile: str) -> list[str]:
    texts = [prompts.PREAMBLES[profile], toolset.NO_PRECEDENT["agent"], agent.NOT_RUN]
    for spec in agent.TOOLSETS[profile]:
        fn = spec["function"]
        texts.append(fn["description"])
        texts += [p.get("description", "") for p in fn["parameters"].get("properties", {}).values()]
    if profile == "coder":
        texts += [v for k, v in vars(loop).items() if k.isupper() and isinstance(v, str)]
    texts.append(gate._valid_basis_hint())
    return texts


def test_each_deepseek_profile_hears_only_about_its_own_tools() -> None:
    for profile in ("chat", "coder"):
        texts = deepseek_texts(profile)
        assert len(texts) > 10
        problems = leaks(f"deepseek-{profile}", texts, set(agent.PROFILES[profile]))
        assert not problems, "\n".join(problems)


def test_every_command_the_repl_dispatches_is_listed() -> None:
    source = (Path(__file__).parents[2] / "src/daedalus/terminal/repl.py").read_text()
    dispatched = set(re.findall(r'name == "(/[a-z]+)"', source))
    for m in re.finditer(r'name in \(([^)]*)\)', source):
        dispatched |= set(re.findall(r'"(/[a-z]+)"', m.group(1)))
    assert dispatched and dispatched <= set(COMMANDS), dispatched - set(COMMANDS)
