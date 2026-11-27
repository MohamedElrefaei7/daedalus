"""daedalus: the terminal REPL. Ask a question; a DeepSeek agent answers with the vault tools.

    daedalus [project]     # set the active project (as `python -m daedalus.state set-active`), then start

Commands: /new, /vaults, /vault, /pull, /drop, /open, /here, /add, /answer, /promote, /wt, /code, /goal, /review,
/end, /cost, /pending, /diff, /approve, /reject, /help, /quit.
The Textual interface comes in commit 14; this is the plain REPL.
"""

from __future__ import annotations

import argparse
import dataclasses
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
import yaml

from daedalus.core import accept, notes, queue, worktree
from daedalus.core.goals import GoalError
from daedalus.core.registry import SHARED_NAME
from daedalus.core.apply import apply, diff, pending, reject
from daedalus.core.ingest import ingest
from daedalus.core.lifecycle import LifecycleError, new_project
from daedalus.core.policy import Op, PolicyError, authorize
from daedalus.core.registry import RegistryError, load_registry
from daedalus.core.session import Session
from daedalus.core.state import StateError, drop, load_session, pull, set_active
from daedalus.core.tools import RefError
from daedalus.sandbox.errors import SandboxError

from . import ledger, obsidian
from daedalus.toolset import refusal

from . import loop as goal_loop
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
/add <file>            convert a local .pdf/.docx/.pptx/.xlsx (or .md/.txt) into sources/ and commit it
/answer <id> <n|text>  answer a design question: its option number, or your own answer in quotes
/promote <note> [dir]  accept an inbox draft into the folder its type maps to (e.g. components/)
/wt new <slug>         a git worktree for the agent, on branch agent/<slug>, from your checkout's HEAD
/wt list               worktrees: branch, commits ahead, uncommitted changes
/wt diff <slug>        what agent/<slug> changes, against its merge base with HEAD
/wt merge <slug>       merge agent/<slug> into your checkout (--no-ff); refused if your checkout is dirty
/wt drop <slug> [--force]  remove the worktree and its branch; unmerged work needs --force
/code <slug>           coder mode: the agent edits and runs checks in that worktree (fresh conversation)
/code off              leave coder mode, offering to commit the worktree's changes
/goal <note>           run the coding loop on a goal note you wrote (goals/<note>.md) in a fresh worktree
/goal resume [text]    continue a paused or stopped run; text is guidance for the next iteration
/goal list             goal runs and their state
/review lessons        walk pending lessons in the shared inbox: approve, reject, or skip
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
RUN_TAIL_LINES = 12  # lines of a run's output shown in the terminal

