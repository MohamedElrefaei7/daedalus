"""Commit 7: the Obsidian bridge. /open shows a note by path; /here brings Obsidian's open note in.

The CLI is faked by an executable `obsidian` first on PATH that logs its cwd and
argv and prints what the test sets. "Is Obsidian running" is an injected
function; pgrep is never involved, and the conftest makes the real app
"not running" for every other test.
"""

from __future__ import annotations

import io
import json
import os
import urllib.parse
from pathlib import Path

import pytest
from core_conftest import write_note, write_raw
from fakes import reply_chunks
from rich.console import Console

from daedalus.core.tools import AmbiguousRefError, RefError
from daedalus.core.policy import PolicyError
from daedalus.terminal import obsidian
from daedalus.terminal.config import Config
from daedalus.terminal.repl import Repl

FAKE_CLI = """#!/bin/sh
printf '%s\\n' "$PWD" >> "$FAKE_OBSIDIAN_LOG"
for arg in "$@"; do printf '%s\\n' "$arg" >> "$FAKE_OBSIDIAN_LOG"; done
printf -- '---\\n' >> "$FAKE_OBSIDIAN_LOG"
cat "$FAKE_OBSIDIAN_OUT"
exit "${FAKE_OBSIDIAN_EXIT:-0}"
"""


class FakeCli:
    def __init__(self, root: Path, monkeypatch) -> None:
        self.bin = root / "fake-bin"
        self.bin.mkdir()
        exe = self.bin / "obsidian"
        exe.write_text(FAKE_CLI)
        exe.chmod(0o755)
        self.log = root / "obsidian.log"
        self.out = root / "obsidian.out"
        self.out.write_text("")
        monkeypatch.setenv("FAKE_OBSIDIAN_LOG", str(self.log))
        monkeypatch.setenv("FAKE_OBSIDIAN_OUT", str(self.out))
        monkeypatch.setenv("PATH", f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}")

    def says(self, text: str) -> None:
        self.out.write_text(text)

    def reports(self, base, file) -> None:
        # The real CLI's format (1.15.1): "=> " then the eval's value, exit 0.
        self.says("=> " + json.dumps({"base": str(base) if base is not None else None, "file": file}) + "\n")

    @property
    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [c.strip("\n").split("\n") for c in self.log.read_text().split("---\n") if c.strip()]


@pytest.fixture
def cli(tmp_path, monkeypatch) -> FakeCli:
    return FakeCli(tmp_path, monkeypatch)


def running(value=True) -> obsidian.Bridge:
    opened: list[list[str]] = []
    bridge = obsidian.Bridge(running=lambda: value, opener=opened.append)
    bridge.opened = opened  # type: ignore[attr-defined]
    return bridge


# 1. Closed app: no launch ----------------------------------------------------------------

def test_closed_obsidian_is_not_open_and_the_cli_is_never_run(gworld, cli) -> None:
    cli.reports(gworld.alpha, "components/usgs-fetcher.md")
    result = obsidian.active_note(gworld.session, running(False))
    assert isinstance(result, obsidian.NotOpen)
    assert "Obsidian isn't open" in result.message
    assert cli.calls == [], "the CLI ran while Obsidian was closed: it would have launched the app"


def test_the_default_bridge_asks_pgrep_not_the_cli(gworld, cli, monkeypatch) -> None:
    asked = []
    monkeypatch.setattr(obsidian, "is_running", lambda: asked.append(1) or False)
    assert isinstance(obsidian.active_note(gworld.session), obsidian.NotOpen)
    assert asked == [1] and cli.calls == []


# 2. Missing CLI ---------------------------------------------------------------------------

def test_missing_cli_names_the_setup_steps(gworld, tmp_path, monkeypatch) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    result = obsidian.active_note(gworld.session, running())
    assert isinstance(result, obsidian.CliMissing)
    assert "Command line interface" in result.message and "obsidian version" in result.message


# 3. Untrusted output ----------------------------------------------------------------------

