"""daedalus: the terminal REPL. Ask a question; a DeepSeek agent answers with the vault tools.

    daedalus [project]     # set the active project (as `python -m daedalus.state set-active`), then start

Commands: /new, /vaults, /vault, /pull, /drop, /open, /here, /answer, /promote, /end, /cost, /pending, /diff,
/approve, /reject, /help, /quit.
The Textual interface comes in commit 14; this is the plain REPL.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import secrets as _stdlib_secrets
import shlex
import sys
from pathlib import Path
from typing import Any, Callable

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text

from daedalus import secrets
from daedalus.core import accept, queue
from daedalus.core.apply import apply, diff, pending, reject
from daedalus.core.lifecycle import LifecycleError, new_project
from daedalus.core.policy import Op, PolicyError, authorize
from daedalus.core.registry import RegistryError, load_registry
from daedalus.core.session import Session
from daedalus.core.state import StateError, drop, load_session, pull, set_active
from daedalus.core.tools import RefError

from . import ledger, obsidian
from daedalus.toolset import refusal

from .agent import Agent, Events, ToolOutcome, tool_label
from .config import Config, ConfigError, load_config
from .deepseek import DeepSeek, DeepSeekError, Reply
from .ledger import CapExceeded
from .meter import Interrupted, Reading, render

log = logging.getLogger("daedalus.terminal")

HELP = """\
/new <name> --repo <path>  create <path>/vault/ from the template, commit it, register the project
/vaults                list registered projects (* active, + pulled)
/vault <name>          switch projects: starts a fresh conversation
/pull <name>           read another project too (read-only); /drop <name> stops
/open <note>           show a note in Obsidian (vault:path/to/note.md, or a wikilink target)
/here                  attach the note open in Obsidian to your next message
/answer <id> <n|text>  answer a design question: its option number, or your own answer in quotes
/promote <note> [dir]  accept an inbox draft into the folder its type maps to (e.g. components/)
/end                   propose a CONTEXT.md log entry for this session, review proposals, exit
/cost                  calls, tokens, and dollars for this session and today, plus the DeepSeek balance
/pending               proposals waiting for you
/diff <id>             what a proposal would change
/approve <id>          apply a proposal and commit it
/reject <id> <reason>  reject a proposal
/help                  this list
/quit                  exit without /end (Ctrl-D works too)
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

    def _rows(self, text: str) -> list[str]:
        """``text`` as the terminal rows rich would wrap it into."""
        return [line.plain for line in Text(text.rstrip("\n")).wrap(self.console, max(20, self.console.width))]

    def _tail(self, text: str, rows: int) -> str:
        """The last ``rows`` terminal rows of ``text``."""
        return "\n".join(self._rows(text)[-rows:]) if rows > 0 else ""

    def _render(self) -> Group:
        """The streaming view, never taller than the terminal.

        A transient Live can only erase what's still on screen: anything that
        scrolled off the top stays behind once the call settles. So while a
        call streams, it shows the tail that fits (the settled answer is
        printed in full by call_done).
        """
        budget = max(3, self.console.height - 2)  # room for the meter line and the prompt
        meter_rows = max(1, len(self._rows(self.line)))
        rows = max(1, budget - meter_rows)
        parts: list[Any] = []
        if self.reasoning:
            r_rows = min(REASONING_TAIL, rows - 1 if self.content else rows)
            if r_rows > 0:
                parts.append(Text(self._tail(self.reasoning, r_rows), style="dim italic"))
                rows -= r_rows
        if self.content and rows > 0:
            parts.append(Text(self._tail(self.content, rows)))
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

    def asked(self, result: dict[str, Any]) -> None:
        """ask_user ended the turn: show the question and its numbered options now."""
        self.end()
        self.console.print(Text(f"\n? {result.get('question', '')}", style="bold magenta"))
        self.console.print(Text(f"  {result['category']} · {result['vault']}:{result['rel_path']} · proposal "
                                f"{result['id']}", style="dim"))
        for n, option in enumerate(result.get("options", []), 1):
            self.console.print(f"  {n}. {option}")
        self.console.print(Text(f"  Answer with /answer {result['id']} <number>, or /answer {result['id']} "
                                "\"your own answer\". The agent waits.", style="magenta"))


