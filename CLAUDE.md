# Daedalus — build contract

Daedalus is a personal knowledge-graph and agent system built on Obsidian. Each project gets a vault of markdown notes (goals, decisions, components, experiments, concepts, plans) that agents read before they act. Agents propose changes, and the human approves them or promotes them into accepted notes. A separate shared vault holds practices and lessons that apply across projects. Later commits build the CLI, the safety core, and the MCP server for Claude on top of these vaults.

## ★ Naming contract

Every commit uses these names. Don't invent alternatives.

| Thing | Name |
|---|---|
| CLI command | `daedalus` |
| Python package | `daedalus`, with the safety core in `daedalus.core` |
| MCP server for Claude | `daedalus-mcp` |
| Global config | `~/.config/daedalus/` (vault registry: `vaults.toml`) |
| Per-vault runtime state | `.daedalus/` inside each vault, git-ignored |
| Shared vault | `~/daedalus/shared/` |

## Repo layout

```
daedalus/
├── CLAUDE.md                 # this file
├── CONTEXT.md                # running log, key decisions, Up Next
├── pyproject.toml            # hatchling build, src/ layout; deps pyyaml, fastmcp (pinned), openai, keyring, prompt_toolkit, rich, markitdown (pinned, 4 local extras)
├── uv.lock
├── config/
│   └── vaults.example.toml   # shape of ~/.config/daedalus/vaults.toml
├── scripts/
│   └── smoke_read.py         # live read-layer smoke test against the real registry
├── src/daedalus/
│   ├── mcp_server.py         # daedalus-mcp: the stdio MCP server for the Claude desktop app
│   ├── toolset.py            # the agent tools: daedalus-mcp (nine), the terminal agent (twelve; fourteen in coder mode)
│   ├── secrets.py            # API keys in the macOS Keychain (`python -m daedalus.secrets set|status`)
│   ├── queue_cli.py          # debug CLI for the queue (list/diff/apply/reject); the REPL replaces it
│   ├── state.py              # `python -m daedalus.state` entry point (logic in core/state.py)
│   ├── sandbox/              # every command an agent triggers runs here, under Seatbelt (commit 10a)
│   │   ├── runner.py         # run(session, argv, worktree) -> RunResult: checks, preflight, venv, execute
│   │   ├── profile.py        # the SBPL profile; every path a realpath passed with -D
│   │   ├── exec.py           # the one place a process is spawned: own session, killpg, rlimits, output tails
│   │   ├── venv.py           # ~/.cache/daedalus/venvs/<project>-<sha12 of uv.lock>, via uv sync --frozen
│   │   ├── config.py         # daedalus.toml ([sandbox] allow, timeout_s, allow_localhost)
│   │   └── __main__.py       # python -m daedalus.sandbox run --project <name> -- <argv>
│   ├── terminal/             # `daedalus`: the REPL (commit 5; Textual UI in commit 14)
│   │   ├── repl.py           # prompt_toolkit input, rich Live output, /commands; main() is the script
│   │   ├── agent.py          # tool specs, the 12-round tool loop, reasoning_content kept in history
│   │   ├── deepseek.py       # OpenAI SDK client for DeepSeek: streaming, accumulation, errors, retries
│   │   ├── meter.py          # token estimates, worst-case cap check, the meter line, interrupted calls
│   │   ├── ledger.py         # spend ledger (SQLite, WAL): reservations, spent(), caps
│   │   ├── pricing.py        # the price table, peak windows (UTC), cost(), worst_case()
│   │   ├── prompts.py        # the byte-stable system prompt
│   │   ├── obsidian.py       # the Obsidian bridge: /open by path, /here via `obsidian eval`
│   │   ├── loop.py           # the coding loop: /goal, iterations, check-ins, run state, report, lessons
│   │   └── config.py         # ~/.config/daedalus/config.toml: model, effort, max_tokens, caps
│   └── core/                 # the safety core
│       ├── schema.py         # SCHEMA: single source of truth for frontmatter
│       ├── registry.py       # vaults.toml loading + validation; register/unregister_project (tomlkit)
│       ├── session.py        # Session dataclass, Area enum
│       ├── state.py          # state.json -> Session (load_session, save_state, set_active/pull/drop) + CLI
│       ├── lifecycle.py      # /new: vault from template, path-limited init commit, register; full rollback
│       ├── policy.py         # Op enum, authorize(), authorize_command() (rule 7, pinned to the venv): the seven rules
│       ├── notes.py          # frontmatter parsing + schema validation (NoteSchemaError)
│       ├── links.py          # wikilink extraction + Obsidian-style resolution
│       ├── index.py          # SQLite FTS5 search index
│       ├── errors.py         # ProposalError and its subclasses
│       ├── vaultio.py        # write lock, fd-relative file ops, path-limited git commits
│       ├── queue.py          # proposal queue (SQLite, WAL) + crash reconciliation
│       ├── apply.py          # apply, apply_changes (several paths, one commit), reject, pending, diff
│       ├── gate.py           # the design-decision gate: check_basis, find_precedent, propose_with_basis
│       ├── accept.py         # ask_user's question, /answer, /promote
│       ├── ingest.py         # /add: local documents into sources/ as markdown (MarkItDown)
│       ├── worktree.py       # agent worktrees: create, list, diff, commit, merge (yours), drop
│       ├── goals.py          # goal notes: the launch rule, done_when under rule 7, pytest counts, honest-done guards
│       └── tools.py          # note read tools; list_files/read_code; propose_*; write_log; edit/write/delete_code
├── templates/
│   ├── project-vault/        # copied wholesale to create a project vault
│   └── shared-vault/         # copied to create ~/daedalus/shared/
└── tests/
    ├── test_templates.py     # template contract test
    ├── core/                 # one file per core part, test_bench.py, test_write_integrity.py
    ├── mcp/                  # daedalus-mcp via FastMCP's in-memory client, plus a stdio subprocess test
    ├── sandbox/              # runner unit tests (fake executor); test_live.py is marked `sandbox`
    └── terminal/             # recorded SSE streams through httpx2.MockTransport; no live calls

Test helpers live in plain modules (`tests/terminal/fakes.py`, `tests/mcp/mcp_helpers.py`), not in conftest:
pytest binds `conftest` to whichever folder's it imported last. Every conftest re-exports the core helpers.
```

Run the tests with `uv run pytest`; `--durations=10` is in the default addopts, so the ten slowest tests always print. The default addopts also deselect the live sandbox tests: run them on purpose with `uv run pytest -m sandbox`. They run the real `sandbox-exec` with scratch2's real venv, and **fail, never skip**, if the sandbox is unavailable.

## Templates are the source of truth

`templates/project-vault/` and `templates/shared-vault/` are the source of truth for vault structure: folders, `AGENTS.md`, `.gitignore`, and one note template per type. Change vault structure there and nowhere else. `tests/test_templates.py` guards them. The `SCHEMA` dict in `daedalus.core.schema` validates both the templates and live notes, and must agree with the block below.