def bad_outputs(w):
    return [
        ("not json", lambda cli: cli.says("Command line interface is not enabled.\n"), obsidian.NotJson),
        ("empty", lambda cli: cli.says(""), obsidian.NotJson),
        ("an error, exit 0", lambda cli: cli.says("Vault not found.\n"), obsidian.NotJson),
        ("no => prefix", lambda cli: cli.says(json.dumps({"base": str(w.alpha), "file": "CONTEXT.md"})), obsidian.NotJson),
        ("not an object", lambda cli: cli.says('=> "just a string"\n'), obsidian.NotJson),
        ("extra lines", lambda cli: cli.says(f'warning\n=> {json.dumps({"base": str(w.alpha), "file": "CONTEXT.md"})}\n'),
         obsidian.NotJson),
        ("base not a dir", lambda cli: cli.reports(w.root / "no-such-vault", "a.md"), obsidian.BadBase),
        ("base is a file", lambda cli: cli.reports(w.alpha / "AGENTS.md", "a.md"), obsidian.BadBase),
        ("file null", lambda cli: cli.reports(w.alpha, None), obsidian.NoActiveFile),
        ("file missing", lambda cli: cli.reports(w.alpha, "components/gone.md"), obsidian.FileMissing),
    ]


@pytest.mark.parametrize("case", range(10))
def test_each_bad_output_has_its_own_error_and_attaches_nothing(gworld, cli, fake, case) -> None:
    label, set_up, expected = bad_outputs(gworld)[case]
    set_up(cli)
    result = obsidian.active_note(gworld.session, running())
    assert type(result) is expected, f"{label}: got {result!r}"
    assert result.message

    fake.responses += [reply_chunks("ok")]
    repl, buf = make_repl(gworld, fake, running())
    repl.run(script("/here", "hello"))
    assert repl.attached is None
    assert last_user(fake) == "hello", f"{label}: something was attached"
    assert " ".join(result.message.split()) in " ".join(buf.getvalue().split())  # long paths wrap


def test_distinct_errors_for_the_four_untrusted_cases(gworld, cli) -> None:
    kinds = set()
    for _, set_up, _ in bad_outputs(gworld):
        set_up(cli)
        kinds.add(type(obsidian.active_note(gworld.session, running())))
    assert {obsidian.NotJson, obsidian.BadBase, obsidian.NoActiveFile, obsidian.FileMissing} <= kinds


