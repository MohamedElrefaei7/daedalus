"""daedalus-mcp, through FastMCP's in-memory client (and, for stdout, a real subprocess)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from conftest import TOOLS, run, write_state
from core_conftest import git, note_text, write_raw

from daedalus.core import queue, tools


def concept(body: str = "# Hello\n", **fm) -> str:
    return note_text("concept", body, **{"source": "claude", **fm})


# 1. Exact tool set -----------------------------------------------------------------------

def test_exact_tool_set(mcp) -> None:
    async def go():
        async with mcp:
            return {t.name for t in await mcp.client.list_tools()}

    assert run(go()) == TOOLS


def test_no_tool_touches_queue_state(mcp) -> None:
    async def go():
        async with mcp:
            return {t.name for t in await mcp.client.list_tools()}

    names = run(go())
    for forbidden in ("apply", "reject", "propose_edit", "propose_lesson", "write_log", "pending", "diff"):
        assert forbidden not in names


# 2. No source parameter -------------------------------------------------------------------

def test_propose_note_has_no_source_parameter_and_records_claude(mcp) -> None:
    async def go():
        async with mcp:
            tool = next(t for t in await mcp.client.list_tools() if t.name == "propose_note")
            with_fm = await mcp.ok("propose_note", {"rel_path": "inbox/a.md", "content": concept(), "summary": "a"})
            bare = await mcp.ok("propose_note", {"rel_path": "inbox/b.md", "content": "just text\n", "summary": "b"})
            return tool.input_schema, with_fm, bare

    schema, with_fm, bare = run(go())
    assert "source" not in schema["properties"]
    assert schema.get("additionalProperties") is False
    for result in (with_fm, bare):
        assert result["source"] == "claude"
        assert queue.get(result["id"]).source == "claude"
        assert queue.get(result["id"]).status == "pending"


# 3. Claude can't impersonate you ----------------------------------------------------------

def test_claude_cannot_impersonate_the_human(mcp, mworld) -> None:
    human = note_text("concept", "# Mine\n", source="human")

    async def go():
        async with mcp:
            plain = await mcp.refused("propose_note", {"rel_path": "inbox/h.md", "content": human, "summary": "s"})
            # A source argument isn't in the schema, so passing one is refused too.
            forced = await mcp.refused(
                "propose_note", {"rel_path": "inbox/h.md", "content": human, "summary": "s", "source": "human"}
            )
            return plain, forced

    plain, forced = run(go())
    assert "source" in plain and "'human'" in plain and "'claude'" in plain
    assert "source" in forced
    assert queue.statuses() == {}
    assert not (mworld.alpha / "inbox" / "h.md").exists()


# 4. Writes outside the inbox are refused --------------------------------------------------

@pytest.mark.parametrize("args, rule", [
    ({"vault": "shared", "rel_path": "practices/tabs.md", "content": note_text("practice", source="claude")}, 2),
    ({"rel_path": "../src/x.md", "content": "code in a note\n"}, 1),
], ids=["shared_practices_rule2", "into_repo_rule1"])
def test_writes_outside_inbox_refused_and_nothing_changes(mcp, mworld, args, rule) -> None:
    before = mworld.snapshot()

    async def go():
        async with mcp:
            return await mcp.refused("propose_note", {**args, "summary": "s"})

    message = run(go())
    assert message.startswith(f"Refused by rule {rule}:"), message
    after = mworld.snapshot()
    assert after["digests"] == before["digests"]
    assert after["heads"] == before["heads"]
    assert after["queue"] == {} == before["queue"]


# 5. Switching takes effect without a restart ----------------------------------------------

def test_switching_project_takes_effect_on_next_call(mcp, mworld) -> None:
    async def go():
        async with mcp:
            a = await mcp.ok("read_note", {"ref": "alpha:decisions/storage-engine.md"})
            scope_a = await mcp.ok("get_scope")
            write_state(mworld.state_path, "gamma")  # nothing restarts
            scope_b = await mcp.ok("get_scope")
            b = await mcp.ok("read_note", {"ref": "gamma:concepts/secret.md"})
            old = await mcp.refused("read_note", {"ref": "alpha:decisions/storage-engine.md"})
            old_link = await mcp.refused("read_note", {"ref": "storage-engine"})
            return a, scope_a, scope_b, b, old, old_link

    a, scope_a, scope_b, b, old, old_link = run(go())
    assert "# Storage engine" in a["body"]
    assert scope_a["active"] == "alpha" and scope_a["pulled"] == ["beta"]
    assert scope_b["active"] == "gamma" and scope_b["pulled"] == []
    assert scope_b["repo"] == str(mworld.gamma.parent)
    assert "# Not pulled" in b["body"]
    assert "'alpha' is not in this session" in old
    assert "matches no note" in old_link


# 6. Read-tool caps -----------------------------------------------------------------------

@pytest.fixture
def code(mworld):
    repo = mworld.alpha_repo
    write_raw(repo, "src/long.py", "".join(f"line {i}\n" for i in range(1, 4501)))
    write_raw(repo, "src/fetch.py", "def fetch():\n    return 1\n")
    write_raw(repo, ".gitignore", "*.log\nbuild/\n")
    write_raw(repo, "debug.log", "noise\n")
    write_raw(repo, "build/out.py", "generated\n")
    (repo / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
    git(repo, "add", "src/fetch.py", ".gitignore")
    git(repo, "commit", "-q", "-m", "code")
    return repo  # src/long.py and logo.png stay untracked


def test_read_code_caps_at_2000_lines_and_pages(mcp, code) -> None:
    async def go():
        async with mcp:
            first = await mcp.ok("read_code", {"rel_path": "src/long.py"})
            second = await mcp.ok("read_code", {"rel_path": "src/long.py", "start_line": first["next_start_line"]})
            last = await mcp.ok("read_code", {"rel_path": "src/long.py", "start_line": 4001})
            window = await mcp.ok("read_code", {"rel_path": "src/long.py", "start_line": 10, "end_line": 12})
            huge = await mcp.ok("read_code", {"rel_path": "src/long.py", "start_line": 1, "end_line": 9999})
            past = await mcp.refused("read_code", {"rel_path": "src/long.py", "start_line": 4501})
            return first, second, last, window, huge, past

    first, second, last, window, huge, past = run(go())
    assert (first["start_line"], first["end_line"], first["total_lines"]) == (1, 2000, 4500)
    assert first["text"].splitlines() == [f"line {i}" for i in range(1, 2001)]
    assert first["next_start_line"] == 2001
    assert (second["start_line"], second["end_line"], second["next_start_line"]) == (2001, 4000, 4001)
    assert second["text"].splitlines()[0] == "line 2001"
    assert (last["end_line"], last["next_start_line"]) == (4500, None)
    assert window["text"] == "line 10\nline 11\nline 12\n"
    assert huge["end_line"] == 2000, "an explicit end_line can't lift the cap"
    assert "past the end" in past


def test_read_code_character_cap(mcp, mworld) -> None:
    write_raw(mworld.alpha_repo, "min.js", ("x" * 150_000 + "\n") * 3)

    async def go():
        async with mcp:
            return await mcp.ok("read_code", {"rel_path": "min.js"})

    r = run(go())
    assert len(r["text"]) <= tools.MAX_CHARS + 100
    assert (r["end_line"], r["next_start_line"]) == (1, 2)


def test_read_code_refuses_binary_vault_and_outside(mcp, code) -> None:
    async def go():
        async with mcp:
            return (
                await mcp.refused("read_code", {"rel_path": "logo.png"}),
                await mcp.refused("read_code", {"rel_path": "vault/CONTEXT.md"}),
                await mcp.refused("read_code", {"rel_path": "../beta/vault/AGENTS.md"}),
                await mcp.refused("read_code", {"rel_path": "../../etc/passwd"}),
                await mcp.refused("read_code", {"rel_path": ".git/config"}),
                await mcp.refused("read_code", {"rel_path": "src"}),
            )

    binary, vault, pulled, outside, dotgit, folder = run(go())
    assert "binary" in binary
    assert "read_note" in vault
    assert "read_note" in pulled
    assert outside.startswith("Refused by rule 1:")
    assert dotgit.startswith("Refused by rule 4:")
    assert "folder" in folder


def test_list_files_follows_gitignore_and_skips_vault(mcp, code) -> None:
    async def go():
        async with mcp:
            return await mcp.ok("list_files"), await mcp.ok("list_files", {"subdir": "src"})

    everything, src = run(go())
    files = set(everything["files"])
    assert {"src/fetch.py", "src/long.py", ".gitignore", "logo.png"} <= files  # tracked and untracked
    assert "debug.log" not in files and "build/out.py" not in files  # .gitignore'd
    assert not any(f == "vault" or f.startswith("vault/") for f in files)
    assert everything["truncated"] is False
    assert src["files"] == ["src/fetch.py", "src/long.py"]


def test_list_files_cap(mcp, code, monkeypatch) -> None:
    monkeypatch.setattr(tools, "MAX_FILES", 2)

    async def go():
        async with mcp:
            return await mcp.ok("list_files")

    r = run(go())
    assert len(r["files"]) == 2 and r["truncated"] is True


def test_list_files_refuses_vault_and_outside(mcp, code) -> None:
    async def go():
        async with mcp:
            return await mcp.refused("list_files", {"subdir": "vault"}), await mcp.refused("list_files", {"subdir": ".."})

    vault, outside = run(go())
    assert "list_notes" in vault
    assert outside.startswith("Refused by rule 1:")


# 7. The server survives errors ------------------------------------------------------------

def test_server_survives_errors(mcp, mworld) -> None:
    async def go():
        async with mcp:
            results = []
            # PolicyError
            results.append(await mcp.refused("propose_note", {"vault": "shared", "rel_path": "practices/x.md",
                                                              "content": "x\n", "summary": "s"}))
            results.append((await mcp.ok("get_scope"))["active"])
            # A schema error: 'related' must be a list
            bad = note_text("concept", "# x\n", source="claude", related="[[not a list]]")
            results.append(await mcp.refused("propose_note", {"rel_path": "inbox/bad.md", "content": bad,
                                                              "summary": "s"}))
            results.append(len((await mcp.ok("list_notes"))["notes"]) > 0)
            # A missing state file
            mworld.state_path.unlink()
            results.append(await mcp.refused("get_scope"))
            write_state(mworld.state_path, "alpha")
            results.append((await mcp.ok("read_note", {"ref": "red-test-first"}))["vault"])
            return results

    policy, ok1, schema, ok2, state, ok3 = run(go())
    assert policy.startswith("Refused by rule 2:")
    assert ok1 == "alpha"
    assert "related" in schema and "draft refused" in schema
    assert ok2 is True
    assert "no session state yet" in state and "set-active" in state
    assert ok3 == "shared"


def test_unexpected_exception_is_a_tool_error(mcp, monkeypatch) -> None:
    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(tools, "list_notes", boom)

    async def go():
        async with mcp:
            err = await mcp.refused("list_notes")
            return err, (await mcp.ok("get_scope"))["active"]

    err, active = run(go())
    assert "internal error" in err and "kaboom" in err
    assert active == "alpha"


def test_read_note_returns_json_safe_frontmatter_and_schema_errors(mcp, mworld) -> None:
    write_raw(mworld.alpha, "concepts/dated.md", "---\ntype: concept\ncreated: 2026-11-25\n---\n# Dated\n")

    async def go():
        async with mcp:
            return await mcp.ok("read_note", {"ref": "alpha:concepts/dated.md"})

    r = run(go())
    assert r["frontmatter"]["created"] == "2026-11-25"
    assert any("status" in e for e in r["schema_errors"])


def test_search_and_backlinks(mcp) -> None:
    async def go():
        async with mcp:
            return (await mcp.ok("search", {"query": "storage", "limit": 500}),
                    await mcp.ok("backlinks", {"ref": "red-test-first"}))

    search, back = run(go())
    assert any(h["rel_path"] == "decisions/storage-engine.md" for h in search["hits"])
    assert [(b["vault"], b["rel_path"], b["via"]) for b in back["backlinks"]] == [
        ("alpha", "decisions/storage-engine.md", ["shared_refs"])
    ]


def test_propose_into_shared_inbox(mcp) -> None:
    async def go():
        async with mcp:
            return await mcp.ok("propose_note", {"vault": "shared", "rel_path": "inbox/lesson.md",
                                                 "content": note_text("lesson", source="claude"), "summary": "s"})

    r = run(go())
    assert (r["vault"], r["rel_path"], r["source"]) == ("shared", "inbox/lesson.md", "claude")


# 8. Stdout carries only protocol messages -------------------------------------------------

def _readline(proc: subprocess.Popen, timeout: float = 30.0) -> str:
    out: list[str] = []
    t = threading.Thread(target=lambda: out.append(proc.stdout.readline()), daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        proc.kill()
        pytest.fail(f"no line on stdout within {timeout}s")
    return out[0]


def test_stdout_is_only_json_rpc(mworld, tmp_path) -> None:
    exe = Path(sys.executable).parent / "daedalus-mcp"
    assert exe.exists(), "run uv sync to install the daedalus-mcp script"
    log_file = tmp_path / "mcp.log"
    proc = subprocess.Popen(
        [str(exe), "--registry", str(mworld.registry_path), "--state", str(mworld.state_path),
         "--log-file", str(log_file)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "FASTMCP_SHOW_SERVER_BANNER": "true"},
    )
    lines: list[str] = []

    def send(msg: dict) -> None:
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def until_id(want: int) -> dict:
        while True:
            line = _readline(proc)
            assert line, "server closed stdout"
            lines.append(line)
            msg = json.loads(line)  # a stray print fails here
            if msg.get("id") == want:
                return msg

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "stdout-test", "version": "0"}}})
        init = until_id(1)
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        listed = until_id(2)
        rest, _ = proc.communicate(timeout=30)  # closes stdin; the server exits at EOF
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    lines += rest.splitlines(keepends=True)

    for line in lines:
        msg = json.loads(line)
        assert msg.get("jsonrpc") == "2.0", line
    assert init["result"]["serverInfo"]["name"] == "daedalus"
    assert {t["name"] for t in listed["result"]["tools"]} == TOOLS
    assert proc.returncode == 0
    assert "daedalus-mcp starting" in log_file.read_text()


def test_refusals_are_not_logged_as_errors(mcp, monkeypatch, caplog) -> None:
    """A refusal is logged once at INFO; only a real fault is logged at ERROR."""
    async def refuse():
        async with mcp:
            await mcp.refused("propose_note", {"vault": "shared", "rel_path": "practices/x.md", "content": "x\n",
                                               "summary": "s"})
            await mcp.refused("read_note", {"ref": "nope:x.md"})

    caplog.set_level("DEBUG")
    run(refuse())
    assert [r for r in caplog.records if r.levelname == "ERROR"] == []
    assert any(r.levelname == "INFO" and "refused propose_note: Refused by rule 2" in r.getMessage() for r in caplog.records)

    caplog.clear()
    monkeypatch.setattr(tools, "list_notes", lambda *a, **k: 1 / 0)

    async def fault():
        async with mcp:
            await mcp.refused("list_notes")

    run(fault())
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert any(r.name == "daedalus.mcp" and r.exc_info for r in errors), "an internal error keeps its traceback"
