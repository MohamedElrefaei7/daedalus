"""The Obsidian bridge: /open shows a note in Obsidian, /here brings Obsidian's open note into the conversation.

Obsidian is asked *where*, never *what*. It tells us which vault folder and
which file are focused; every byte of content is read through daedalus.core
and its path policy, exactly as read_note reads it. The CLI's own read is
never used.

Two traps shape the code:

* Every project vault is a folder named ``vault``, and Obsidian names a vault
  after its folder, so several Obsidian vaults share the name "vault". Nothing
  here targets a vault by name: /open uses ``obsidian://open?path=<absolute
  path>``, and /here maps the reported base path to a registered vault by
  real path.
* The CLI launches Obsidian when it isn't running. /here must not, so it checks
  ``pgrep -x Obsidian`` first and only then runs the CLI. /open may launch it:
  you asked to see the note.

The CLI's output is untrusted (some 1.12 CLI calls silently returned wrong or
empty data), so each way it can be wrong gets its own error, never a guess.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from daedalus.core import tools
from daedalus.core.notes import Note
from daedalus.core.policy import Op, PolicyError, authorize
from daedalus.core.registry import SHARED_NAME, Registry
from daedalus.core.session import Session
from daedalus.toolset import refusal

CLI = "obsidian"
APP_PROCESS = "Obsidian"
CLI_TIMEOUT_S = 5.0
# A working directory inside a vault makes the CLI pick that vault; "/" is inside none, so it answers
# for the vault you focused last.
NEUTRAL_CWD = "/"
EVAL_CODE = ("JSON.stringify({base: app.vault.adapter.getBasePath(), "
             "file: app.workspace.getActiveFile()?.path ?? null})")
# `obsidian eval` prints its result as "=> <value>" and exits 0, errors included ("Error: ...",
# "Vault not found."), so the exit code can't be trusted and the prefix is required (checked on 1.15.1).
EVAL_PREFIX = "=> "
MAX_ATTACH_CHARS = 60_000

SETUP_STEPS = (
    "The `obsidian` command isn't on your PATH. Turn on Obsidian's CLI once: in Obsidian, Settings → General → "
    "Advanced → Command line interface → turn it on and register the command. Then open a new terminal and check "
    "that `obsidian version` works."
)


# Results of active_note ------------------------------------------------------------------

@dataclass(frozen=True)
class Problem:
    """/here couldn't attach a note; ``message`` says why and what to do."""

    message: str


class NotOpen(Problem):
    """Obsidian isn't running; the CLI was not called (it would have launched the app)."""


class CliMissing(Problem):
    """No ``obsidian`` on PATH."""


class CliFailed(Problem):
    """The CLI exited non-zero, timed out, or couldn't be run."""


class NotJson(Problem):
    """The CLI's output isn't the JSON object the eval expression prints."""


class BadBase(Problem):
    """``base`` isn't an existing directory."""


class NoActiveFile(Problem):
    """``file`` is null: no note is open in the focused vault."""


class Unregistered(Problem):
    """``base`` is a folder the registry doesn't know."""


class NotInScope(Problem):
    """``base`` is a registered project that isn't active or pulled."""


class Refused(Problem):
    """The path policy refused the file (the message names the rule)."""


class FileMissing(Problem):
    """``base`` + ``file`` doesn't exist."""


class NotANote(Problem):
    """The focused file isn't a readable markdown note (a canvas, a PDF, non-UTF-8 text)."""


@dataclass(frozen=True)
class Here:
    """The note open in Obsidian, read through daedalus.core."""

    vault: str
    rel_path: str
    text: str  # the whole file, as read after the policy check
    note: Note
    schema_errors: list[str] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return f"{self.vault}:{self.rel_path}"


# The outside world, injectable for tests --------------------------------------------------