## Path policy

Every read and write goes through `daedalus.core.policy.authorize(path, op, session)`, which resolves the path with `os.path.realpath`, classifies it into exactly one area, and raises `PolicyError(rule, path, reason)` on refusal.

**Deny by default.** Every op has an explicit list of allowed areas and folders, and anything not listed is refused. Never write "allow unless it's X".

`Op`: `READ`, `PROPOSE`, `CREATE_LOG`, `WRITE_PLAN`, `USER_ACTION`, `WRITE_CODE`.

1. **Location.** Inside a registered vault, the active repo, or the worktree. The repo is read-only; its only write is `WRITE_CODE` in the worktree.
2. **Shared vault.** `READ`; `PROPOSE` into `inbox/`; `USER_ACTION` into `practices/` and `lessons/`.
3. **Pulled vaults.** `READ` only.
4. **Off limits for every op.** Any `.obsidian` or `.git` component (case-folded), and anything in `<worktree>/vault/`.
5. **Active vault writes.** `PROPOSE`: `inbox/`, `questions/`, `CONTEXT.md`. `CREATE_LOG`: new files in `logs/`. `WRITE_PLAN`: `plans/`. `USER_ACTION`: `goals/`, `sources/`, `decisions/`, `components/`, `experiments/`, `concepts/`, `questions/` (since commit 8, so `/answer` can mark a question answered), `CONTEXT.md`. Never `AGENTS.md` or `templates/`. Agents still can't write `decisions/`: `PROPOSE` is refused there.
6. **Code.** `WRITE_CODE` only in the worktree, never in your own checkout.
7. **Commands.** `authorize_command(argv, project_config, venv) -> Path`. `argv` is a non-empty list of strings with no NUL bytes; `argv[0]` is a bare name (no `/`) in `daedalus.toml`'s `allow`; and it runs **only** as `<venv>/bin/<name>`, which must exist and be executable. The host's `PATH` never matters, so `/tmp/evil/pytest` is unreachable. The pinned path is returned unresolved, because a venv's `bin/python` is a symlink and Python finds its venv from the path it was started as.

The `Session` is passed in explicitly to every core function. Only `daedalus.core.state` reads `~/.config/daedalus/state.json` (`{"active": "scratch", "pulled": []}`), and only front ends call it: `load_session()` builds a Session or raises `StateError` saying how to fix it; `save_state()` validates against the registry, then writes atomically. `python -m daedalus.state show | set-active <name> | pull <name> | drop <name>` is the stopgap until commit 6's `/vault` and `/pull`.

## Search index

One SQLite FTS5 database per vault at `~/.cache/daedalus/index/<sha1 of vault realpath>.sqlite`, never inside a vault. It's refreshed before each query by walking the vault and comparing each file's `(mtime_ns, size)`. The vault's top-level `templates/` holds templates, not notes: one walk (`index.walk_markdown`) skips it for search, link resolution, backlinks, and `list_notes` alike. A template stays readable by explicit path.

## Frontmatter schema

```yaml
# Base keys — required on every note type
type:          # must equal the template's filename stem
status:        # one of: draft, proposed, accepted, superseded
created:       # "{{date}}" in templates, filled by Obsidian
related:       # list of quoted wikilinks
shared_refs:   # list of quoted wikilinks into the shared vault
project_refs:  # list of quoted wikilinks into other projects
source:        # one of: human, deepseek, claude

# Extra required keys by type
component:  code              # list of repo-relative paths that implement it
goal:       done_when, max_iterations, budget_usd, checkin_every, protected
question:   options, answered # answered: false until resolved
plan:       spec, decisions, tasks
practice:   applies_to
lesson:     applies_to
```

## Rule

Every invariant gets a test that fails when the invariant is broken.

## Writes: proposals, the queue, apply

Agents never write a vault directly. `propose_note`, `propose_edit`, and `propose_lesson` validate the draft against `SCHEMA` and write only to the queue. **The link rule.** A typed draft must link at least one other note. A typed draft is one for a typed folder, or one that declares a type elsewhere, such as an `inbox/` draft. Any of `related`, `shared_refs`, `project_refs`, or a body link counts, dangling links included (the note may not be written yet). A link to `AGENTS.md` or `CONTEXT.md` is refused, whether it resolves to one or just names one. A link that matches more than one note in its scope is refused too, and the error names the matches and their path-qualified forms (`[[decisions/x]]`). **Source is provenance.** A create's frontmatter `source` must equal the proposal's source. An edit may change it only when the proposal's source is `human`, so an agent may edit a human's draft but can't re-attribute it. The queue is SQLite at `~/.local/state/daedalus/queue.sqlite`, outside every vault, in WAL mode with short transactions. Content is capped at 256 KB.

**A write that isn't committed shouldn't exist.** `daedalus.core.apply.apply(session, change_id)`, in order:

1. Load the record; refuse unless it is `pending`, so a second apply is refused.
2. Re-authorize against the session passed in now, not the propose-time check.
3. Stale check. An edit's target must still hash to `base_sha256`; a create's target must still not exist. Otherwise mark it `stale` and refuse.
4. Mark the proposal `applying`, recording the target, its repo, and HEAD. Then write atomically, relative to a directory fd opened with `O_NOFOLLOW`: temp file, `fsync`, `os.replace`. Confirm `realpath(target)` is still the authorized path, or restore and refuse.
5. Commit exactly that path with `git commit --only -- <path>`. Never `-a`, never `git add .`.
6. If the commit fails, restore the previous content (or remove a create) and mark it `failed` with git's output.
7. Mark it `applied` and store `commit_sha`.

The commit message is `agent(<source>): <summary>`, a blank line, then the trailer `Daedalus-Change: <id>`. `write_log` creates `logs/<run_id>.md` with an exclusive create (`open(..., "x")`) and commits it at once as `log(<source>): <run_id>`; no approval is needed. Every vault write, whether apply, reject, or `write_log`, holds one write lock. It's an OS lock (`fcntl.flock`) on `~/.local/state/daedalus/write.lock`: the kernel releases it when the holder exits, crashes included, and the file's existence means nothing.

**Crash recovery.** Every queue open reconciles `applying` records whose apply isn't running; "not running" means the write lock is free. If a commit with the proposal's `Daedalus-Change` trailer exists, the record is marked `applied` with that sha. Otherwise the old content is restored (or a create removed), the path unstaged, and the record marked `failed` with reason `interrupted`. A file changed after the crash is left alone.

A refused write leaves every vault's digest, every repo's HEAD, and the queue unchanged; `tests/core/test_write_integrity.py` checks this for every refusal path.

## daedalus-mcp

The MCP server for the Claude desktop app, in `src/daedalus/mcp_server.py` (FastMCP, pinned in `pyproject.toml`), run over stdio as `daedalus-mcp`. It's a thin layer over `daedalus.core`: every tool goes through the same session, path policy, and queue. The tool bodies and descriptions live in `daedalus.toolset`, shared with the terminal agent.