END_PROMPT = """\
The session is ending. Write its summary now, in this order:

1. Call propose_edit on the active project's CONTEXT.md (rel_path "CONTEXT.md") with the complete new file: \
the current content below, unchanged, plus one new entry at the end of its "## Log" section, headed \
"### {date} — <a short title>". The entry says what was asked, what was learned, what was proposed or \
decided (with proposal ids), and any open threads. Change "## Up Next" only if this session clearly changed \
it. Use the summary "session log: <the short title>".
2. Then, only for something that failed in this session where you learned why and how it was fixed, call \
propose_lesson (type lesson, applies_to tags, source deepseek, a link to a related practice). A lesson records \
what failed, why, and the fix; nothing else qualifies, and most sessions have none.
3. Then reply with one line saying what you proposed.

The current CONTEXT.md of {project}, between the markers:
----- begin CONTEXT.md -----
{context}----- end CONTEXT.md -----"""


class Repl:
    def __init__(self, session: Session, cfg: Config, ds: DeepSeek, console: Console,
                 session_id: str | None = None, registry_path=None, state_path=None,
                 bridge: obsidian.Bridge | None = None) -> None:
        self.session = session
        self.cfg = cfg
        self.ds = ds
        self.console = console
        self.session_id = session_id or _stdlib_secrets.token_hex(8)
        self.registry_path = registry_path
        self.state_path = state_path
        self.agent = Agent(session, ds, cfg, self.session_id)
        self.view = LiveView(console)
        self.bridge = bridge or obsidian.Bridge()
        # /here's note, waiting for your next message. Never the system prompt: that would break the stable
        # prefix DeepSeek caches. One at a time: a second /here replaces it.
        self.attached: obsidian.Here | None = None
        self.read_line: Callable[..., str] = lambda prompt="› ": input(prompt)

    # The loop -------------------------------------------------------------------------

    def run(self, read_line: Callable[..., str]) -> int:
        self.read_line = read_line
        self.console.print(Text(
            f"daedalus · {self.session.active} · {self.cfg.model} (reasoning {self.cfg.reasoning_effort}, "
            f"max {self.cfg.max_tokens:,} tokens) · caps ${self.cfg.session_cap_usd:.2f}/session "
            f"${self.cfg.daily_cap_usd:.2f}/day · /help", style="bold"))
        while True:
            try:
                line = read_line("› ")
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

    def ask(self, text: str, attach: bool = True) -> None:
        sent = self.attached if attach else None
        if sent is not None:
            text = f"{obsidian.attachment(sent)}\n\n{text}"
        try:
            self.agent.turn(text, self.view)
            if sent is not None and self.attached is sent:
                self.attached = None  # delivered; a failed turn keeps it for the retry
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
            if name == "/end":
                self.end()
                return "quit"
            if name == "/new":
                self.new(rest)
            elif name == "/vaults":
                self.vaults()
            elif name == "/vault":
                self.switch(self._id(rest, "/vault <name>"))
            elif name in ("/pull", "/drop"):
                change = pull if name == "/pull" else drop
                self.session = change(self._id(rest, f"{name} <name>"), self.registry_path, self.state_path)
                self.agent.rebuild(self.session)
                if self.attached is not None and self.attached.vault not in {v for v, _ in self.session.scope}:
                    self.console.print(f"dropped the /here attachment {self.attached.ref}: its vault left the session.")
                    self.attached = None
                self.console.print(f"pulled: {', '.join(self.session.pulled) or 'none'} (read-only). The system "
                                   "prompt was rebuilt; the conversation continues.")
            elif name == "/open":
                opened = obsidian.open_note(self.session, self._id(rest, "/open <note>"), self.bridge)
                self.console.print(f"opened {opened.vault}:{opened.rel_path} in Obsidian")
            elif name == "/here":
                self.here()
            elif name == "/answer":
                change_id, _, answer = rest.partition(" ")
                if not change_id or not answer.strip():
                    raise ValueError('usage: /answer <question-id> <option-number | "free text">')
                done = accept.answer_question(self.session, change_id, answer)
                if done.approved_question:
                    self.console.print(f"approved the question {done.question_ref} "
                                       f"(commit {done.approved_question[:7]})")
                self.console.print(f"answered {done.question_ref}: {done.chosen}")
                self.console.print(f"accepted {done.decision_ref} (source: human); one commit, {done.commit_sha[:7]}, "
                                   "holds the decision and the answered question.")
            elif name == "/promote":
                words = shlex.split(rest)
                if not 1 <= len(words) <= 2:
                    raise ValueError("usage: /promote <inbox-note> [folder]")
                done = accept.promote(self.session, words[0], words[1] if len(words) == 2 else None)
                self.console.print(f"promoted {done.source_ref} → {done.target_ref} (status: accepted, source kept) "
                                   f"in commit {done.commit_sha[:7]}")
            elif name == "/help":
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
        except PolicyError as e:
            self.console.print(Text(f"error: {refusal(e)}", style="red"))
        except (queue.ProposalError, StateError, RegistryError, LifecycleError, KeyError, ValueError, RefError,
                obsidian.ObsidianError) as e:
            self.console.print(Text(f"error: {e}", style="red"))
        return None

    # Projects ---------------------------------------------------------------------------

    def new(self, rest: str) -> None:
        words = shlex.split(rest)
        if len(words) != 3 or words[1] != "--repo":
            raise ValueError("usage: /new <name> --repo <path>")
        made = new_project(words[0], words[2], self.registry_path, self.state_path)
        self.console.print(f"{len(made.files)} files committed in {made.repo} as \"init: daedalus vault from "
                           f"template\" ({made.commit_sha[:7]}).")
        self.console.print(made.instructions())

    def vaults(self) -> None:
        reg = load_registry(self.registry_path)
        for name in sorted(reg.projects):
            mark = "*" if name == self.session.active else ("+" if name in self.session.pulled else " ")
            note = " (active)" if mark == "*" else (" (pulled, read-only)" if mark == "+" else "")
            self.console.print(f"{mark} {name:20} {reg.projects[name]}{note}")

    def switch(self, name: str) -> None:
        if name == self.session.active:
            self.console.print(f"{name} is already the active project.")
            return
        self.session = set_active(name, self.registry_path, self.state_path)
        # A fresh conversation: history about the old project mustn't carry into this one. Same session id,
        # so the session spend cap carries over: it belongs to this terminal, not to a project.
        self.agent = Agent(self.session, self.ds, self.cfg, self.session_id, meter=self.agent.meter)
        self.attached = None  # a fresh conversation starts with nothing attached
        self.console.print(Text(f"Switched to {name}. Fresh conversation: history cleared, system prompt rebuilt "
                                f"from {name}'s AGENTS.md and CONTEXT.md.", style="bold"))

    # Obsidian ---------------------------------------------------------------------------

    def here(self) -> None:
        """Attach the note open in Obsidian to the next message, replacing any earlier /here."""
        try:
            registry = load_registry(self.registry_path)  # fresh, so a vault registered since start is known
        except RegistryError:
            registry = None
        found = obsidian.active_note(self.session, self.bridge, registry)
        if isinstance(found, obsidian.Problem):
            dropped = self.attached
            self.attached = None  # a failed /here attaches nothing, not a stale note
            self.console.print(Text(found.message, style="yellow"))
            if dropped is not None:
                self.console.print(Text(f"(the earlier /here note, {dropped.ref}, was dropped too)", style="yellow"))
            return
        replaced = self.attached
        self.attached = found
        cut = (f", attaching the first {obsidian.MAX_ATTACH_CHARS:,}"
               if len(found.text) > obsidian.MAX_ATTACH_CHARS else "")
        self.console.print(Text(f"→ here {found.ref} ({len(found.text):,} chars{cut})", style="green"))
        if found.schema_errors:
            self.view.schema_errors(found.ref, found.schema_errors)
        if replaced is not None:
            self.console.print(Text(f"  (replaces {replaced.ref})", style="dim"))
        self.console.print(Text("  It goes with your next message.", style="dim"))

    # /end -------------------------------------------------------------------------------

    def end(self) -> None:
        """Propose this session's CONTEXT.md log entry (and any lessons), walk the proposals, then exit."""
        if self.agent.tool_calls == 0 and not self.agent.proposals:
            self.console.print("Nothing happened in this session that needs a log entry. Bye.")
            return
        context = authorize(self.session.active_vault / "CONTEXT.md", Op.READ, self.session).path
        current = context.read_text(encoding="utf-8")
        self.console.print(Text(f"Writing the session summary for {self.session.active}'s CONTEXT.md …", style="bold"))
        self.ask(END_PROMPT.format(date=dt.date.today().isoformat(), project=self.session.active,
                                   context=current if current.endswith("\n") else current + "\n"), attach=False)
        self.review(self.agent.proposals)

    def review(self, ids: list[str]) -> None:
        """Walk proposals one at a time: approve, reject with a reason, or skip (leave pending)."""
        waiting = [i for i in dict.fromkeys(ids) if queue.get(i).status == "pending"]
        if not waiting:
            self.console.print("No proposals to review.")
            return
        for n, change_id in enumerate(waiting, 1):
            p = queue.get(change_id)
            self.console.print(Text(f"\n[{n}/{len(waiting)}] {p.id}  {p.kind}  {p.vault}:{p.rel_path}  ({p.source}) "
                                    f"{p.summary}", style="bold"))
            self.console.print(Text(diff(p.id)))
            while True:
                try:
                    choice = self.read_line("[a]pprove, [r]eject, or [s]kip? ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    self.console.print("Left the rest pending.")
                    return
                try:
                    if choice in ("a", "approve"):
                        done = apply(self.session, p.id)
                        self.console.print(f"applied {done.id} in commit {done.commit_sha[:7]}")
                    elif choice in ("r", "reject"):
                        reason = ""
                        while not reason:
                            reason = self.read_line("reason: ").strip()
                        self.console.print(f"rejected {reject(p.id, reason).id}: {reason}")
                    elif choice in ("s", "skip", ""):
                        self.console.print(f"skipped {p.id}; it stays pending (/pending lists it).")
                    else:
                        continue
                except (PolicyError, queue.ProposalError) as e:
                    self.console.print(Text(f"error: {e}", style="red"))
                break

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

    if not sys.stdin.isatty():  # piped input: no line editor
        return lambda prompt="› ": input(prompt)
    history = queue.STATE_ROOT / "repl_history"
    history.parent.mkdir(parents=True, exist_ok=True)
    ps: PromptSession = PromptSession(history=FileHistory(str(history)))
    return lambda prompt="› ": ps.prompt(prompt)


def main(argv: list[str] | None = None, *, transport=None, read_line: Callable[..., str] | None = None,
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
            set_active(args.project, args.registry, args.state)
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
        repl = Repl(session, cfg, ds, out, registry_path=args.registry, state_path=args.state)
        log.info("session %s started: project %s, model %s", repl.session_id, session.active, cfg.model)
        code = repl.run(read_line or _prompt_reader())
        log.info("session %s ended", repl.session_id)
        return code
    finally:
        ds.close()
        undo_logging()


if __name__ == "__main__":
    sys.exit(main())
