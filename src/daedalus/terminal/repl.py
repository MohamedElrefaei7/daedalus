"""daedalus: the terminal REPL. Ask a question; a DeepSeek agent answers with the vault tools.

    daedalus [project]     # set the active project (as `python -m daedalus.state set-active`), then start

Commands: /cost, /pending, /diff <id>, /approve <id>, /reject <id> <reason>, /help, /quit.
The Textual interface comes in commit 14; this is the plain REPL.
"""

from __future__ import annotations

import argparse
import logging
import secrets as _stdlib_secrets
import sys
from pathlib import Path
from typing import Any, Callable

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text

from daedalus import secrets
from daedalus.core import queue
from daedalus.core.apply import apply, diff, pending, reject
from daedalus.core.policy import PolicyError
from daedalus.core.registry import RegistryError
from daedalus.core.session import Session
from daedalus.core.state import StateError, load_session, read_state, save_state

from . import ledger
from .agent import Agent, Events, ToolOutcome, tool_label
from .config import Config, ConfigError, load_config
from .deepseek import DeepSeek, DeepSeekError, Reply
from .ledger import CapExceeded
from .meter import Interrupted, Reading, render

log = logging.getLogger("daedalus.terminal")

HELP = """\
/cost                  calls, tokens, and dollars for this session and today, plus the DeepSeek balance
/pending               proposals waiting for you
/diff <id>             what a proposal would change
/approve <id>          apply a proposal and commit it
/reject <id> <reason>  reject a proposal
/help                  this list
/quit                  exit (Ctrl-D works too)
Ctrl-C stops a streaming answer; the call is recorded as an estimate."""

REASONING_TAIL = 6  # lines of reasoning shown while it streams


class LiveView(Events):
    """Streams one call at a time into a rich Live, then prints its settled form."""

    def __init__(self, console: Console) -> None:
        self.console = console
        self.live: Live | None = None
        self.reasoning = ""
        self.content = ""
        self.line = ""

    # One Live per model call.
    def call_start(self) -> None:
        self.end()
        self.reasoning, self.content, self.line = "", "", ""
        self.live = Live(self._render(), console=self.console, transient=True, refresh_per_second=12)
        self.live.start()

    def end(self) -> None:
        if self.live is not None:
            self.live.stop()
            self.live = None

    def _render(self) -> Group:
        parts: list[Any] = []
        if self.reasoning:
            tail = "\n".join(self.reasoning.rstrip().splitlines()[-REASONING_TAIL:])
            parts.append(Text(tail, style="dim italic"))
        if self.content:
            parts.append(Text(self.content))
        parts.append(Text(self.line, style="cyan"))
        return Group(*parts)

    def delta(self, kind: str, text: str) -> None:
        if kind == "reasoning":
            self.reasoning += text
        elif kind == "content":
            self.content += text

    def reading(self, reading: Reading) -> None:
        self.line = render(reading)
        if self.live is not None:
            self.live.update(self._render())

    def call_done(self, reply: Reply, reading: Reading) -> None:
        self.end()
        if reply.reasoning:
            self.console.print(Text(f"▸ reasoning, {reading.reasoning_tokens:,} tokens (collapsed)", style="dim"))
        if reply.content.strip():
            self.console.print(Markdown(reply.content))
        self.console.print(Text(render(reading), style="cyan"))

    def tool(self, outcome: ToolOutcome) -> None:
        label = tool_label(outcome.name, outcome.args)
        if outcome.ok:
            self.console.print(Text(label, style="green"))
        else:
            first = outcome.text.splitlines()[0] if outcome.text else "refused"
            self.console.print(Text(f"{label}  ✗ {first}", style="yellow"))

    def schema_errors(self, ref: str, errors: list[str]) -> None:
        self.console.print(Text(f"  ⚠ {ref} breaks the schema:", style="bold yellow"))
        for e in errors:
            self.console.print(Text(f"    - {e}", style="yellow"))

    def stopped(self, message: str) -> None:
        self.end()
        self.console.print(Text(message, style="bold yellow"))