**The exact tool set.** `get_scope`, `list_notes`, `read_note`, `search`, `backlinks`, `list_files`, `read_code`, `propose_note`, `find_precedent` (nine). Nothing else: never `apply`, `reject`, `propose_edit`, `ask_user`, `write_log`, or anything touching queue state. `tests/mcp/test_server.py::test_exact_tool_set` fails if a tool is added or removed.

- **The server sets `source="claude"`.** `propose_note` has no `source` parameter, and its input schema refuses extra arguments. With the source rule, a draft saying `source: human` is refused, so Claude can't present its work as the human's.
- **Never write to stdout.** Over stdio, stdout carries only JSON-RPC; one stray `print()` corrupts the stream. Logging goes to stderr and to `~/.local/state/daedalus/mcp.log`; the FastMCP banner is off. `test_stdout_is_only_json_rpc` runs the real script and parses every stdout line.
- **The session is loaded on every tool call, never cached,** so `python -m daedalus.state set-active <name>` takes effect on Claude's next call without restarting the app.
- **Refusals are tool errors, never crashes.** `PolicyError` becomes "Refused by rule N: <reason>. (path: ...)"; `NoteSchemaError`, `DraftInvalid` and other `ProposalError`s, `StateError`, `RegistryError`, `RefError`, and `ReadError` pass their messages through; anything else is logged with its traceback and returned as an internal error. Refusals are logged at INFO (FastMCP's own line for them is demoted to DEBUG), so ERROR in `mcp.log` always means a fault.
- **Caps protect usage:** `read_code` returns at most 2,000 lines and 200,000 characters per call (page with `start_line`), `list_files` and `list_notes` at most 2,000 entries, `search` at most 50 hits. `read_code` refuses binary files (a NUL byte in the first 8 KB) and anything outside the active repo area, including its `vault/`.

The desktop app's entry, merged into `~/Library/Application Support/Claude/claude_desktop_config.json` under `mcpServers` with absolute paths (the app doesn't expand `~`):

```json
"daedalus": {
  "command": "/opt/homebrew/bin/uv",
  "args": ["--directory", "/Users/<you>/code/daedalus", "run", "daedalus-mcp"]
}
```

`daedalus-mcp --registry <toml> --state <json> --log-file <path>` overrides the defaults; tests use these.

## The terminal: `daedalus`

`daedalus [project]` sets the active project (like `python -m daedalus.state set-active`), then starts a plain REPL: `prompt_toolkit` input with history, `rich` `Live` output. A DeepSeek agent answers with the daedalus tools. Commands:
- `/new <name> --repo <path>`, `/vaults`, `/vault <name>`, `/pull <name>`, `/drop <name>`, `/end`;
- `/open <note>`, `/here`, `/add <file>`;
- `/answer <question-id> <option-number | "free text">`, `/promote <inbox-note> [folder]`;
- `/wt new <slug>`, `/wt list`, `/wt diff <slug>`, `/wt merge <slug>`, `/wt drop <slug> [--force]`, `/code <slug>`, `/code off`;
- `/goal <note>`, `/goal resume [guidance]`, `/goal list`, `/review lessons`;
- `/cost`, `/pending`, `/diff <id>`, `/approve <id>`, `/reject <id> <reason>`, `/help`, `/quit`.

`queue_cli` stays as a debug tool. The REPL changes the session only through its own commands, which write `state.json` through `daedalus.core.state`; `daedalus-mcp` follows on its next call.

**Switching.**
- `/vault <name>` starts a **fresh conversation**: history is cleared, the system prompt is rebuilt, and the REPL says so. History about project A mustn't carry into B. The session id is kept, so the session spend cap carries over: it belongs to this terminal, not to a project.
- `/pull` and `/drop` change `pulled` and rebuild the system prompt, keeping history. Pulled projects stay read-only (rule 3).

**`/new <name> --repo <path>`** (`daedalus.core.lifecycle`), in order:
1. The name is valid under the registry's rules and not registered yet.
2. `--repo` is an existing git repo's top level, with no `.obsidian/` at its root (refused with the commit-1 message).
3. `<repo>/vault/` doesn't exist, or is empty.
4. `templates/project-vault/` is copied into `<repo>/vault/`.
5. Exactly `vault/` is committed (`git add -- vault`, then `git commit --only -- vault`) as `init: daedalus vault from template`. Anything else staged or modified in that repo stays as it was.
6. The project is registered.
7. The manual Obsidian step is printed: Open folder as vault on `<repo>/vault`, then turn on Templates, set Deleted files to system trash, and make `created` a Date.

Any failure rolls back in reverse. Each undo is armed before its step and tolerates a half-done step:
- unregister;
- undo the init commit, only if it's still HEAD and touches only `vault/`: a soft reset (or deleting HEAD for a first commit), then `vault/` alone is dropped from the index;
- remove the copied folder.

**The registry** is written only by `registry.register_project` and `unregister_project` (only `/new` calls them). They edit with `tomlkit`, so comments, key order, and other entries survive byte for byte. They validate the new text with `load_registry` before an atomic replace, so an invalid result leaves the old file untouched. `unregister_project` refuses the active project and a pulled one.

**The API key lives in the macOS Keychain,** through `keyring`: service `daedalus`, account `deepseek`, never in a file. `python -m daedalus.secrets set deepseek` stores it (via `getpass`), and `status` says whether one is present without printing it. `DEEPSEEK_API_KEY` overrides the Keychain, for tests and CI. With no key, `daedalus` exits saying how to set one, before any client exists.

**Agent tools.** Exactly `get_scope`, `list_notes`, `read_note`, `search`, `backlinks`, `list_files`, `read_code`, `propose_note`, `find_precedent`, plus `propose_edit` and `propose_lesson` for `/end`, and `ask_user`: twelve. daedalus-mcp keeps the first nine. Coder mode has its own fourteen (below). The policy limits `propose_edit` to `CONTEXT.md`, `inbox/`, and `questions/` (rule 5), and `propose_lesson` to the shared `inbox/`. The schemas refuse extra arguments, and none has `source`: the agent code calls `propose_note` with `source="deepseek"`. Tool results are capped at 60,000 characters, with a truncation note. There are at most 12 tool rounds per user turn. Refusals go back to the model as the tool result, naming the rule. Schema errors that `read_note` reports are also shown in the terminal.

**Pricing** (`terminal/pricing.py`, one table with `checked: 2026-11-25`). USD per 1M tokens, off-peak; peak is double.

| Model | Cache-hit input | Cache-miss input | Output |
|---|---|---|---|
| `deepseek-flash` | $0.003 | $0.15 | $0.60 |
| `deepseek-v4-pro` | $0.022 | $0.66 | $1.98 |

- **Peak** is 01:00–04:00 and 06:00–10:00 UTC, Monday to Friday. Each window is half-open: 04:00:00 is off-peak.
- **All time math is in UTC.** The daily cap resets at 00:00 UTC.
- **Chinese public holidays are off-peak** in DeepSeek's billing, but they can't be known here. A weekday in a peak window is always priced as peak, which overestimates on holidays: the safe direction for a cap.
- **A call is priced at its start time.** Reasoning tokens are part of `completion_tokens` and bill as output.