COMMIT_SYSTEM = ("You write git commit messages. Reply with exactly one line: imperative mood, at most 72 "
                 "characters, no quotes, no trailing period.")


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
        if outcome.ok and outcome.name == "run" and outcome.result:
            r = outcome.result
            state = "timed out" if r.get("timed_out") else f"exit {r.get('exit_code')}"
            style = "green" if r.get("exit_code") == 0 and not r.get("timed_out") else "yellow"
            self.console.print(Text(f"{label}  · sandboxed · {state} in {r.get('duration_s', 0):.2f} s", style=style))
            lines = (r.get("stdout_tail") or "").rstrip("\n").splitlines()[-RUN_TAIL_LINES:]
            for line in lines:
                self.console.print(Text(f"  │ {line}", style="dim"))
        elif outcome.ok:
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
        self.coding: worktree.Record | None = None  # set in coder mode (/code <slug>)
        # What coder mode did this terminal session, for /end: each stint's summary, and its proposals.
        self.coded: list[str] = []
        self.carried_proposals: list[str] = []
        self.runner = None  # the goal loop's check runner; None is the real sandbox (tests inject a fake)

    def prompt(self) -> str:
        if self.coding is not None:
            return f"[coding {self.session.active} · {self.coding.branch}] › "
        return "› "

    # The loop -------------------------------------------------------------------------

    def run(self, read_line: Callable[..., str]) -> int:
        self.read_line = read_line
        self.console.print(Text(
            f"daedalus · {self.session.active} · {self.cfg.model} (reasoning {self.cfg.reasoning_effort}, "
            f"max {self.cfg.max_tokens:,} tokens) · caps ${self.cfg.session_cap_usd:.2f}/session "
            f"${self.cfg.daily_cap_usd:.2f}/day · /help", style="bold"))
        while True:
            try:
                line = read_line(self.prompt())
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
            if name == "/wt":
                self.wt(rest)
            elif name == "/code":
                self.code(rest)
            elif name == "/goal":
                self.goal(rest)
            elif name == "/review":
                self.review_lessons(rest)
            elif name == "/new":
                self.new(rest)
            elif name == "/vaults":
                self.vaults()
            elif name == "/vault":
                self.switch(self._id(rest, "/vault <name>"))
            elif name in ("/pull", "/drop"):
                change = pull if name == "/pull" else drop
                self.session = change(self._id(rest, f"{name} <name>"), self.registry_path, self.state_path)
                if self.coding is not None:  # state.json never holds the worktree; keep coding in it
                    self.session = dataclasses.replace(self.session, worktree=Path(self.coding.path))
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
            elif name == "/add":
                self.add(rest)
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
        except worktree.MergeConflict as e:
            self.console.print(Text(f"error: {e}", style="red"))
            for f in e.files:
                self.console.print(Text(f"  conflict: {f}", style="red"))
        except (queue.ProposalError, StateError, RegistryError, LifecycleError, KeyError, ValueError, RefError,
                obsidian.ObsidianError, worktree.WorktreeError, SandboxError, GoalError) as e:
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
        if self.coding is not None:
            self.leave_coding()
        self.session = set_active(name, self.registry_path, self.state_path)
        # A fresh conversation: history about the old project mustn't carry into this one. Same session id,
        # so the session spend cap carries over: it belongs to this terminal, not to a project.
        self.agent = Agent(self.session, self.ds, self.cfg, self.session_id, meter=self.agent.meter)
        self.attached = None  # a fresh conversation starts with nothing attached
        self.console.print(Text(f"Switched to {name}. Fresh conversation: history cleared, system prompt rebuilt "
                                f"from {name}'s AGENTS.md and CONTEXT.md.", style="bold"))

    # Worktrees and coder mode ---------------------------------------------------------

    def wt(self, rest: str) -> None:
        words = shlex.split(rest)
        sub, args = (words[0], words[1:]) if words else ("", [])
        registry, project = self.session.registry, self.session.active
        if sub == "new" and len(args) == 1:
            made = worktree.create(self.session, args[0])
            r = made.record
            self.console.print(f"created {r.path}")
            self.console.print(f"  branch {r.branch}, from {project}'s HEAD ({r.base[:7]}). Next: /code {r.slug}")
            if made.warning:
                self.console.print(Text(f"  ⚠ {made.warning}", style="yellow"))
        elif sub == "list" and not args:
            rows = worktree.list_worktrees(registry, project)
            for w in rows:
                if w.state == "unrecorded":
                    self.console.print(Text(f"? {w.branch or '(detached)'}  {w.path}  (git knows this worktree; "
                                            "daedalus didn't create it)", style="yellow"))
                elif w.state == "missing":
                    self.console.print(Text(f"! {w.slug:16} {w.branch}  missing: {w.path}", style="yellow"))
                else:
                    mark = "*" if self.coding is not None and self.coding.slug == w.slug else " "
                    changes = "uncommitted changes" if w.dirty else "clean"
                    self.console.print(f"{mark} {w.slug:16} {w.branch:24} {w.ahead} ahead  {changes:19}  {w.path}")
            if not rows:
                self.console.print(f"no worktrees for {project}; /wt new <slug> makes one")
        elif sub == "diff" and len(args) == 1:
            text = worktree.diff(registry, project, args[0])
            self.console.print(Text(text) if text.strip() else f"agent/{args[0]} has no committed changes yet.")
            if worktree.changes(project, args[0]):
                self.console.print(Text("  (the worktree also has uncommitted changes, not shown)", style="dim"))
        elif sub == "merge" and len(args) == 1:
            sha = worktree.merge(registry, project, args[0])
            self.console.print(f"merged agent/{args[0]} into {self.session.active_repo} as {sha[:7]} (--no-ff)")
            self.console.print(Text(f"  /wt drop {args[0]} removes the worktree and its branch.", style="dim"))
        elif sub == "drop" and len(args) in (1, 2) and (len(args) == 1 or args[1] == "--force"):
            if self.coding is not None and self.coding.slug == args[0]:
                raise ValueError(f"you're coding in {args[0]}; /code off first")
            r = worktree.drop(registry, project, args[0], force=len(args) == 2)
            self.console.print(f"dropped {r.path} and {r.branch}")
        else:
            raise ValueError("usage: /wt new <slug> | /wt list | /wt diff <slug> | /wt merge <slug> | "
                             "/wt drop <slug> [--force]")

    def code(self, rest: str) -> None:
        if rest == "off":
            if self.coding is None:
                raise ValueError("you're not in coder mode")
            self.leave_coding()
            self.console.print(Text("Left coder mode. Fresh conversation, chat tools.", style="bold"))
            return
        slug = self._id(rest, "/code <slug> | /code off")
        if self.coding is not None:
            raise ValueError(f"already coding in {self.coding.slug}; /code off first")
        rec = worktree.get(self.session.active, slug)
        if not Path(rec.path).is_dir():
            raise ValueError(f"{rec.path} is missing; /wt list shows the worktrees")
        self.session = dataclasses.replace(self.session, worktree=Path(rec.path))
        self.agent = Agent(self.session, self.ds, self.cfg, self.session_id, meter=self.agent.meter, profile="coder")
        self.attached = None
        self.coding = rec
        self.console.print(Text(f"Coding {self.session.active} on {rec.branch} in {rec.path}. Fresh conversation; "
                                "the agent can edit_code, write_code, delete_code, and run checks there, sandboxed. "
                                "/code off leaves.", style="bold"))

    def commit_message(self, changes: list[str]) -> str:
        """An agent-written one-line message for the worktree's changes; the edit log's paths if that fails."""
        edits = self.agent.edit_log.edits
        fallback = "agent: " + (", ".join(f"{e.op} {e.rel_path}" for e in edits[-4:]) or "update worktree")
        asks = [m["content"] for m in self.agent.history if m.get("role") == "user"][:3]
        runs = [f"{' '.join(r['argv'] or [])} → exit {r['exit_code']}" for r in self.agent.runs[-3:]]
        user = ("What the user asked:\n" + "\n".join(f"- {a[:300]}" for a in asks) +
                "\n\nChanged paths (git status --porcelain):\n" + "\n".join(changes[:40]) +
                "\n\nEdits:\n" + "\n".join(f"- {e.op} {e.rel_path}" for e in edits[-40:]) +
                ("\n\nLast runs:\n" + "\n".join(runs) if runs else ""))
        try:
            line = self.agent.one_shot(COMMIT_SYSTEM, user).splitlines()
        except (CapExceeded, DeepSeekError, Interrupted, KeyboardInterrupt):
            return fallback[:72]
        message = (line[0] if line else "").strip().strip("\"'`").rstrip(".")
        return message[:72] or fallback[:72]

    def leave_coding(self) -> str:
        """Offer to commit the worktree's changes, then go back to the chat profile with a fresh conversation.

        Returns a summary of the coding session, for /end's log entry.
        """
        rec, coder = self.coding, self.agent
        assert rec is not None
        changes = worktree.uncommitted(self.session)
        committed = None
        if changes:
            self.console.print(f"The {rec.slug} worktree has {len(changes)} uncommitted change(s):")
            for line in changes[:20]:
                self.console.print(Text(f"  {line}", style="dim"))
            try:
                yes = self.read_line(f"commit them to {rec.branch}? [Y/n] ").strip().lower() in ("", "y", "yes")
            except (EOFError, KeyboardInterrupt):
                yes = False
            if yes:
                default = self.commit_message(changes)
                try:
                    message = self.read_line(f"message [{default}]: ").strip() or default
                except (EOFError, KeyboardInterrupt):
                    message = default
                sha = worktree.commit(self.session, message)
                committed = (sha, message)
                self.console.print(Text(f"committed {sha[:7]} on {rec.branch}: {message}", style="green"))
            else:
                self.console.print(f"Left them uncommitted in the worktree; /code {rec.slug} picks them up again.")
        self.session = dataclasses.replace(self.session, worktree=None)
        self.agent = Agent(self.session, self.ds, self.cfg, self.session_id, meter=coder.meter)
        self.coding = None
        self.carried_proposals += coder.proposals
        runs = "; ".join(f"{' '.join(r['argv'] or [])} → exit {r['exit_code']}" for r in coder.runs) or "none"
        edits = ", ".join(f"{e.op} {e.rel_path}" for e in coder.edit_log.edits) or "none"
        outcome = (f"committed {committed[0][:7]} \"{committed[1]}\"" if committed else
                   "uncommitted changes left in the worktree" if changes else "nothing to commit")
        summary = (f"Coder mode on {rec.branch} ({rec.path}): edits: {edits}. Runs: {runs}. Then: {outcome}. "
                   "Merging is the user's action (/wt merge).")
        self.coded.append(summary)
        return summary

    # Goals ------------------------------------------------------------------------------

    def goal(self, rest: str) -> None:
        if self.coding is not None:
            raise ValueError("leave coder mode (/code off) before running a goal")
        sub, _, more = rest.partition(" ")
        if not sub:
            raise ValueError("usage: /goal <note> | /goal resume [guidance] | /goal list")
        if sub == "list":
            runs = goal_loop.all_runs(self.session.active)
            for s in runs:
                state = s["status"] + (f" ({s['pause']})" if s.get("pause") else "") + (
                    f": {s['stop_reason']}" if s.get("stop_reason") else "")
                self.console.print(f"{s['run_id']:32} {s['goal_ref']:28} {len(s['iterations'])} iteration(s)  {state}")
            if not runs:
                self.console.print(f"no goal runs in {self.session.active}")
            return
        run = goal_loop.Loop(self.session, self.ds, self.cfg, self.session_id, LoopView(self), meter=self.agent.meter,
                             runner=self.runner)
        try:
            state = run.resume(guidance=more.strip() or None) if sub == "resume" else run.start(rest.strip())
        finally:
            self.carried_proposals += run.proposals
        if state["status"] == "stopped":
            self.coded.append(f"Goal run {state['run_id']} ({state['goal_ref']}, {state['branch']}): "
                              f"{state['stop_reason']} after {len(state['iterations'])} iteration(s).")

    def review_lessons(self, rest: str) -> None:
        if rest.strip() != "lessons":
            raise ValueError("usage: /review lessons")

        def is_lesson(p) -> bool:
            if p.vault != SHARED_NAME or not p.rel_path.startswith("inbox/"):
                return False
            raw, _ = notes.split_frontmatter(p.content)
            try:
                return (yaml.safe_load(raw or "") or {}).get("type") == "lesson"
            except yaml.YAMLError:
                return False

        ids = [p.id for p in pending() if is_lesson(p)]
        if not ids:
            self.console.print("No pending lessons.")
            return
        self.review(ids, after_approve=lambda p: self.console.print(Text(
            f"  It's in shared:{p.rel_path} as a draft. To accept it as a lesson: /promote shared:{p.rel_path}",
            style="dim")))

    # Sources ----------------------------------------------------------------------------

    def add(self, rest: str) -> None:
        """/add <file>: no model call. Quote or backslash-escape spaces; a dragged-in path works as is."""
        if not rest:
            raise ValueError("usage: /add <file>")
        try:
            words = shlex.split(rest)
        except ValueError:
            words = [rest]
        path = words[0] if len(words) == 1 else rest  # an unquoted path with spaces
        done = ingest(self.session, path)
        self.console.print(Text(f"→ added {done.ref} ({done.chars:,} chars, from {done.original_name})",
                                style="green"))
        self.console.print(Text("Agents can read this, and anything they read is sent to DeepSeek.", style="yellow"))

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
        """Propose this session's CONTEXT.md log entry (and any lessons), walk the proposals, then exit.

        In coder mode it first leaves coder mode (offering the commit). The log entry is then written by a
        chat-profile agent, which has propose_edit, from a summary of the coding session.
        """
        if self.coding is not None:
            self.leave_coding()
        carried = self.carried_proposals
        if not self.coded and self.agent.tool_calls == 0 and not self.agent.proposals and not carried:
            self.console.print("Nothing happened in this session that needs a log entry. Bye.")
            return
        context = authorize(self.session.active_vault / "CONTEXT.md", Op.READ, self.session).path
        current = context.read_text(encoding="utf-8")
        self.console.print(Text(f"Writing the session summary for {self.session.active}'s CONTEXT.md …", style="bold"))
        prompt = END_PROMPT.format(date=dt.date.today().isoformat(), project=self.session.active,
                                   context=current if current.endswith("\n") else current + "\n")
        if self.coded:
            prompt = ("This session included coder mode, which has no propose_edit, so you write its log entry: "
                      + " ".join(self.coded) + f"\n\n{prompt}")
        self.ask(prompt, attach=False)
        self.review(carried + self.agent.proposals)

    def review(self, ids: list[str], after_approve: Callable[[Any], None] | None = None) -> None:
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
                        if after_approve is not None:
                            after_approve(done)
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


