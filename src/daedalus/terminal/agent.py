"""The DeepSeek agent: the daedalus tools and the tool-calling loop for one user turn.

* Two profiles. "chat": exactly the twelve tools in daedalus.toolset.AGENT_NAMES
  (daedalus-mcp's nine plus propose_edit and propose_lesson, which /end needs,
  and ask_user). "coder" (/code <slug>): exactly the fourteen in CODER_NAMES,
  the eight read and propose tools plus edit_code, write_code, delete_code,
  run, find_precedent, and ask_user, working in the session's worktree. None
  takes a source: proposals are made with source="deepseek" here, never by
  the model.
* A successful ask_user ENDS THE TURN: no further model call is made, and
  any later tool calls in the same reply get a "not run" result.
* Each tool result is capped at 60,000 characters, with a note when cut.
* At most 12 tool rounds per user turn; then the turn stops and says so.
* Refusals (PolicyError and friends) go back to the model as the tool
  result, naming the rule, so it can adjust. They don't end the turn.
* Schema errors that read_note reports are also surfaced to the user.
* Every assistant message keeps its reasoning_content, which DeepSeek
  requires on later requests that carry tools.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from daedalus import toolset
from daedalus.core import tools
from daedalus.core.schema import DESIGN_CATEGORIES
from daedalus.core.session import Session

from .config import Config
from .deepseek import DeepSeek, Reply
from .meter import Meter, Reading, metered_call, render
from .prompts import system_prompt

SOURCE = "deepseek"
MAX_TOOL_ROUNDS = 12
MAX_RESULT_CHARS = 60_000

log = logging.getLogger("daedalus.terminal")

_STR = {"type": "string"}
_OPT_STR = {"type": "string"}  # optional: left out of "required"; a null the model sends is dropped
_OPT_INT = {"type": "integer"}

# JSON Schemas for the tool arguments. Property names match daedalus-mcp's
# (a test compares them), and none of them is "source".
PARAMETERS: dict[str, dict[str, Any]] = {
    "get_scope": {"type": "object", "properties": {}},
    "list_notes": {"type": "object", "properties": {
        "folder": {**_OPT_STR, "description": "top-level folder, e.g. 'decisions'"},
        "type": {**_OPT_STR, "description": "note type, e.g. 'practice'"},
        "status": {**_OPT_STR, "description": "draft, proposed, accepted, superseded, or INVALID"},
    }},
    "read_note": {"type": "object", "properties": {
        "ref": {**_STR, "description": "'vault:path/to/note.md' or a bare wikilink target"},
    }, "required": ["ref"]},
    "search": {"type": "object", "properties": {
        "query": {**_STR, "description": "full-text query"},
        "limit": {"type": "integer", "description": f"max hits, default 20, capped at {toolset.MAX_SEARCH}"},
    }, "required": ["query"]},
    "backlinks": {"type": "object", "properties": {
        "ref": {**_STR, "description": "the note, named like read_note's ref"},
    }, "required": ["ref"]},
    "list_files": {"type": "object", "properties": {
        "subdir": {**_STR, "description": "repo-relative folder; empty for the whole repo"},
    }},
    "read_code": {"type": "object", "properties": {
        "rel_path": {**_STR, "description": "repo-relative file path"},
        "start_line": {**_OPT_INT, "description": "1-based first line"},
        "end_line": {**_OPT_INT, "description": "1-based last line, inclusive"},
    }, "required": ["rel_path"]},
    "propose_note": {"type": "object", "properties": {
        "rel_path": {**_STR, "description": "path in the vault, e.g. 'inbox/idea.md'"},
        "content": {**_STR, "description": "the whole note"},
        "summary": {**_STR, "description": "one line, at most 200 characters"},
        "vault": {**_OPT_STR, "description": "'shared' for the shared inbox; omit for the active project"},
    }, "required": ["rel_path", "content", "summary"]},
    "find_precedent": {"type": "object", "properties": {
        "topic": {**_STR, "description": "a few words about the choice, e.g. 'storage database'"},
        "category": {**_OPT_STR, "enum": list(DESIGN_CATEGORIES), "description": "the design category"},
    }, "required": ["topic"]},
    "ask_user": {"type": "object", "properties": {
        "question": {**_STR, "description": "the design question, one sentence"},
        "category": {**_STR, "enum": list(DESIGN_CATEGORIES), "description": "the design category"},
        "options": {"type": "array", "items": _STR, "minItems": 2, "description": "the choices, at least two"},
        "context_refs": {"type": "array", "items": _STR, "minItems": 1,
                         "description": "wikilinks to notes giving context, e.g. ['[[usgs-fetcher]]']"},
    }, "required": ["question", "category", "options", "context_refs"]},
    "propose_edit": {"type": "object", "properties": {
        "rel_path": {**_STR, "description": "the note to replace, e.g. 'CONTEXT.md' or 'inbox/idea.md'"},
        "content": {**_STR, "description": "the complete new file"},
        "summary": {**_STR, "description": "one line, at most 200 characters"},
        "vault": {**_OPT_STR, "description": "'shared' for the shared inbox; omit for the active project"},
    }, "required": ["rel_path", "content", "summary"]},
    "edit_code": {"type": "object", "properties": {
        "rel_path": {**_STR, "description": "worktree-relative file path"},
        "old": {**_STR, "description": "the exact text to replace; must occur exactly once"},
        "new": {**_STR, "description": "the replacement text"},
    }, "required": ["rel_path", "old", "new"]},
    "write_code": {"type": "object", "properties": {
        "rel_path": {**_STR, "description": "worktree-relative file path"},
        "content": {**_STR, "description": "the whole file, at most 64 KB"},
    }, "required": ["rel_path", "content"]},
    "delete_code": {"type": "object", "properties": {
        "rel_path": {**_STR, "description": "worktree-relative file path"},
    }, "required": ["rel_path"]},
    "run": {"type": "object", "properties": {
        "argv": {"type": "array", "items": _STR, "minItems": 1,
                 "description": "the command as a list, e.g. [\"pytest\", \"-q\"]"},
    }, "required": ["argv"]},
    "propose_lesson": {"type": "object", "properties": {
        "rel_path": {**_STR, "description": "file name in the shared inbox/, e.g. 'git-hooks-path.md'"},
        "content": {**_STR, "description": "the whole lesson note"},
        "summary": {**_STR, "description": "one line, at most 200 characters"},
    }, "required": ["rel_path", "content", "summary"]},
}
for _schema in PARAMETERS.values():
    _schema["additionalProperties"] = False

PROFILES: dict[str, tuple[str, ...]] = {"chat": toolset.AGENT_NAMES, "coder": toolset.CODER_NAMES}


def tools_for(profile: str) -> list[dict[str, Any]]:
    return [
        {"type": "function", "function": {
            "name": name, "description": toolset.description(name, SOURCE, profile), "parameters": PARAMETERS[name],
        }}
        for name in PROFILES[profile]
    ]


# The tools arrays, built once in a fixed order: they're part of the cached prefix.
TOOLSETS: dict[str, list[dict[str, Any]]] = {profile: tools_for(profile) for profile in PROFILES}
TOOLS = TOOLSETS["chat"]
PROPOSING = ("propose_note", "propose_edit", "propose_lesson", "ask_user")
ENDS_TURN = "ask_user"
NOT_RUN = "Not run: ask_user ended this turn. Wait for Mohamed's answer."


def call_tool(session: Session, name: str, args: dict[str, Any],
              edit_log: tools.EditLog | None = None) -> dict[str, Any]:
    """Run one tool. source is fixed here; the model can't pass it (the schemas refuse extras)."""
    if name in ("edit_code", "write_code", "delete_code"):
        return getattr(toolset, name)(session, **args, edit_log=edit_log)
    if name in ("propose_note", "propose_edit"):
        fn = getattr(toolset, name)
        return fn(session, SOURCE, args["rel_path"], args["content"], args["summary"], args.get("vault"))
    if name == "propose_lesson":
        return toolset.propose_lesson(session, SOURCE, args["rel_path"], args["content"], args["summary"])
    if name == "ask_user":
        return toolset.ask_user(session, SOURCE, args["question"], args["category"], args["options"],
                                args["context_refs"])
    fn = getattr(toolset, name)
    return fn(session, **args)