**Requests.** OpenAI SDK, `base_url="https://api.deepseek.com"`, `stream=True` with `stream_options={"include_usage": True}`. `max_tokens` (default 8,000) and `reasoning_effort` (default `low`) are set on every request, never left to the API defaults (64K and `high`). `thinking` is enabled explicitly, and `tool_choice` is `auto` (`required` is a 400 in thinking mode). 429 and 5xx retry twice with backoff; 400, 401, 402, and 422 surface as clear messages. **`reasoning_content` is passed back:** every assistant message in history keeps it, because DeepSeek returns 400 on a tools request without it. That passed-back reasoning bills as input on every later call, which is why the default effort is `low`.

**Caps** (`~/.config/daedalus/config.toml`, defaults `session_cap_usd = 1.00`, `daily_cap_usd = 3.00`; an unknown key is an error). **Worst case before every call:**
- The worst case assumes every input token is a cache miss and the full `max_tokens` is used, at the current period's rate. Input is estimated at 3.0 characters per token, then calibrated from the last call's billed `prompt_tokens`.
- If `spent_session + worst_case > session_cap` or `spent_today + worst_case > daily_cap`, the call is refused before any network I/O, and the message names the cap.
- The check and a worst-case reservation row are one SQLite transaction in `~/.local/state/daedalus/ledger.sqlite`. The ledger is the single source of truth, so two terminals share the daily cap.
- The row then settles to the billed usage (`estimated = 0`), or is deleted if DeepSeek never answered with a stream (nothing was billed).

**Interrupted streams are recorded as estimated.** On Ctrl-C, a dropped connection, or a stream with no usage chunk, the row settles to an estimate (`estimated = 1`): input all cache-miss, output from the streamed characters. It counts toward the caps: a stopped stream may still have been billed. A process killed mid-call leaves the worst-case reservation in place.

**The meter.** While streaming, the line is an estimate (leading `~`), with reasoning shown separately. On the final chunk it snaps to the billed numbers, for example `↑ 12,480 (9,100 cached)   ↓ 312 (reasoning 180)   $0.0021 this call   $0.21 today   off-peak`.

**The system prompt runs from most stable to least stable**, so DeepSeek's prefix cache hits as much as possible:
1. the role preamble;
2. the shared `AGENTS.md`;
3. the active project's `AGENTS.md`;
4. accepted shared practices (`status: accepted`), by path, at most 12,000 characters, packing whole practices and naming any left out;
5. each pulled project, sorted by name: only the "Decisions index" and "Where things live" sections of its `CONTEXT.md`, at most 6,000 characters each;
6. the active project's `CONTEXT.md`, last, because it changes most: every section whole except Log, which keeps its 8 newest entries (by heading date when every entry is dated, else the last in the file) and a one-line note counting the older ones.

It's byte-identical for identical files: fixed order, sorted paths, no timestamps, no set iteration. A test compares all six blocks across processes with different hash seeds. Anything dated, such as `/end`'s date, goes in the user message, never the system prompt.

**`/end`.**
- If the session ran no tools and made no proposals, it just exits.
- Otherwise it sends one more user turn, carrying today's date and the current `CONTEXT.md`. The agent proposes, through `propose_edit` (so with the current `base_sha256`), the full file plus a dated Log entry: what was asked, learned, proposed or decided, and open threads. Up Next changes only if the session clearly changed it.
- The agent may also propose lessons into the shared `inbox/`. A lesson records what failed, why, and the fix; nothing else qualifies.
- The REPL then walks this session's pending proposals one at a time (diff, then approve, reject with a reason, or skip), and exits.
- Nothing is written until you approve. A `CONTEXT.md` edited meanwhile makes the approval refuse as stale.

## The Obsidian bridge: `/open`, `/here`

`daedalus.terminal.obsidian`. **Obsidian is asked where, never what.** It reports which vault folder and which file are focused; every byte of content is read through `daedalus.core` (`tools.read_note`, so the path policy and schema reporting apply). The CLI's own `read`, or any CLI command that changes a vault, is never used.

**Trap 1: every project vault is named `vault`.** Obsidian names a vault after its folder, so scratch and scratch2 (and every future project) are all "vault" to Obsidian. Never target a vault by name (`vault=` in a CLI call or a URI):
- `/open` uses `open "obsidian://open?path=<url-encoded absolute path>"`, and Obsidian picks the vault that contains the path.
- `/here` maps the reported `base` to a registered vault by real path (`realpath`, then `samefile`), never by folder name.

**Trap 2: the CLI launches Obsidian if it isn't running.** `/here` checks `pgrep -x Obsidian` first and answers "Obsidian isn't open" without touching the CLI. `/open` may launch Obsidian, because you asked to see the note.

**`/here`, in order:**
1. If Obsidian isn't running, return `NotOpen`.
2. If there's no `obsidian` on PATH, return `CliMissing`, with the setup steps: Settings → General → Advanced → Command line interface, register, then check `obsidian version` in a new terminal.
3. Run the eval below from `/`, a working directory inside no vault, so the CLI answers for the vault you focused last, with a 5 s timeout:

   ```
   obsidian eval code="JSON.stringify({base: app.vault.adapter.getBasePath(), file: app.workspace.getActiveFile()?.path ?? null})"
   ```

4. Treat the output as untrusted, and give each failure its own error, never a guess:
   - The CLI prints `=> <value>` and **exits 0 even on errors** (`Error: …`, `Vault not found.`). So anything but exactly one `=> {base, file}` line is `NotJson`, and the error quotes what the CLI printed.
   - `base` must be an existing directory (`BadBase`).
   - `file` mustn't be null (`NoActiveFile`).
   - `base + file` must exist (`FileMissing`).
5. A `base` that isn't registered is `Unregistered`. A registered vault that isn't in the session is `NotInScope`, and the message says `/pull <name>` would bring it in.
6. Authorize READ on the file, so `.obsidian/` is refused with rule 4, and a path escaping the vault with rule 1. Only `.md` notes are attached.

**The attachment rule.** `/here`'s note is prefixed to your **next user message** as a fenced block headed by its `vault:path`, and never put in the system prompt: that would break the stable prefix and cost cache hits. The fence is longer than any backtick run in the note, and the note is capped at 60,000 characters with a truncation note.
- It goes with exactly one message.
- A second `/here` replaces it; it doesn't stack.
- A failed `/here` attaches nothing and drops any earlier attachment.
- A failed turn keeps it for the retry.
- `/end` never carries it.
- `/vault` clears it, and so does a `/drop` of its vault.

Schema errors are shown as for `read_note`. A test checks the next request's system message is byte-identical to the one before `/here`.

**Tests** fake the CLI with an executable `obsidian` first on PATH, which logs its cwd and argv, and inject the running check. The terminal conftest makes the real app "not running" and refuses `open` for every test.