class LoopView:
    """The goal loop's face in the REPL: its messages, the agent's stream, and check-ins."""

    def __init__(self, repl: "Repl") -> None:
        self.repl = repl
        self.events = repl.view

    def say(self, text: str, style: str | None = None) -> None:
        self.repl.view.end()
        self.repl.console.print(Text(text, style=style or ""))

    def checkin(self, info: goal_loop.CheckIn) -> tuple[str, str | None]:
        out = self.repl.console
        self.repl.view.end()
        why = f"config changed: {', '.join(info.config_changed)}" if info.config_changed else "scheduled"
        total = f"; ${info.total_spent:.4f} in total across {info.batches} batches" if info.batches > 1 else ""
        out.print(Text(f"\n── check-in ({why}) ── iteration {info.iteration} of {info.max_iterations} · spent "
                       f"${info.spent:.4f} of ${info.budget:.2f}{' this batch' if total else ''}{total}",
                       style="bold magenta"))
        out.print(f"last: {info.last_summary}")
        out.print(Text(info.diff_stat, style="dim"))
        if info.config_diff:
            out.print(Text(info.config_diff.rstrip(), style="yellow"))
        if info.lockfile_note:
            out.print(Text(info.lockfile_note, style="yellow"))
        if info.tests_modified:
            out.print(Text(f"existing tests modified: {', '.join(info.tests_modified)}", style="bold yellow"))
            out.print(Text(info.tests_diff.rstrip(), style="yellow"))
        while True:
            choice = self.repl.read_line("[c]ontinue, [r]edirect, or [s]top? ").strip().lower()
            if choice in ("c", "continue", ""):
                return "continue", None
            if choice in ("s", "stop"):
                return "stop", None
            if choice in ("r", "redirect"):
                text = ""
                while not text:
                    text = self.repl.read_line("guidance for the next iteration: ").strip()
                return "redirect", text


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