def test_cli_failure_and_timeout(gworld, cli, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_OBSIDIAN_EXIT", "3")
    cli.reports(gworld.alpha, "components/usgs-fetcher.md")
    assert isinstance(obsidian.active_note(gworld.session, running()), obsidian.CliFailed)

    import subprocess

    def slow(_cli):
        raise subprocess.TimeoutExpired("obsidian", obsidian.CLI_TIMEOUT_S)

    monkeypatch.setattr(obsidian, "ask_cli", slow)
    result = obsidian.active_note(gworld.session, running())
    assert isinstance(result, obsidian.CliFailed) and "5 s" in result.message


def test_the_cli_runs_eval_from_outside_every_vault(gworld, cli) -> None:
    cli.reports(gworld.alpha, "components/usgs-fetcher.md")
    assert isinstance(obsidian.active_note(gworld.session, running()), obsidian.Here)
    ((cwd, *argv),) = cli.calls
    assert argv == ["eval", f"code={obsidian.EVAL_CODE}"]
    vaults = [gworld.shared, gworld.alpha, gworld.beta, gworld.gamma]
    assert not any(Path(os.path.realpath(cwd)) == v or v in Path(os.path.realpath(cwd)).parents for v in vaults)
    assert "read" not in argv  # never the CLI's own read


# 4. Same-named vaults ---------------------------------------------------------------------

def test_same_named_vaults_resolve_by_real_path(gworld, cli, fake) -> None:
    assert gworld.alpha.name == gworld.beta.name == "vault"
    write_note(gworld.alpha, "components/fetcher.md", "component", "# Fetcher\n\nALPHA's fetcher.\n", code=["a.py"])
    write_note(gworld.beta, "components/fetcher.md", "component", "# Fetcher\n\nBETA's fetcher.\n", code=["b.py"])
    cli.reports(gworld.beta, "components/fetcher.md")  # the second vault named "vault"

    fake.responses += [reply_chunks("beta's")]
    repl, buf = make_repl(gworld, fake, running(), pulled=["beta"])
    repl.run(script("/here", "what does this note say?"))
    sent = last_user(fake)
    assert "beta:components/fetcher.md" in sent and "BETA's fetcher." in sent
    assert "ALPHA's fetcher." not in sent
    assert "→ here beta:components/fetcher.md" in buf.getvalue()


def test_base_given_through_a_symlink_maps_by_real_path(gworld, cli, tmp_path) -> None:
    link = tmp_path / "beta-link"
    link.symlink_to(gworld.beta)
    cli.reports(link, "concepts/quake-magnitude.md")
    result = obsidian.active_note(gworld.session, running())
    assert isinstance(result, obsidian.Here) and result.ref == "beta:concepts/quake-magnitude.md"


# 5. Out of scope, unregistered -------------------------------------------------------------

def test_registered_but_not_pulled_suggests_pull(gworld, cli, fake) -> None:
    cli.reports(gworld.gamma, "concepts/secret.md")
    result = obsidian.active_note(gworld.session, running())
    assert isinstance(result, obsidian.NotInScope) and "/pull gamma" in result.message
    fake.responses += [reply_chunks("ok")]
    repl, buf = make_repl(gworld, fake, running())
    repl.run(script("/here", "hello"))
    assert "/pull gamma" in buf.getvalue()
    assert "Not pulled" not in json.dumps(fake.bodies())


def test_unregistered_base_is_refused(gworld, cli, fake, tmp_path) -> None:
    stranger = tmp_path / "elsewhere" / "vault"
    write_raw(stranger, "note.md", "# a stranger's note\n")
    cli.reports(stranger, "note.md")
    result = obsidian.active_note(gworld.session, running())
    assert isinstance(result, obsidian.Unregistered) and "isn't registered" in result.message
    fake.responses += [reply_chunks("ok")]
    repl, _ = make_repl(gworld, fake, running())
    repl.run(script("/here", "hello"))
    assert "a stranger's note" not in json.dumps(fake.bodies())


def test_shared_vault_is_always_in_scope(gworld, cli) -> None:
    cli.reports(gworld.shared, "practices/red-test-first.md")
    result = obsidian.active_note(gworld.session, running())
    assert isinstance(result, obsidian.Here) and result.ref == "shared:practices/red-test-first.md"


# 6. The policy still applies ---------------------------------------------------------------

def test_obsidian_config_is_refused_with_rule_4(gworld, cli, fake) -> None:
    assert (gworld.alpha / ".obsidian" / "app.json").is_file()
    cli.reports(gworld.alpha, ".obsidian/app.json")
    result = obsidian.active_note(gworld.session, running())
    assert isinstance(result, obsidian.Refused) and result.message.startswith("Refused by rule 4:")


def test_a_file_escaping_the_vault_is_refused(gworld, cli) -> None:
    cli.reports(gworld.alpha, "../../gamma/vault/concepts/secret.md")
    result = obsidian.active_note(gworld.session, running())
    assert isinstance(result, obsidian.Refused) and result.message.startswith("Refused by rule 1:")


def test_a_non_markdown_file_is_not_attached(gworld, cli) -> None:
    write_raw(gworld.alpha, "inbox/board.canvas", "{}\n")
    cli.reports(gworld.alpha, "inbox/board.canvas")
    assert isinstance(obsidian.active_note(gworld.session, running()), obsidian.NotANote)


# 7. Open by path ---------------------------------------------------------------------------

def test_open_note_uses_path_never_vault(gworld) -> None:
    bridge = running(False)  # /open may launch Obsidian; it doesn't care
    opened = obsidian.open_note(gworld.session, "shared:practices/red-test-first.md", bridge)
    ((argv),) = bridge.opened
    assert argv[0] == "open" and len(argv) == 2
    uri = argv[1]
    parsed = urllib.parse.urlsplit(uri)
    query = urllib.parse.parse_qs(parsed.query)
    assert (parsed.scheme, parsed.netloc) == ("obsidian", "open")
    assert set(query) == {"path"} and "vault=" not in uri
    assert query["path"] == [str(gworld.shared / "practices" / "red-test-first.md")]
    assert opened.vault == "shared"


def test_open_note_by_wikilink_and_with_spaces(gworld) -> None:
    write_note(gworld.alpha, "concepts/two words.md", "concept", "# Two words\n")
    bridge = running()
    obsidian.open_note(gworld.session, "two words", bridge)
    uri = bridge.opened[0][1]
    assert " " not in uri and "%20" in uri
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(uri).query)["path"] == [str(gworld.alpha / "concepts/two words.md")]