## The design-decision gate

**No design choice goes ahead without a precedent or the human's answer.** `daedalus.core.gate`, `daedalus.core.accept`.

**The four categories** are `DESIGN_CATEGORIES` in `daedalus.core.schema`: `schema`, `dependency`, `interface`, `data_source`. Every `AGENTS.md` (both templates and every live vault) names exactly these four in its "Design choices" section, as `` - `name`: ... `` bullets. `tests/test_templates.py::test_agents_md_names_exactly_the_design_categories` checks the templates and every vault in the real registry.

**What counts as precedent, and nothing else:**
- an accepted note (`status: accepted`, passing the schema) in the **active** project's `decisions/`;
- an accepted note in the **shared** vault's `practices/`.

**`check_basis(session, category, choice, basis) -> Verdict`.** `basis` is a wikilink or the literal `new`. The verdict is one of three:
- `OK(note, ref)`: the basis is precedent.
- `NEEDS_USER`: the basis was `new`; ask with `ask_user` and stop.
- `REFUSED(reason)`: anything else. The reason says what would make it valid. The refusal kinds:
  - a `proposed`, `draft`, or `superseded` note;
  - any other folder, including shared `lessons/` and `inbox/`;
  - a **pulled** project's decision: it doesn't bind this project, so promote it into a shared practice;
  - a missing, ambiguous, or out-of-scope link (an ambiguous link names its matches and the `[[decisions/x]]` path form);
  - an unknown category;
  - a basis that isn't a wikilink;
  - a note that breaks the schema.

The gate only reads, through `tools.read_note`. `gate.propose_with_basis` is `check_basis` followed by `propose_note`, raising `GateRefused` (a `ProposalError`) unless the verdict is `OK`; the coding loop and plan approval (commits 10–12) call it.

**`find_precedent(topic, category=None)`**, in both daedalus-mcp and the agent:
- It runs an any-word FTS search over the active `decisions/` and the shared `practices/`.
- Only accepted, schema-valid notes are kept, so every result passes `check_basis`.
- It returns at most 10 entries of `(ref, title, status, excerpt)`, where the excerpt is the body's first 300 characters, best match first.
- With `category`, notes that mention it come first.

**`ask_user(question, category, options, context_refs)`** is for the agent only; Claude asks in its own chat.
- It proposes a typed `question` note, `questions/q-<slug>.md`, through the queue, as `source: deepseek`. The `q-` prefix means a question never shares a name with its decision. The note holds:
  - `options`, with at least two distinct;
  - `answered: false`;
  - `related` set to `context_refs`, at least one, under the link rule;
  - a body with the question, `` Category: `x` ``, the numbered options, the context links, and why no precedent applied, from its own `find_precedent` run.
- ★ **A successful `ask_user` ends the agent's turn.** No further model call is made that turn. Later tool calls in the same reply get a "Not run: ask_user ended this turn" result, so the history stays valid. A refused `ask_user` doesn't end the turn: the model can fix it.
- The REPL shows the question and its numbered options at once.

**`/answer <question-id> <option-number | "free text">`:**
1. The answer is validated against the question first.
2. If the question proposal is still pending, it's approved (its own commit).
3. Then **one** commit:
   - it creates `decisions/<slug>.md`, the question's stem without `q-` (`-2`, `-3` … if taken), with `type: decision`, `status: accepted`, and `source: human`;
   - its `related` is `["[[questions/q-<slug>]]"]`;
   - its body holds the question, the category, the decision, and the rejected options;
   - it edits the question to `answered: true`, adds `[[decisions/<slug>]]` to its `related`, and fills `## Answer`.
   - A bare `[[<slug>]]` therefore always means the decision.

Free text rejects every option. An already-answered, rejected, or non-question proposal is refused, as is an unknown id.

**`/promote <inbox-ref> [folder]`** moves an inbox draft into the folder its `type` maps to, in **one** commit that holds the inbox file's deletion and the target's creation:
- `status` becomes `accepted`;
- every other byte stays as it was, `source` included, because acceptance is the status;
- `folder`, if given, must agree with the mapping.

It's refused in these cases:
- the draft has no `type`;
- the target already exists;
- the draft doesn't validate, under the schema and the link rule, against the target type;
- a shared `inbox/` draft isn't a `practice` or `lesson`;
- a project `inbox/` draft is a `practice` or `lesson` (those belong in the shared inbox);
- the note isn't directly in `inbox/`;
- the draft is in a pulled vault.

The inbox deletion is authorized as `PROPOSE` (inbox is where drafts live), and the target as `USER_ACTION`, so the shared `practices/` and `lessons/` are allowed by rule 2.

**Multi-path apply.** `apply.apply_changes(repo, [Change(path, content | None, base_sha256 | None)], summary)` writes N paths and commits them as one, under the OS write lock throughout:
1. **Stale check.** Every path is checked against its `base_sha256`; `None` means the path must not exist yet.
2. **Record.** One `applying` change set goes into `queue.sqlite` (tables `changesets` and `changeset_paths`, queue schema v3), holding every path's prior content, mode, and new content.
3. **Write.** Each path is replaced atomically, or unlinked for a delete, and checked to still resolve to its authorized path.
4. **Commit.** One `git commit --only -- <paths>`: new or untracked paths are `git add`ed first, and deleted tracked paths need no staging. The commit carries `Daedalus-Change: <changeset id>`.
5. **On failure.** A failing commit (a hook included) or any exception restores **every** path, unstages what was staged, and marks the change set `failed`.
6. **Crash recovery.** Every queue open reconciles `applying` change sets like proposals:
   - if a commit with the trailer landed, the set is marked applied;
   - otherwise every path still holding our content is restored, and a path changed since is left alone.

Plan approval in commit 12 will reuse this to write several decision notes at once.

## Uploads: `/add`

`/add <file>` (`daedalus.core.ingest.ingest`) converts a local document to markdown, saves it as `sources/<slug>.md` in the active vault, and commits it as `add: <original_name>`. It calls no model; the agent reads the note with `read_note` like any other. The REPL prints `→ added <vault>:sources/<slug>.md (N chars, from <name>)`, then the data rule: "Agents can read this, and anything they read is sent to DeepSeek."

**Allowed extensions:** `.pdf`, `.docx`, `.pptx`, `.xlsx` are converted, and `.md` and `.txt` are copied as-is after a UTF-8 check. Anything else is refused before conversion.

**Local-only conversion.**
- **The package:** `markitdown[pdf,docx,pptx,xlsx]==0.1.9`, pinned, and never `[all]`: the other extras (Document Intelligence, audio and YouTube transcription, …) reach web services.
- **The converter:** one `MarkItDown(enable_plugins=False)`, created on first use. It's called only as `convert_local(path)`, reading `result.markdown`. It never gets a URL, an `llm_client`, or a `docintel_endpoint`.
- **Path checks first.** MarkItDown does I/O with this process's privileges, and its README warns against passing untrusted input to it directly, so the path is checked before the converter ever sees it:
  - it resolves with `realpath`;
  - it's a regular file, not a directory, device, or FIFO;
  - it's outside every registered vault, any `.git`, and `~/.local/state/daedalus`;
  - it's at most 25 MB;
  - a `scheme://` URL is refused.