def cap_result(text: str) -> str:
    if len(text) <= MAX_RESULT_CHARS:
        return text
    note = (f"\n[truncated: showing {MAX_RESULT_CHARS:,} of {len(text):,} characters. Narrow the request, "
            "e.g. read_code with start_line/end_line or list_files with subdir.]")
    return text[:MAX_RESULT_CHARS] + note


@dataclass
class ToolOutcome:
    name: str
    args: dict[str, Any]
    ok: bool
    text: str  # what goes back to the model
    schema_errors: list[str] = field(default_factory=list)
    result: dict[str, Any] | None = None


def run_tool(session: Session, name: str, raw_args: str, profile: str = "chat",
             edit_log: tools.EditLog | None = None) -> ToolOutcome:
    """Execute one model tool call; every failure becomes a tool result, never an exception."""
    names = PROFILES[profile]
    if name not in names:
        return ToolOutcome(name, {}, False, f"Unknown tool {name!r}. Tools: {', '.join(names)}.")
    try:
        args = json.loads(raw_args or "{}")
        if not isinstance(args, dict):
            raise ValueError("arguments must be a JSON object")
        args = {k: v for k, v in args.items() if v is not None}  # null means "not given"
    except ValueError as e:
        return ToolOutcome(name, {}, False, f"Arguments for {name} aren't valid JSON ({e}); send a JSON object.")
    allowed = set(PARAMETERS[name]["properties"])
    extra = sorted(set(args) - allowed)
    missing = [k for k in PARAMETERS[name].get("required", []) if k not in args]
    if extra or missing:
        parts = []
        if extra:
            parts.append(f"unknown argument(s) {', '.join(extra)}")
        if missing:
            parts.append(f"missing {', '.join(missing)}")
        return ToolOutcome(name, args, False, f"{name}: {'; '.join(parts)}. Allowed: {', '.join(sorted(allowed))}.")
    try:
        result = call_tool(session, name, args, edit_log)
    except Exception as e:
        text = toolset.error_text(e)
        if text is None:
            log.exception("tool %s failed", name)
            text = f"internal error in {name} ({type(e).__name__}: {e})"
        return ToolOutcome(name, args, False, text)
    errors = list(result.get("schema_errors") or []) if name == "read_note" else []
    return ToolOutcome(name, args, True, cap_result(json.dumps(result, ensure_ascii=False)), errors, result)