def is_running() -> bool:
    """True if the Obsidian app is running. Never launches it."""
    try:
        return subprocess.run(["pgrep", "-x", APP_PROCESS], capture_output=True, timeout=CLI_TIMEOUT_S).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def run_open(argv: list[str]) -> None:
    """Run macOS ``open``; raises ObsidianError if it fails."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ObsidianError(f"couldn't run {argv[0]}: {e}") from None
    if done.returncode != 0:
        raise ObsidianError(f"{argv[0]} failed: {(done.stderr or done.stdout).strip() or f'exit {done.returncode}'}")


@dataclass
class Bridge:
    """The three things that touch the machine. Looked up at call time, so tests can patch the module."""

    running: Callable[[], bool] = field(default_factory=lambda: (lambda: is_running()))
    opener: Callable[[list[str]], None] = field(default_factory=lambda: (lambda argv: run_open(argv)))
    which: Callable[[str], str | None] = field(default_factory=lambda: (lambda name: shutil.which(name)))


class ObsidianError(Exception):
    """/open couldn't show the note."""


# /open -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Opened:
    vault: str
    rel_path: str
    path: Path
    uri: str


def open_uri(path: Path) -> str:
    """``obsidian://open?path=...``: Obsidian picks the vault that contains the path, so names never matter."""
    return "obsidian://open?path=" + urllib.parse.quote(str(path), safe="")


def open_note(session: Session, ref: str, bridge: Bridge | None = None) -> Opened:
    """Show ``ref`` (``vault:rel/path.md`` or a wikilink target) in Obsidian.

    Resolved and READ-authorized through daedalus.core like read_note, so an
    ambiguous target, a vault out of scope, or an off-limits path is refused
    before anything runs. Launching Obsidian is fine here.
    """
    bridge = bridge or Bridge()
    match = tools.resolve_ref(session, ref)
    auth = authorize(session.vault_root(match.vault) / match.rel_path, Op.READ, session)
    if not auth.path.is_file():
        raise ObsidianError(f"{match.vault}:{match.rel_path} doesn't exist")
    uri = open_uri(auth.path)
    bridge.opener(["open", uri])
    return Opened(match.vault, match.rel_path, auth.path, uri)


# /here -----------------------------------------------------------------------------------