- **Hashed twice.** The file is hashed before and after conversion, and refused if it changed in between.
- **Proven network-free.** `test_conversion_never_touches_the_network` blocks `socket.connect`, `connect_ex`, `create_connection`, and `getaddrinfo`, builds the real converter from scratch, and converts every allowed format. Attempts are recorded as well as refused, so a converter that swallows the error still fails the test.
- **PDF glyph artifacts.** pdfminer writes `(cid:NNN)` for a glyph it can't map to a character, such as a phone or envelope icon. Those markers, and the spaces after them, are stripped from PDF output only; a `.md` or `.txt` file that mentions one keeps it.

**The checks, in order:**
1. **Duplicates.** The original's SHA-256 is checked against the `original_sha256` of every note in the active `sources/`. A match is refused and named, so renaming a file doesn't get it in twice.
2. **No text layer.** A result with fewer than 50 non-whitespace characters is refused: "no text layer — likely a scanned document; OCR isn't enabled". A scanned PDF converts to empty text with no error, and an empty note would look like success.
3. **Size.** Markdown over 2 MB is refused, never truncated.

**`SOURCE_KEYS`** (`daedalus.core.schema`) are exactly the frontmatter `/add` writes, in this order:
- `title`: the first markdown heading, else the filename stem;
- `original_name`
- `original_sha256`
- `original_bytes`
- `converter`: `markitdown <version>`, or `copy` for `.md` and `.txt`;
- `added`: today's local date, quoted;
- `source: human`

`sources/` stays exempt from the typed schema and the link rule.

**The original never enters git.** Only the converted markdown is committed; the hash identifies the original without storing it, so PDFs and decks don't bloat every clone.

**Names.** The stem is the slugged title, with `-2`, `-3` … until it matches no note stem in any registered vault, so a bare link to it is never ambiguous.

**The write** is one path through `apply.apply_changes`, as `USER_ACTION` into `sources/`, so it carries apply's guarantees: the lock, the stale check, the atomic write, the path-limited commit, rollback, and crash reconciliation. It uses `apply_changes` rather than a queue proposal because a proposal's content is capped at 256 KB, and `/add` is a direct human action.

## The sandbox: running commands

Once a program runs, the path policy no longer applies: it can do anything your user account can. So **every command an agent triggers runs through `daedalus.sandbox.runner.run`**, under macOS's built-in Seatbelt (`/usr/bin/sandbox-exec`), the mechanism Anthropic's sandbox-runtime uses for Claude Code on macOS. Nothing is installed and nothing runs in the background.

★ **Fail closed.** At first use per process, the runner runs `sandbox-exec -p "(version 1)(allow default)" /usr/bin/true`. If that fails, or `sandbox-exec` is missing, this run and every later one refuse with "sandbox unavailable — Daedalus won't run commands unsandboxed". A profile that doesn't load (exit 65 or 71 with a `sandbox-exec:` message, which is how a missing `-D` parameter shows up too) is the same refusal, never a result. There is no unsandboxed fallback.

**`run(session, argv, worktree) -> RunResult(exit_code, stdout_tail, stderr_tail, duration_s, timed_out)`**, in an order where every refusal comes before any spawn it can stop:
1. `daedalus.toml` from your checkout of the project repo, never the worktree, so code an agent edits can't widen what it may run.
2. Rule 7's shape and name checks.
3. The worktree must be the session's worktree or the active repo, and not the home folder or above it.
4. The venv for the current `uv.lock` (below).
5. Rule 7's pin.
6. The preflight.
7. A fresh run folder (`mkdtemp`, then `realpath`) holding the profile and `tmp/`.
8. `/usr/bin/sandbox-exec -f <profile> -D KEY=<realpath> ... -- <venv>/bin/<name> <args>`, in exec form (no shell), with `cwd=<worktree>`, in its own session.
9. The last 200 KB of stdout and stderr are kept, and the run folder is deleted, even if the run made parts of it read-only.

**The profile** (`sandbox/profile.py`) starts from `(version 1) (allow default)` and denies the four things that matter. In SBPL the later rule wins, so each deny comes before its exceptions.
- ★ **Why allow-default, not `(deny default)`:** allow-everything-then-deny is far easier to get right (a deny-default profile has to list every mach service, sysctl, and file Python touches, and breaks quietly when macOS changes), and it's still strong here, because the denies cover the four things that matter: network, writes, reads in the home folder, and the Keychain.
- **Network:** `(deny network*)`, Unix sockets included. DNS also goes over mach, so `com.apple.dnssd.service` and `com.apple.mDNSResponder` are denied as well. `allow_localhost = true` re-allows only `localhost:*` bind, inbound, and outbound (by address: name lookup stays off, so use `127.0.0.1`).
- **Writes:** `(deny file-write*)`, then only the run's `tmp/` (also `HOME` and `TMPDIR`), `/dev/null`, `/dev/zero`, `/dev/tty*`, and `/dev/fd`. ★ **The worktree is read-only to the run,** so tests can't modify the code they test; code changes go only through 10b's policy-checked `edit_code`. The venv is read-only too, so a run can't plant code for the next one.
- **Reads:** `(deny file-read*)` under the real home folder, then re-allowed for the worktree, the venv, uv's Python folder (`uv python dir`, realpath'd), the venv's base Python if it lives under home, and the run's `tmp/`. That keeps `~/.ssh`, `~/.aws`, `~/.config`, `~/.local/state/daedalus`, every vault, other repos, `~/Library/Keychains`, and the Claude desktop config unreadable. The worktree's `vault/` and `.git` are denied again (rule 4). System paths outside home stay readable; Python needs them.
- ★ **Metadata only where pytest needs it.** pytest walks up from its rootdir looking for its config, and stats every entry it collects. Seatbelt answers a denied `stat` with `EPERM`, and Python's `Path.is_file()` raises on `EPERM` rather than returning False, so plain `deny file-read*` breaks pytest in any worktree under home or with a `vault/` or `.git`. So `file-read-metadata` is re-allowed, by `literal` path only, on each ancestor folder of the worktree up to and including home, and on pytest's config filenames in those folders (`pytest.ini`, `.pytest.ini`, `pyproject.toml`, `tox.ini`, `setup.cfg`, `conftest.py`), each its own `-D` parameter. Inside the worktree, `vault/` and `.git` keep metadata (pytest fails without it). Everything else under home can't even be `stat`ed, and metadata never gives contents, a listing, or xattrs; the live tests check all of it. A worktree outside home needs no such rule.
- **The Keychain:** `com.apple.SecurityServer` and `com.apple.securityd` are denied over mach, since securityd isn't reached through files. Each layer (the mach denial, the home read denial) blocks the Keychain on its own, even when the run puts `HOME` back to the real home folder; with both removed, the scratch2 venv's interpreter, the one granted "Always Allow", reads the key silently. That's why both stay.

