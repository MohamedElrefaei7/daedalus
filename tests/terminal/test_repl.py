"""Tests 10, 11: the key never leaks, a missing key sends nothing. Plus the REPL commands."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest
from conftest import TEST_KEY, reply_chunks, usage
from core_conftest import note_text
from rich.console import Console

from daedalus.core import queue
from daedalus.terminal import ledger
from daedalus.terminal.repl import main


def script(*lines: str):
    it = iter(lines)

    def read_line() -> str:
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    return read_line


def console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    return Console(file=buf, width=140, force_terminal=False, color_system=None), buf


def run_repl(gworld, fake, *lines: str, project: str | None = "alpha", config_text: str = "") -> tuple[int, str]:
    state = gworld.root / "state.json"
    config = gworld.root / "config.toml"
    config.write_text(config_text)
    out, buf = console()
    argv = ([project] if project else []) + ["--registry", str(gworld.registry_path), "--state", str(state),
                                             "--config", str(config)]
    code = main(argv, transport=fake.transport, read_line=script(*lines), console=out)
    return code, buf.getvalue()


def tool_reply(name, args, call_id="c1", reasoning="Let me look."):
    return reply_chunks(reasoning=reasoning, tool_calls=[{"name": name, "args": args, "id": call_id}],
                        use=usage(prompt_hit=0, prompt_miss=3000, completion=40, reasoning=12))


# 11. A missing key sends nothing -----------------------------------------------------------

def test_missing_key_sends_nothing(gworld, fake) -> None:
    fake.responses.append(reply_chunks("never"))
    code, out = run_repl(gworld, fake, "hello")
    assert code == 1
    assert fake.requests == []
    assert "python -m daedalus.secrets set deepseek" in out
    assert ledger.rows() == []


# 10. The key never leaks ---------------------------------------------------------------------

def all_files(*roots: Path):
    for root in roots:
        for dirpath, _, filenames in os.walk(root):
            for name in filenames:
                yield Path(dirpath, name)


def test_key_never_leaks(gworld, fake, memory_keyring, state_root, monkeypatch) -> None:
    memory_keyring.set_password("daedalus", "deepseek", TEST_KEY)
    draft = note_text("concept", "# DeepSeek hello\n", source="deepseek", shared_refs=["[[red-test-first]]"])
    fake.responses += [
        tool_reply("search", {"query": "failing test"}),
        tool_reply("read_note", {"ref": "red-test-first"}, call_id="c2"),
        reply_chunks("Red test first: break the code once and watch the test fail.", reasoning="Answer now.",
                     use=usage(prompt_hit=2500, prompt_miss=700, completion=30, reasoning=8)),
        tool_reply("propose_note", {"rel_path": "inbox/deepseek-hello.md", "content": draft, "summary": "hello"},
                   call_id="c3"),
        reply_chunks("Proposed."),
    ]
    code, out = run_repl(gworld, fake, "What does red-test-first say?", "propose a note", "/pending", "/cost",
                         "/quit")
    assert code == 0
    (pid,) = queue.statuses()
    assert queue.get(pid).source == "deepseek"
    assert len(fake.bodies()) == 5 and any(r.url.path == "/user/balance" for r in fake.requests)
    assert all(r.headers["authorization"] == f"Bearer {TEST_KEY}" for r in fake.requests)  # it was really used

    written = list(all_files(state_root, gworld.root))
    names = {p.name for p in written}
    assert {"ledger.sqlite", "queue.sqlite", "repl.log", "state.json"} <= names
    for p in written:
        assert TEST_KEY.encode() not in p.read_bytes(), f"the key leaked into {p}"
    assert TEST_KEY not in out


def test_key_never_leaks_into_mcp_log(gworld, state_root, monkeypatch) -> None:
    """daedalus-mcp never sees the key at all; prove its log doesn't either, with the key in the environment."""
    import subprocess
    import sys

    monkeypatch.setenv("DEEPSEEK_API_KEY", TEST_KEY)
    state = gworld.root / "state.json"
    state.write_text(json.dumps({"active": "alpha"}))
    log = state_root / "mcp.log"
    exe = Path(sys.executable).parent / "daedalus-mcp"
    proc = subprocess.run([str(exe), "--registry", str(gworld.registry_path), "--state", str(state),
                           "--log-file", str(log)], input=json.dumps({
                               "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                                   "protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "t", "version": "0"}}}) + "\n",
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0
    assert log.exists() and TEST_KEY not in log.read_text()