def tool_label(name: str, args: dict[str, Any]) -> str:
    """The one-line form shown in the terminal: → read_note shared:practices/red-test-first.md"""
    main = {"read_note": "ref", "backlinks": "ref", "search": "query", "list_files": "subdir",
            "read_code": "rel_path", "list_notes": "folder", "edit_code": "rel_path", "write_code": "rel_path",
            "delete_code": "rel_path"}.get(name)
    if name == "run":
        argv = args.get("argv") or []
        return f"→ run {' '.join(str(a) for a in argv)}".rstrip()
    if name in ("propose_note", "propose_edit"):
        target = f"{args.get('vault') or ''}:{args.get('rel_path', '')}".lstrip(":")
        return f"→ {name} {target}"
    if name == "propose_lesson":
        return f"→ propose_lesson shared:inbox/{args.get('rel_path', '')}"
    value = args.get(main) if main else None
    return f"→ {name} {value}" if value else f"→ {name}"


class Events:
    """What the REPL shows. Every method is optional to override."""

    def call_start(self) -> None: ...
    def delta(self, kind: str, text: str) -> None: ...
    def reading(self, reading: Reading) -> None: ...
    def call_done(self, reply: Reply, reading: Reading) -> None: ...
    def tool(self, outcome: ToolOutcome) -> None: ...
    def schema_errors(self, ref: str, errors: list[str]) -> None: ...
    def stopped(self, message: str) -> None: ...
    def asked(self, result: dict[str, Any]) -> None: ...


@dataclass
class TurnResult:
    answer: str
    rounds: int  # tool rounds run
    stopped: bool  # hit MAX_TOOL_ROUNDS
    asked: dict[str, Any] | None = None  # ask_user's result, when it ended the turn


