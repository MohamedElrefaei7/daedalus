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
├── pyproject.toml            # hatchling build, src/ layout; deps pyyaml, fastmcp (pinned), openai, keyring, prompt_toolkit, rich
├── uv.lock
├── config/
│   └── vaults.example.toml   # shape of ~/.config/daedalus/vaults.toml
├── scripts/
│   └── smoke_read.py         # live read-layer smoke test against the real registry
├── src/daedalus/
│   ├── mcp_server.py         # daedalus-mcp: the stdio MCP server for the Claude desktop app
│   ├── toolset.py            # the eight agent tools, shared by daedalus-mcp and the terminal agent
│   ├── secrets.py            # API keys in the macOS Keychain (`python -m daedalus.secrets set|status`)
│   ├── queue_cli.py          # debug CLI for the queue (list/diff/apply/reject); the REPL replaces it
│   ├── state.py              # `python -m daedalus.state` entry point (logic in core/state.py)
│   ├── terminal/             # `daedalus`: the REPL (commit 5; Textual UI in commit 14)
│   │   ├── repl.py           # prompt_toolkit input, rich Live output, /commands; main() is the script
│   │   ├── agent.py          # tool specs, the 12-round tool loop, reasoning_content kept in history
│   │   ├── deepseek.py       # OpenAI SDK client for DeepSeek: streaming, accumulation, errors, retries
│   │   ├── meter.py          # token estimates, worst-case cap check, the meter line, interrupted calls
│   │   ├── ledger.py         # spend ledger (SQLite, WAL): reservations, spent(), caps
│   │   ├── pricing.py        # the price table, peak windows (UTC), cost(), worst_case()
│   │   ├── prompts.py        # the byte-stable system prompt
│   │   └── config.py         # ~/.config/daedalus/config.toml: model, effort, max_tokens, caps
│   └── core/                 # the safety core
│       ├── schema.py         # SCHEMA: single source of truth for frontmatter
│       ├── registry.py       # vaults.toml loading + validation (RegistryError)
│       ├── session.py        # Session dataclass, Area enum
│       ├── state.py          # state.json -> Session (load_session, save_state, StateError) + stopgap CLI
│       ├── policy.py         # Op enum, authorize(), authorize_command(): the seven rules
│       ├── notes.py          # frontmatter parsing + schema validation (NoteSchemaError)
│       ├── links.py          # wikilink extraction + Obsidian-style resolution
│       ├── index.py          # SQLite FTS5 search index
│       ├── errors.py         # ProposalError and its subclasses
│       ├── vaultio.py        # write lock, fd-relative file ops, path-limited git commits
│       ├── queue.py          # proposal queue (SQLite, WAL) + crash reconciliation
│       ├── apply.py          # apply, reject, pending, recent_rejections, diff
│       └── tools.py          # note read tools; list_files/read_code; propose_note/edit/lesson; write_log
├── templates/
│   ├── project-vault/        # copied wholesale to create a project vault
│   └── shared-vault/         # copied to create ~/daedalus/shared/
└── tests/
    ├── test_templates.py     # template contract test
    ├── core/                 # one file per core part, test_bench.py, test_write_integrity.py
    ├── mcp/                  # daedalus-mcp via FastMCP's in-memory client, plus a stdio subprocess test
    └── terminal/             # recorded SSE streams through httpx2.MockTransport; no live calls
```

Run the tests with `uv run pytest`.

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
5. **Active vault writes.** `PROPOSE`: `inbox/`, `questions/`, `CONTEXT.md`. `CREATE_LOG`: new files in `logs/`. `WRITE_PLAN`: `plans/`. `USER_ACTION`: `goals/`, `sources/`, `decisions/`, `components/`, `experiments/`, `concepts/`, `CONTEXT.md`. Never `AGENTS.md` or `templates/`.
6. **Code.** `WRITE_CODE` only in the worktree, never in your own checkout.
7. **Commands.** `authorize_command(argv, allowlist)`: `argv[0]`'s basename must be allowlisted.

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
goal:       done_when, max_iterations, budget_usd, checkin_every
question:   options, answered # answered: false until resolved
plan:       spec, decisions, tasks
practice:   applies_to
lesson:     applies_to
```

## Rule

Every invariant gets a test that fails when the invariant is broken.

## Writes: proposals, the queue, apply

Agents never write a vault directly. `propose_note`, `propose_edit`, and `propose_lesson` validate the draft against `SCHEMA` and write only to the queue. **The link rule.** A typed draft must link at least one other note. A typed draft is one for a typed folder, or one that declares a type elsewhere, such as an `inbox/` draft. Any of `related`, `shared_refs`, `project_refs`, or a body link counts, dangling links included. A link to `AGENTS.md` or `CONTEXT.md` is refused, whether it resolves to one or just names one. **Source is provenance.** A create's frontmatter `source` must equal the proposal's source. An edit may change it only when the proposal's source is `human`, so an agent may edit a human's draft but can't re-attribute it. The queue is SQLite at `~/.local/state/daedalus/queue.sqlite`, outside every vault, in WAL mode with short transactions. Content is capped at 256 KB.

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

**The exact tool set.** `get_scope`, `list_notes`, `read_note`, `search`, `backlinks`, `list_files`, `read_code`, `propose_note`. Nothing else: never `apply`, `reject`, `propose_edit`, `write_log`, or anything touching queue state. `tests/mcp/test_server.py::test_exact_tool_set` fails if a tool is added or removed.

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

`daedalus [project]` sets the active project (like `python -m daedalus.state set-active`), then starts a plain REPL: `prompt_toolkit` input with history, `rich` `Live` output. A DeepSeek agent answers with the daedalus tools. Commands: `/cost`, `/pending`, `/diff <id>`, `/approve <id>`, `/reject <id> <reason>`, `/help`, `/quit`. `queue_cli` stays as a debug tool. The REPL's session is fixed at start; commit 6 adds `/vault` and `/pull`.

**The API key lives in the macOS Keychain,** through `keyring`: service `daedalus`, account `deepseek`, never in a file. `python -m daedalus.secrets set deepseek` stores it (via `getpass`), and `status` says whether one is present without printing it. `DEEPSEEK_API_KEY` overrides the Keychain, for tests and CI. With no key, `daedalus` exits saying how to set one, before any client exists.

**Agent tools.** Exactly `get_scope`, `list_notes`, `read_note`, `search`, `backlinks`, `list_files`, `read_code`, `propose_note`. The schemas refuse extra arguments, and none has `source`: the agent code calls `propose_note` with `source="deepseek"`. Tool results are capped at 60,000 characters, with a truncation note. There are at most 12 tool rounds per user turn. Refusals go back to the model as the tool result, naming the rule. Schema errors that `read_note` reports are also shown in the terminal.

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

**The system prompt is byte-identical across calls:** a fixed preamble, then the shared vault's `AGENTS.md`, then the active project's. There are no timestamps or set iteration in it, because identical prefixes are what DeepSeek's cache hits on. A test compares it across processes with different hash seeds.