def test_open_note_refusals_run_nothing(gworld) -> None:
    write_note(gworld.beta, "components/usgs-fetcher.md", "component", "# another\n", code=["x.py"])
    bridge = running()
    with pytest.raises(AmbiguousRefError):
        obsidian.open_note(gworld.session, "usgs-fetcher", bridge)
    with pytest.raises(RefError, match="not in this session"):
        obsidian.open_note(gworld.session, "gamma:concepts/secret.md", bridge)
    with pytest.raises(PolicyError) as e:
        obsidian.open_note(gworld.session, "alpha:.obsidian/app.json", bridge)
    assert e.value.rule == 4
    with pytest.raises(obsidian.ObsidianError, match="doesn't exist"):
        obsidian.open_note(gworld.session, "alpha:inbox/nothing.md", bridge)
    assert bridge.opened == []


def test_open_from_the_repl(gworld, fake) -> None:
    bridge = running()
    repl, buf = make_repl(gworld, fake, bridge)
    repl.run(script("/open storage-engine", "/open gamma:concepts/secret.md", "/open"))
    out = buf.getvalue()
    assert "opened alpha:decisions/storage-engine.md in Obsidian" in out
    assert "not in this session" in out and "usage: /open <note>" in out
    assert len(bridge.opened) == 1 and fake.requests == []


# 8. Prefix stability ------------------------------------------------------------------------

def test_here_goes_in_the_user_message_and_the_system_prompt_is_unchanged(gworld, cli, fake) -> None:
    cli.reports(gworld.alpha, "components/usgs-fetcher.md")
    fake.responses += [reply_chunks("hi"), reply_chunks("It's the USGS fetcher.")]
    repl, buf = make_repl(gworld, fake, running())
    repl.run(script("hello", "/here", "what does this note say?"))
    before, after = fake.bodies()
    assert before["messages"][0]["role"] == after["messages"][0]["role"] == "system"
    assert after["messages"][0]["content"] == before["messages"][0]["content"], "/here changed the system prompt"
    note = (gworld.alpha / "components/usgs-fetcher.md").read_text()
    sent = after["messages"][-1]
    assert sent["role"] == "user"
    assert sent["content"].startswith("alpha:components/usgs-fetcher.md (the note open in Obsidian")
    assert note in sent["content"] and sent["content"].endswith("what does this note say?")
    assert f"→ here alpha:components/usgs-fetcher.md ({len(note):,} chars)" in buf.getvalue()


# The attachment rules ------------------------------------------------------------------------

def test_a_second_here_replaces_the_first(gworld, cli, fake) -> None:
    fake.responses += [reply_chunks("ok"), reply_chunks("ok")]

    def focus(rel):
        def step():
            cli.reports(gworld.alpha, rel)
            return "/here"
        return step

    repl, buf = make_repl(gworld, fake, running())
    repl.run(script(focus("components/usgs-fetcher.md"), focus("decisions/storage-engine.md"), "which?", "again"))
    first, second = (b["messages"][-1]["content"] for b in fake.bodies())
    assert "alpha:decisions/storage-engine.md" in first and "# Storage engine" in first
    assert "usgs-fetcher.md (the note" not in first and first.count("```") == 2
    assert second == "again", "the attachment went with more than one message"
    assert "(replaces alpha:components/usgs-fetcher.md)" in buf.getvalue()


