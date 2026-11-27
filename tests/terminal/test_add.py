"""/add in the REPL: one summary line, the data-rule reminder, and no model call."""

from __future__ import annotations

import io
import json

from rich.console import Console

from daedalus.core.state import load_session
from daedalus.terminal.config import Config
from daedalus.terminal.repl import Repl

BODY = "# Quarterly report\n\n" + "The readings rose sharply this quarter at every station. " * 3 + "\n"


def make_repl(w, fake):
    state = w.root / "state.json"
    state.write_text(json.dumps({"active": "alpha", "pulled": []}))
    buf = io.StringIO()
    out = Console(file=buf, width=200, force_terminal=False, color_system=None)
    return Repl(load_session(w.registry_path, state), Config(), fake.client(), out, registry_path=w.registry_path,
                state_path=state), buf


def test_add_prints_the_summary_and_the_reminder_and_calls_no_model(gworld, fake, tmp_path) -> None:
    src = tmp_path / "docs" / "quarterly report.md"
    src.parent.mkdir()
    src.write_text(BODY)
    repl, buf = make_repl(gworld, fake)
    repl.read_line = None
    for line in (f"/add {src}", f"/add {str(src).replace(' ', chr(92) + ' ')}", "/add", "/add /no/such.pdf"):
        repl.command(line)
    lines = buf.getvalue().splitlines()
    assert f"→ added alpha:sources/quarterly-report.md ({len(BODY):,} chars, from quarterly report.md)" in lines
    assert "Agents can read this, and anything they read is sent to DeepSeek." in lines
    out = buf.getvalue()
    assert "already in the vault as alpha:sources/quarterly-report.md" in out  # the escaped path is the same file
    assert "usage: /add <file>" in out and "no such file" in out
    assert fake.requests == []