def ask_cli(cli: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([cli, "eval", f"code={EVAL_CODE}"], cwd=NEUTRAL_CWD, capture_output=True, text=True,
                          timeout=CLI_TIMEOUT_S)


def parse_eval(stdout: str):
    """The value of one ``=> <json>`` line, or None for anything else (an error, a warning, nothing)."""
    lines = stdout.strip().splitlines()
    if len(lines) != 1 or not lines[0].startswith(EVAL_PREFIX):
        return None
    try:
        return json.loads(lines[0][len(EVAL_PREFIX):])
    except ValueError:
        return None


def _excerpt(text: str, limit: int = 200) -> str:
    text = text.strip()
    if not text:
        return "(no output)"
    return repr(text if len(text) <= limit else text[:limit] + "…")


def _vault_for(base: Path, registry: Registry) -> str | None:
    """The registered vault whose root is ``base``, compared by real path (never by folder name)."""
    real = Path(os.path.realpath(base))
    for name, root in [(SHARED_NAME, registry.shared), *registry.projects.items()]:
        if real == root or os.path.samefile(real, root):
            return name
    return None


def active_note(session: Session, bridge: Bridge | None = None, registry: Registry | None = None) -> Here | Problem:
    """The note focused in Obsidian, read through daedalus.core, or the Problem that stopped it.

    ``registry`` defaults to the session's; the REPL passes a freshly loaded
    one so a project registered since it started is recognised.
    """
    bridge = bridge or Bridge()
    registry = registry or session.registry

    # 1. Never launch Obsidian to answer "where am I": check it's running before touching the CLI.
    if not bridge.running():
        return NotOpen("Obsidian isn't open, so there's no note to bring in. Open the note in Obsidian, then /here.")
    # 2.
    cli = bridge.which(CLI)
    if cli is None:
        return CliMissing(SETUP_STEPS)
    # 3.
    try:
        done = ask_cli(cli)
    except subprocess.TimeoutExpired:
        return CliFailed(f"`obsidian eval` didn't answer within {CLI_TIMEOUT_S:g} s; nothing was attached.")
    except OSError as e:
        return CliFailed(f"couldn't run {cli}: {e}")
    if done.returncode != 0:
        return CliFailed(f"`obsidian eval` failed (exit {done.returncode}): {_excerpt(done.stderr or done.stdout)}")

    # 4. Untrusted output: each failure is its own error.
    data = parse_eval(done.stdout)
    if not isinstance(data, dict) or "base" not in data or "file" not in data:
        return NotJson(f"Obsidian's CLI didn't answer with the expected JSON ({{base, file}}); it printed "
                       f"{_excerpt(done.stdout)}. Nothing was attached.")
    base, file = data["base"], data["file"]
    if not isinstance(base, str) or not base or not os.path.isabs(base) or not os.path.isdir(base):
        return BadBase(f"Obsidian reported a vault folder that isn't an existing directory: {base!r}. "
                       "Nothing was attached.")
    if file is None:
        return NoActiveFile("No note is open in the Obsidian vault you focused last. Click into a note, then /here.")
    if not isinstance(file, str) or not file:
        return NotJson(f"Obsidian's CLI reported a file that isn't a path: {file!r}. Nothing was attached.")

    # 5. Which registered vault, by real path.
    name = _vault_for(Path(base), registry)
    if name is None:
        return Unregistered(f"The Obsidian vault at {base} isn't registered with daedalus, so it can't be read. "
                            "Register it with /new, or focus a registered vault's window.")
    in_scope = {vault for vault, _ in session.scope}
    if name not in in_scope:
        return NotInScope(f"The note is in {name}, which isn't in this session. /pull {name} would bring it in "
                          "(read-only).")

    # 6. Read through daedalus.core: the same policy and schema reporting as read_note.
    root = session.vault_root(name)
    try:
        auth = authorize(root / file, Op.READ, session)
    except PolicyError as e:
        return Refused(refusal(e))
    if not auth.path.is_file():
        return FileMissing(f"Obsidian reported {name}:{file}, but {auth.path} doesn't exist. Nothing was attached.")
    if auth.path.suffix.lower() != ".md":
        return NotANote(f"{name}:{file} isn't a markdown note, so /here can't attach it.")
    rel = auth.path.relative_to(root).as_posix()
    try:
        note, errors = tools.read_note(session, f"{name}:{rel}", strict=False)
        text = note.path.read_text(encoding="utf-8")
    except PolicyError as e:
        return Refused(refusal(e))
    except UnicodeDecodeError:
        return NotANote(f"{name}:{rel} isn't UTF-8 text, so /here can't attach it.")
    return Here(name, rel, text, note, list(errors))


def attachment(here: Here) -> str:
    """The block prefixed to your next message: a fence headed by the vault-qualified path, capped."""
    text = here.text
    note = ""
    if len(text) > MAX_ATTACH_CHARS:
        note = (f"\n[truncated: showing {MAX_ATTACH_CHARS:,} of {len(text):,} characters; read_note {here.ref} "
                "for the rest]")
        text = text[:MAX_ATTACH_CHARS]
    longest = max((len(run) for run in _backtick_runs(text)), default=0)
    fence = "`" * max(3, longest + 1)  # longer than any fence inside the note
    body = text if text.endswith("\n") else text + "\n"
    return f"{here.ref} (the note open in Obsidian, attached with /here):\n{fence}markdown\n{body}{fence}{note}"


def _backtick_runs(text: str):
    run = 0
    for ch in text:
        if ch == "`":
            run += 1
        elif run:
            yield "`" * run
            run = 0
    if run:
        yield "`" * run