def test_schema_errors_are_shown(gworld, cli, fake) -> None:
    write_raw(gworld.alpha, "decisions/broken.md", "---\ntype: decision\n---\n# Broken\n")
    cli.reports(gworld.alpha, "decisions/broken.md")
    repl, buf = make_repl(gworld, fake, running())
    repl.run(script("/here"))
    out = buf.getvalue()
    assert "→ here alpha:decisions/broken.md" in out and "breaks the schema" in out
    assert repl.attached is not None and repl.attached.schema_errors


def test_attachment_is_capped_and_fenced_safely(gworld, cli) -> None:
    body = "# Big\n\n```python\nprint(1)\n```\n\n" + "x" * 70_000 + "\n"
    write_raw(gworld.alpha, "inbox/big.md", body)
    cli.reports(gworld.alpha, "inbox/big.md")
    here = obsidian.active_note(gworld.session, running())
    block = obsidian.attachment(here)
    assert "[truncated: showing 60,000 of" in block and "read_note alpha:inbox/big.md" in block
    assert block.count("````") == 2  # a fence longer than the note's own ```
    assert len(block) < obsidian.MAX_ATTACH_CHARS + 500


def test_a_failed_turn_keeps_the_attachment_for_the_retry(gworld, cli, fake) -> None:
    import httpx2

    cli.reports(gworld.alpha, "components/usgs-fetcher.md")
    fake.responses += [httpx2.Response(400, json={"error": {"message": "bad request"}}), reply_chunks("ok")]
    repl, buf = make_repl(gworld, fake, running())
    repl.run(script("/here", "first try", "second try"))
    first, second = (b["messages"][-1]["content"] for b in fake.bodies())
    assert "alpha:components/usgs-fetcher.md" in first and "alpha:components/usgs-fetcher.md" in second
    assert second.endswith("second try") and repl.attached is None


def test_end_never_carries_the_attachment(gworld, cli, fake) -> None:
    cli.reports(gworld.alpha, "components/usgs-fetcher.md")
    write_raw(gworld.alpha, "CONTEXT.md", "# CONTEXT\n\n## Log\n")
    fake.responses += [reply_chunks(tool_calls=[{"name": "get_scope", "args": {}, "id": "c1"}]), reply_chunks("ok"),
                       reply_chunks("nothing to propose")]
    repl, _ = make_repl(gworld, fake, running())
    repl.run(script("scope?", "/here", "/end"))
    end_msg = fake.bodies()[-1]["messages"][-1]["content"]
    assert "----- begin CONTEXT.md -----" in end_msg and "usgs-fetcher" not in end_msg


def test_switching_or_dropping_the_vault_clears_the_attachment(gworld, cli, fake) -> None:
    cli.reports(gworld.beta, "concepts/quake-magnitude.md")
    repl, buf = make_repl(gworld, fake, running(), pulled=["beta"])
    repl.run(script("/here", "/drop beta"))
    assert repl.attached is None and "dropped the /here attachment" in buf.getvalue()
    cli.reports(gworld.alpha, "components/usgs-fetcher.md")
    repl.run(script("/here", "/vault gamma"))
    assert repl.attached is None


# Helpers ----------------------------------------------------------------------------------

def script(*lines):
    items = list(lines)

    def read(prompt: str = "") -> str:
        if not items:
            raise EOFError
        item = items.pop(0)
        return item() if callable(item) else item

    return read


def make_repl(w, fake, bridge, pulled=()):
    from daedalus.core.state import load_session

    state = w.root / "state.json"
    state.write_text(json.dumps({"active": "alpha", "pulled": list(pulled)}))
    w.state_path = state
    buf = io.StringIO()
    out = Console(file=buf, width=200, force_terminal=False, color_system=None)
    session = load_session(w.registry_path, state)
    return Repl(session, Config(), fake.client(), out, registry_path=w.registry_path, state_path=state,
                bridge=bridge), buf


def last_user(fake) -> str:
    return [m for m in fake.bodies()[-1]["messages"] if m["role"] == "user"][-1]["content"]