# The REPL -----------------------------------------------------------------------------------

@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", TEST_KEY)


def test_project_argument_sets_state(gworld, fake, keyed) -> None:
    code, out = run_repl(gworld, fake, "/quit", project="gamma")
    assert code == 0
    assert json.loads((gworld.root / "state.json").read_text())["active"] == "gamma"
    assert "daedalus · gamma" in out


def test_unknown_project_is_refused(gworld, fake, keyed) -> None:
    code, out = run_repl(gworld, fake, "/quit", project="nope")
    assert code == 1 and "not registered" in out and fake.requests == []


def test_bad_config_is_refused(gworld, fake, keyed) -> None:
    code, out = run_repl(gworld, fake, "/quit", config_text="dailly_cap_usd = 100\n")
    assert code == 1 and "unknown key" in out and fake.requests == []


def test_answer_tool_lines_and_meter(gworld, fake, keyed) -> None:
    fake.responses += [
        tool_reply("read_note", {"ref": "shared:practices/red-test-first.md"}),
        reply_chunks("It says to watch the test fail.", reasoning="ok",
                     use=usage(prompt_hit=9_100, prompt_miss=3_380, completion=312, reasoning=180)),
    ]
    code, out = run_repl(gworld, fake, "what does it say?")
    assert "→ read_note shared:practices/red-test-first.md" in out
    assert "It says to watch the test fail." in out
    assert "↑ 12,480 (9,100 cached)   ↓ 312 (reasoning 180)" in out
    assert "▸ reasoning, 180 tokens (collapsed)" in out
    assert "this call" in out and " today " in out


def test_cap_message_in_repl(gworld, fake, keyed) -> None:
    fake.responses.append(reply_chunks("never"))
    code, out = run_repl(gworld, fake, "hello", config_text="session_cap_usd = 0.0001\n")
    assert code == 0 and fake.requests == []
    assert "session cap" in out and "Not sent" in out


def test_schema_errors_shown_in_terminal(gworld, fake, keyed) -> None:
    (gworld.alpha / "concepts" / "broken.md").write_text("---\ntype: concept\n---\n# Broken\n")
    fake.responses += [tool_reply("read_note", {"ref": "alpha:concepts/broken.md"}), reply_chunks("broken")]
    code, out = run_repl(gworld, fake, "read broken")
    assert "⚠ alpha:concepts/broken.md breaks the schema:" in out
    assert "missing base key 'status'" in out


def test_pending_diff_approve_reject(gworld, fake, keyed) -> None:
    from daedalus.core.tools import propose_note

    a = propose_note(gworld.session, "alpha", "inbox/a.md", "# A\n", "deepseek", "note a")
    b = propose_note(gworld.session, "alpha", "inbox/b.md", "# B\n", "deepseek", "note b")
    code, out = run_repl(gworld, fake, "/pending", f"/diff {a.id}", f"/approve {a.id}",
                         f"/reject {b.id} not needed", "/reject", "/approve nope", "/pending", "/bogus", "/help")
    assert code == 0
    assert f"{a.id}  create  alpha:inbox/a.md  (deepseek) note a" in out
    assert "+# A" in out or "# A" in out
    assert f"applied {a.id}: alpha:inbox/a.md in commit" in out
    assert f"rejected {b.id}: not needed" in out
    assert "usage: /reject <id> <reason>" in out
    assert "no pending proposals" in out
    assert "unknown command /bogus" in out
    assert queue.get(a.id).status == "applied" and queue.get(b.id).status == "rejected"
    assert (gworld.alpha / "inbox" / "a.md").read_text() == "# A\n"
    assert fake.requests == []


def test_cost_shows_session_today_and_balance(gworld, fake, keyed) -> None:
    fake.responses.append(reply_chunks("hi", use=usage(prompt_hit=10, prompt_miss=90, completion=5)))
    code, out = run_repl(gworld, fake, "hi", "/cost")
    assert "this session  1 call(s)   ↑ 100 (10 cached)   ↓ 5 (reasoning 0)" in out
    assert "today (UTC)   1 call(s)" in out
    assert "balance       4.98 USD" in out
    assert "caps          $1.00 per session, $3.00 per UTC day" in out