class Repl:
    def __init__(self, session: Session, cfg: Config, ds: DeepSeek, console: Console,
                 session_id: str | None = None) -> None:
        self.session = session
        self.cfg = cfg
        self.ds = ds
        self.console = console
        self.session_id = session_id or _stdlib_secrets.token_hex(8)
        self.agent = Agent(session, ds, cfg, self.session_id)
        self.view = LiveView(console)

    # The loop -------------------------------------------------------------------------

    def run(self, read_line: Callable[[], str]) -> int:
        self.console.print(Text(
            f"daedalus · {self.session.active} · {self.cfg.model} (reasoning {self.cfg.reasoning_effort}, "
            f"max {self.cfg.max_tokens:,} tokens) · caps ${self.cfg.session_cap_usd:.2f}/session "
            f"${self.cfg.daily_cap_usd:.2f}/day · /help", style="bold"))
        while True:
            try:
                line = read_line()
            except KeyboardInterrupt:
                continue
            except EOFError:
                return 0
            line = line.strip()
            if not line:
                continue
            if line.startswith("/"):
                if self.command(line) == "quit":
                    return 0
                continue
            self.ask(line)

    def ask(self, text: str) -> None:
        try:
            self.agent.turn(text, self.view)
        except CapExceeded as e:
            self.view.end()
            self.console.print(Text(str(e), style="bold red"))
        except DeepSeekError as e:
            self.view.end()
            self.console.print(Text(str(e), style="bold red"))
        except Interrupted as e:
            self.view.end()
            self.console.print(Text(f"~ {e}", style="yellow"))
        except KeyboardInterrupt:
            self.view.end()
            self.console.print(Text("stopped.", style="yellow"))
        finally:
            self.view.end()

    # Commands -------------------------------------------------------------------------

    def command(self, line: str) -> str | None:
        name, _, rest = line.partition(" ")
        rest = rest.strip()
        try:
            if name in ("/quit", "/exit"):
                return "quit"
            if name == "/help":
                self.console.print(HELP)
            elif name == "/cost":
                self.cost()
            elif name == "/pending":
                self.pending()
            elif name == "/diff":
                self.console.print(Text(diff(self._id(rest, "/diff <id>"))))
            elif name == "/approve":
                p = apply(self.session, self._id(rest, "/approve <id>"))
                self.console.print(f"applied {p.id}: {p.vault}:{p.rel_path} in commit {p.commit_sha[:7]}")
            elif name == "/reject":
                change_id, _, reason = rest.partition(" ")
                p = reject(self._id(change_id, "/reject <id> <reason>"), reason.strip())
                self.console.print(f"rejected {p.id}: {p.status_reason}")
            else:
                self.console.print(Text(f"unknown command {name}; /help lists them", style="yellow"))
        except (PolicyError, queue.ProposalError, KeyError, ValueError) as e:
            self.console.print(Text(f"error: {e}", style="red"))
        return None

    @staticmethod
    def _id(value: str, usage: str) -> str:
        if not value:
            raise ValueError(f"usage: {usage}")
        return value

    def pending(self) -> None:
        in_scope = {name for name, _ in self.session.scope}
        rows = [p for p in pending() if p.vault in in_scope]
        for p in rows:
            self.console.print(f"{p.id}  {p.kind:6}  {p.vault}:{p.rel_path}  ({p.source}) {p.summary}")
        if not rows:
            self.console.print("no pending proposals")

    def cost(self) -> None:
        now = ledger.utc_now()
        for label, t in (("this session", ledger.totals(session_id=self.session_id)),
                         ("today (UTC)", ledger.totals(since_utc=ledger.start_of_day(now)))):
            est = f", {t.estimated_calls} estimated" if t.estimated_calls else ""
            self.console.print(
                f"{label:13} {t.calls} call(s){est}   ↑ {t.prompt_cache_hit + t.prompt_cache_miss:,} "
                f"({t.prompt_cache_hit:,} cached)   ↓ {t.completion:,} (reasoning {t.reasoning:,})   ${t.cost_usd:.4f}"
            )
        self.console.print(f"caps          ${self.cfg.session_cap_usd:.2f} per session, "
                           f"${self.cfg.daily_cap_usd:.2f} per UTC day")
        try:
            b = self.ds.balance()
            infos = ", ".join(f"{i.get('total_balance')} {i.get('currency')}" for i in b.get("balance_infos", []))
            ok = "" if b.get("is_available", True) else " (not enough for API calls)"
            self.console.print(f"balance       {infos or 'unknown'}{ok}")
        except DeepSeekError as e:
            self.console.print(Text(f"balance       unavailable: {e}", style="yellow"))


# Entry point ------------------------------------------------------------------------------

def setup_logging() -> Callable[[], None]:
    """The daedalus loggers only, to repl.log; the SDK's loggers stay off (they can echo request options).

    Returns the undo, so main() leaves logging as it found it.
    """
    path = queue.STATE_ROOT / "repl.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger = logging.getLogger("daedalus")
    before = (logger.level, logger.propagate)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    def undo() -> None:
        logger.removeHandler(handler)
        handler.close()
        logger.setLevel(before[0])
        logger.propagate = before[1]

    return undo


def _prompt_reader() -> Callable[[], str]:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory

    history = queue.STATE_ROOT / "repl_history"
    history.parent.mkdir(parents=True, exist_ok=True)
    ps: PromptSession = PromptSession(history=FileHistory(str(history)))
    return lambda: ps.prompt("› ")


def main(argv: list[str] | None = None, *, transport=None, read_line: Callable[[], str] | None = None,
         console: Console | None = None) -> int:
    parser = argparse.ArgumentParser(prog="daedalus", description="The Daedalus terminal.")
    parser.add_argument("project", nargs="?", help="make this the active project first")
    parser.add_argument("--registry", help=argparse.SUPPRESS)
    parser.add_argument("--state", help=argparse.SUPPRESS)
    parser.add_argument("--config", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    out = console or Console()

    def fail(message: str) -> int:
        out.print(Text(message, style="bold red"))
        return 1

    try:
        if args.project:
            try:
                pulled = [p for p in read_state(args.state)["pulled"] if p != args.project]
            except StateError:
                pulled = []
            save_state(args.project, pulled, args.registry, args.state)
        session = load_session(args.registry, args.state)
        cfg = load_config(args.config)
    except (StateError, RegistryError, ConfigError) as e:
        return fail(f"error: {e}")

    key = secrets.get_key("deepseek")
    if key is None:
        return fail(secrets.missing_key_message("deepseek"))  # before any client exists: nothing is sent

    undo_logging = setup_logging()
    ds = DeepSeek(key, transport=transport)
    try:
        repl = Repl(session, cfg, ds, out)
        log.info("session %s started: project %s, model %s", repl.session_id, session.active, cfg.model)
        code = repl.run(read_line or _prompt_reader())
        log.info("session %s ended", repl.session_id)
        return code
    finally:
        ds.close()
        undo_logging()


if __name__ == "__main__":
    sys.exit(main())