★ **The realpath rule.** Every path goes in as a `-D` parameter, resolved with `realpath` first, and the profile text names only `(param "KEY")`, never a path. On macOS `/tmp` and `/var` are symlinks into `/private`, and Seatbelt matches real paths: a profile allowing `/tmp/run123` allows nothing.

★ **A clean environment.** The child's whole environment is built from scratch, never inherited: `PATH=<venv>/bin:/usr/bin:/bin`, `HOME=<run tmp>`, `TMPDIR=<run tmp>`, `LANG=en_US.UTF-8`, `PYTHONDONTWRITEBYTECODE=1`, `PYTEST_ADDOPTS=-p no:cacheprovider`. Nothing else, so `DEEPSEEK_API_KEY` and `SSH_AUTH_SOCK` never reach it.

**Limits and the timeout** (`sandbox/exec.py`). The child gets `RLIMIT_CPU = timeout_s`, at most 1,024 open files, a 512 MB file-size cap, and no core files. On timeout the whole process group gets SIGTERM, 2 s to exit, then SIGKILL. Whatever the outcome, the group is SIGKILLed once the leader is done, so no background child outlives its run, and the runner confirms the group is gone. ★ **There's no memory cap:** macOS doesn't reliably enforce memory rlimits, so none is claimed. The timeout and the CPU limit are the real protection. A process that calls `setsid()` leaves the group and escapes the kill, but keeps its CPU limit and its sandbox: no network, no writes outside its (deleted) run folder.

**The venv** (`sandbox/venv.py`). ★ It lives outside the worktree, at `~/.cache/daedalus/venvs/<project>-<sha12 of uv.lock>/`, built from your checkout with `UV_PROJECT_ENVIRONMENT=<that path> uv sync --frozen --no-install-project`. `--no-install-project` keeps the project itself out: an editable install would point at your checkout, and tests must import the worktree's code. A venv counts only once `.daedalus-ready` exists, so a sync that died halfway is removed and redone; an flock stops two terminals building the same one. Old venvs aren't pruned yet.
- ★ **Dependency install is unsandboxed, and trusted only because the lockfile is approved and frozen.** `uv sync` needs the network, so it runs outside the sandbox, and only when the lock's hash is new. The lockfile is its only input. A dependency change is a design decision in the `dependency` category, so the gate puts it in front of you before it can ever reach `uv.lock`.

**`daedalus.toml`**, committed at the project repo root:

```toml
[sandbox]
allow = ["pytest", "ruff"]   # programs agents may run: bare names, looked up only in the venv's bin/
timeout_s = 300              # 1 to 3600; also the CPU-seconds limit
allow_localhost = false      # true only if tests need to bind local sockets
```

No file means the project can't run commands, and the refusal shows how to add one. Unknown tables or keys are errors, and so are an `allow` entry with a `/`, a non-integer timeout, and a non-boolean `allow_localhost`.

**`python -m daedalus.sandbox run --project <name> [--worktree <path>] -- <program> [args]`** runs one command for a registered project without touching `state.json`. The worktree defaults to your checkout, which the run can read but never write. It prints the output tails and `— sandboxed: exit N in S s`, and exits with the command's code (124 on timeout, 2 if refused).

## Worktrees and coder mode

★ **Agents write code only in worktrees, and only you merge.** Your checkout is never written by an agent: rule 6 confines `WRITE_CODE` to the session's worktree, and merging is a REPL command, never a tool.

**Where.** ★ Each worktree lives at `~/.cache/daedalus/worktrees/<project>/<slug>-<6 hex>/`, not beside the repo, so `~/code` stays clean, on branch `agent/<slug>`. Slugs follow the registry's project-name rules. `daedalus.core.worktree` records what it creates in `~/.local/state/daedalus/worktrees.json`. `Session.worktree` is set only while coder mode is on; `state.json` never holds it, so daedalus-mcp's sessions never have one.

**The lifecycle** (`daedalus.core.worktree`; create, merge, and drop hold the vault write lock, since apply commits into the same repo):
- `create(session, slug)`: `git worktree add -b agent/<slug> <path> HEAD` from your checkout. Refused if the slug, branch, or path is taken. If your checkout has uncommitted changes, it warns that the worktree starts from HEAD and won't see them.
- `list_worktrees(registry, project)`: slug, branch, path, commits ahead of your HEAD, and uncommitted changes, merged with `git worktree list --porcelain`. A worktree git knows but daedalus didn't create is reported as unrecorded; a recorded one that's gone, as missing.
- `diff(registry, project, slug)`: `git diff HEAD...agent/<slug>`, the branch against its merge base.
- `commit(session, message)`: `git add -A` in the worktree, then commit with the trailer `Daedalus-Worktree: <slug>`. ★ `git add -A` is safe here, unlike in your checkout: only the edit tools write the worktree (sandboxed runs can't), and its `vault/` and `.git` are unwritable. It still checks after staging that no staged path is under `vault/`, and if one is, unstages everything and aborts.
- `merge(registry, project, slug)`, **your action only**: refused if your checkout has uncommitted changes (untracked files included), if the branch has nothing to merge, or if it touches `vault/`. Then `git merge --no-ff agent/<slug>` (subject `merge: agent/<slug>`, same trailer), so the merge shows in history. On a conflict it runs `git merge --abort`, confirms HEAD and `git status` are as before, and names the conflicting files.
- `drop(registry, project, slug, force=False)`: `git worktree remove`, then deletes the branch. ★ **Drop protection:** refused if `agent/<slug>` has commits not merged into HEAD, or the worktree has uncommitted changes, unless `force`. Unmerged agent work is never thrown away by accident.

**Code tools** (`daedalus.core.tools`, for the agent in coder mode):
- **Reads follow the worktree.** With a worktree active, `read_code` and `list_files` read it, so the agent sees its own edits; without one, your checkout, as before.
- ★ **`edit_code(rel_path, old, new)`** is the default editing tool: an exact replacement, far cheaper in output tokens than rewriting a file. **`old` must occur exactly once**; zero or several matches are refused with the count, and the file is left alone.
- **`write_code(rel_path, content)`** creates a file (making missing folders) or replaces one whole, at most 64 KB before and after. **`delete_code(rel_path)`** deletes one.
- Each authorizes `Op.WRITE_CODE` (rules 1, 4, 6; an absolute path is left to the policy, so one into your checkout is rule 6 by name), writes atomically through an `O_NOFOLLOW` directory fd, and re-checks the realpath afterwards, restoring the file if it moved, as apply does. Binary (a NUL byte) and non-UTF-8 content is refused, and an edit keeps the file's mode. Every edit lands in the conversation's in-memory `EditLog`, for 10c's summaries.
- **`run(argv)`** is 10a's sandboxed runner in the session's worktree. It returns the exit code, duration, timed-out flag, and the last 20,000 characters of stdout and 8,000 of stderr. `repl.log` gets metadata only: the program, the argument count, exit, duration, and output sizes; never the arguments' text or the output.

**The coder profile.** Exactly `get_scope`, `list_notes`, `read_note`, `search`, `backlinks`, `list_files`, `read_code`, `propose_note`, `edit_code`, `write_code`, `delete_code`, `run`, `find_precedent`, `ask_user`: fourteen, with its own preamble (block 1 of the system prompt) and its own wording for `read_code` and `list_files`. The chat profile's twelve and daedalus-mcp's nine are unchanged; each set has an exact-set test.

**Coder mode in the REPL.**
- `/code <slug>` sets `Session.worktree`, switches to the coder profile, starts a fresh conversation, and shows `[coding <project> · agent/<slug>] › ` as the prompt. Each `run` prints its exit code, its time, and the last 12 lines of its output.
- `/code off` offers to commit the worktree's uncommitted changes. The default message is written by a separate, tool-less one-shot call (metered and capped like any other, outside the conversation, so it can't act); Enter accepts it. Declining leaves the changes in the worktree. Then it's back to the chat profile, with a fresh conversation.
- `/end` in coder mode does the same, then the usual `CONTEXT.md` proposal, written by a chat-profile agent (coder mode has no `propose_edit`) from a summary of the coding session: the branch, the edits, the runs, and the commit.
- `/vault` in coder mode leaves coder mode first. `/pull` and `/drop` keep it. `/wt drop` of the worktree you're coding in is refused.