class Agent:
    """A conversation: the fixed system prompt, history, and the metered calls."""

    def __init__(self, session: Session, ds: DeepSeek, cfg: Config, session_id: str,
                 meter: Meter | None = None, profile: str = "chat", run_id: str | None = None,
                 run_cap: float | None = None) -> None:
        if profile not in PROFILES:
            raise ValueError(f"unknown agent profile {profile!r}")
        if profile == "coder" and session.worktree is None:
            raise ValueError("the coder profile needs a session with a worktree")
        self.session = session
        self.ds = ds
        self.cfg = cfg
        self.session_id = session_id
        self.meter = meter or Meter()
        self.profile = profile
        self.run_id, self.run_cap = run_id, run_cap  # a goal run's tag and budget, checked before every call
        self.tools = TOOLSETS[profile]
        self.system = system_prompt(session, profile)
        self.history: list[dict[str, Any]] = []
        self.tool_calls = 0  # tools run this conversation
        self.proposals: list[str] = []  # ids of proposals made this conversation, in order
        self.edit_log = tools.EditLog()  # this conversation's code edits (coder mode)
        self.runs: list[dict[str, Any]] = []  # this conversation's run results: exit code, duration, timed out

    def rebuild(self, session: Session) -> None:
        """Point at a new session (a /pull or /drop) and rebuild the system prompt; history is kept."""
        self.session = session
        self.system = system_prompt(session, self.profile)

    def one_shot(self, system: str, user: str) -> str:
        """One metered call outside the conversation, with no tools: it can't act, and history is untouched."""
        reply, reading = metered_call(self.ds, self.meter, self.cfg, self.session_id,
                                      [{"role": "system", "content": system}, {"role": "user", "content": user}],
                                      None, run_id=self.run_id, run_cap=self.run_cap)
        log.info("session %s one-shot call: %s", self.session_id, render(reading))
        return reply.content.strip()

    def messages(self) -> list[dict[str, Any]]:
        return [{"role": "system", "content": self.system}, *self.history]

    def turn(self, text: str, events: Events | None = None) -> TurnResult:
        """Answer one user message, running tools as the model asks, at most MAX_TOOL_ROUNDS rounds.

        If anything raises (a cap, an API error, an interrupt), the turn is
        rolled back out of history so the next turn starts clean.
        """
        ev = events or Events()
        mark = len(self.history)
        self.history.append({"role": "user", "content": text})
        try:
            rounds = 0
            while True:
                ev.call_start()
                reply, reading = metered_call(
                    self.ds, self.meter, self.cfg, self.session_id, self.messages(), self.tools,
                    on_reading=ev.reading, on_delta=ev.delta, run_id=self.run_id, run_cap=self.run_cap,
                )
                ev.call_done(reply, reading)
                log.info("session %s call: %s", self.session_id, render(reading))
                self.history.append(reply.message())
                calls = reply.ordered_tool_calls()
                if not calls:
                    return TurnResult(reply.content, rounds, False)
                asked: dict[str, Any] | None = None
                for call in calls:
                    if asked is not None:  # ask_user ended the turn; every call still needs a tool result
                        self.history.append({"role": "tool", "tool_call_id": call.id, "content": NOT_RUN})
                        continue
                    outcome = run_tool(self.session, call.name, call.arguments, self.profile, self.edit_log)
                    if outcome.ok and outcome.name == "run" and outcome.result:
                        self.runs.append({k: outcome.result[k] for k in ("exit_code", "duration_s", "timed_out")}
                                         | {"argv": outcome.args.get("argv")})
                    log.info("session %s %s %s", self.session_id, tool_label(outcome.name, outcome.args),
                             "ok" if outcome.ok else f"refused: {outcome.text.splitlines()[0][:200]}")
                    ev.tool(outcome)
                    self.tool_calls += 1
                    if outcome.ok and outcome.name in PROPOSING and outcome.result:
                        self.proposals.append(outcome.result["id"])
                    if outcome.schema_errors:
                        ev.schema_errors(str(outcome.args.get("ref", "")), outcome.schema_errors)
                    self.history.append({"role": "tool", "tool_call_id": call.id, "content": outcome.text})
                    if outcome.ok and outcome.name == ENDS_TURN:
                        asked = outcome.result
                rounds += 1
                if asked is not None:  # no further model call this turn
                    ev.asked(asked)
                    return TurnResult(reply.content, rounds, False, asked)
                if rounds >= MAX_TOOL_ROUNDS:
                    message = (f"Stopped after {MAX_TOOL_ROUNDS} tool rounds without an answer. Ask again more "
                               "narrowly, or say what to look at.")
                    ev.stopped(message)
                    return TurnResult(reply.content, rounds, True)
        except BaseException:
            del self.history[mark:]
            raise