## The coding loop: `/goal`

`/goal <note>` (`daedalus.terminal.loop`, with `daedalus.core.goals`) runs coder mode by itself on a goal note, in a fresh worktree `agent/<goal-slug>`, until the goal's check passes **honestly**, or a cap or a check-in stops it. Merging stays your command.

**Goal notes** (`goals/<slug>.md`, type `goal`): `done_when` (a command, e.g. `"pytest -q"`), `max_iterations`, `budget_usd`, `checkin_every`, and `protected`, a list of repo-relative paths the run may not change (it may be empty; an entry ending in `/` covers a folder).
- ★ **Only you launch goals.** `/goal` refuses a note unless it has `source: human`, or `status: accepted` (how an agent's draft gets there, by `/promote`). Agents can't start runs from goals they wrote.
- `done_when` is split with `shlex` and authorized as the sandbox will run it (rule 7: allowed, and installed in the venv) **before** any worktree exists. A goal whose check can't run is refused, and nothing is left behind. A pytest check that prints no summary line at the baseline is refused too, and its worktree removed.

**Start.** The worktree, then ★ **a baseline**: `done_when`, sandboxed, before any edit. For pytest, the collected, skipped, and xfailed counts from its summary line (collected is passed + failed + errors + skipped + xfailed + xpassed, so deselected tests don't count, and a module that fails to import counts as one); otherwise just the exit code.

**Each iteration:**
1. ★ **Fresh context.** A new conversation, on the coder profile, whose one user message holds the goal note, every earlier iteration's log in order, the worktree's `git diff --stat` against its base, the end of the last check's output, and any redirect or answered question. No tool history or `reasoning_content` carries over between iterations, which bounds context and cost however long the goal runs; within an iteration, `reasoning_content` is passed back as usual. At most 12 tool rounds. The agent ends with `PLAN:`, `DONE:`, and `NEXT:` lines.
2. **Plan and edit.** A design choice cites precedent (`find_precedent`) or goes to `ask_user`, which pauses the run.
3. **Check.** The loop runs `done_when` itself, sandboxed, whatever the agent ran.
4. ★ **The honest-done rule.** Done only if all four hold: the check exits 0; for pytest, collected is at least the baseline; skipped and xfailed are each no more than the baseline; and no protected path differs from the base (committed, uncommitted, or new). A check that passes with a failed guard is **not done**, and the log and report say which guard, in words: "the check passed, but 1 fewer test was collected than at the start (2 → 1)."
5. **Log.** `logs/<goal-slug>-<NN>.md` through `write_log`: create-only, committed in your checkout's vault. It holds the plan, the files edited (from the edit log), the check, its exit code and counts, the last 40 lines of output, each guard, the verdict, and what's next. Numbers continue across runs of the same goal.
6. **Commit** the worktree's changes as `iter <NN>: <summary>` (nothing to commit, no commit).
7. **Stop** at the first of: done; `max_iterations`; the goal's budget; a stop at a check-in. ★ Every ledger row in a run carries its `run_id` (ledger schema v2), and the goal budget, the session cap, and the daily cap are all checked in one transaction **before** each call, against its worst case. A call refused at the start of an iteration leaves no log.

**Protected paths.** For the length of a run, the session carries the goal's `protected` (`Session.protected`), and `edit_code`, `write_code`, and `delete_code` refuse them: `PolicyError(rule=6)`, "protected by the goal", compared case-folded after the path is resolved.

★ **Config changes check in at once.** A write to `pyproject.toml`, `setup.cfg`, `tox.ini`, `pytest.ini`, `.pytest.ini`, any `conftest.py`, `daedalus.toml`, or `uv.lock` is allowed, but the loop checks in right after that iteration, whatever `checkin_every` says, showing the diff. You see every change to how tests are found or run while it happens, not at merge. For `daedalus.toml` and `uv.lock` the check-in says the edit changes nothing until you merge: 10a reads both from your checkout.

**Check-ins**, every `checkin_every` iterations and after a config change, show the iteration count, the spend against the goal budget, the last summary, and `git diff --stat`, then ask: **continue**, **redirect** (your guidance goes into the next iteration's message), or **stop**.

**Pause and resume.** `ask_user` pauses the run (its iteration is logged and committed, with no check); answer with `/answer`, then `/goal resume` continues at the next iteration, with the answered question in its message. Ctrl-C pauses too: the iteration's edits stay uncommitted, and `/goal resume` commits them as `iter <NN> (interrupted)`, logs it, and goes on. `/goal resume <guidance>` on a run that stopped without being done grants another `max_iterations` iterations and another `budget_usd`.

**Run state** lives in `~/.local/state/daedalus/runs/<run_id>.json` (`<goal-slug>-<UTC timestamp>`), written atomically after every step, so a resume works after quitting the REPL. `/goal list` shows the runs.

**The report**, when a run ends: the stop reason, iterations and spend, each guard of the last check, any config files changed, and the branch's full `git diff --stat`. It ends with `/wt diff <slug>`, then `/wt merge <slug>`, or `/goal resume <guidance>`.

**Lessons.** If something failed in the run and a later check fixed it, one more DeepSeek call, with no tools and no history, reads the run's logs and returns at most 3 lessons, which the loop proposes into the shared `inbox/` with `propose_lesson`: what broke, why, and the fix, with `applies_to` tags and a link to the run's last log. `/review lessons` walks pending lessons: approve (it lands in `shared:inbox/` as a draft, and the REPL prints the `/promote` that would accept it; approval never promotes), reject with a reason, or skip.
