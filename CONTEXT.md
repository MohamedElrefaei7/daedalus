# CONTEXT

## Log

### 2026-11-25 — Commit 1: vault templates, shared vault template, registry example, template contract test

- Scaffolded the repo: `pyproject.toml` (uv, Python ≥3.11, runtime dep `pyyaml`, dev extra `pytest`), `.gitignore`, `CLAUDE.md`, this file. No package code yet.
- `templates/project-vault/`: 11 folders with `.gitkeep`, `AGENTS.md` (purpose, who-writes table, schema, design choices, linking), empty `CONTEXT.md`, `.gitignore`, and seven note templates (decision, component, experiment, concept, goal, question, plan).
- `templates/shared-vault/`: 4 folders with `.gitkeep`, `AGENTS.md`, `.gitignore`, and practice and lesson templates.
- Live shared vault at `~/daedalus/shared/` (its own git repo) with two accepted practices: `red-test-first`, `one-commit-one-change`.
- Registry: `config/vaults.example.toml` here; the real one is at `~/.config/daedalus/vaults.toml` and isn't committed.
- Scratch project `~/code/daedalus-scratch/` with `vault/` copied by hand from the template. Commit 6's `/new` replaces the manual copy.
- `tests/test_templates.py`: 118 tests over the real template files. Proved red by deleting `code:` from `component.md`, by unquoting `{{date}}` in `decision.md`, and by putting a YAML comment back in `plan.md`.
- Obsidian live check passed: Templates plugin inserts both templates, `created` fills with a real date and is typed as Date, `related` produces a graph edge, Properties shows every key without errors.

### 2026-11-25 — Guard: the repo root is not a vault

- During the live check, Obsidian opened `~/code/daedalus` as a vault by mistake and wrote a `.obsidian/` into the repo root. `test_repo_root_is_not_a_vault` now fails while that folder exists. Proved red with an empty `.obsidian/`. Suite: 119 tests.

### 2026-11-25 — Commit 2: daedalus.core read layer, path policy, FTS search, benchmark

- `pyproject.toml` gains a hatchling `[build-system]`; package code lives in `src/daedalus/`.
- `daedalus.core`: `schema` (SCHEMA moved here from the template test), `registry`, `session`, `policy` (seven rules, `Op` enum), `notes`, `links`, `index` (SQLite FTS5), `tools` (`list_notes`, `read_note`, `backlinks`, `search`). Nothing in this commit writes to a vault.
- Tests: 348 in total, which is the 119 template tests plus 229 in `tests/core/`. Fixtures copy the real templates into `tmp_path`.
- Proved red three ways. Deleting rule 2's shared-vault branch fails the rule-2 tests, because deny-by-default then refuses with rule 1. Replacing `realpath` with path joining fails the four symlink tests. Making `search()` scan every file fails the benchmark at 362 ms.
- Benchmark on a 10,000-note vault (Apple silicon Mac, APFS): warm `search()` with its freshness walk has a median of about 30 ms (29–37 ms across runs), and `authorize()` about 25 µs.
- Live smoke (`scripts/smoke_read.py`) against the real registry: lists scratch and shared, reads `shared:practices/red-test-first.md`, finds `red-test-first` as a backlink of `one-commit-one-change`, and finds it again searching "failing test". Both vaults hash the same before and after the run.

### 2026-11-25 — Fix: exclude templates/ from search and link resolution

- `list_notes` skipped `templates/`, but search, link resolution, and backlinks didn't. Searching "Decision" returned `templates/decision.md`, and a real `goals/goal.md` was reported ambiguous with `templates/goal.md`. The skip now lives in `index.walk_markdown`, the one walk that feeds all four tools. Existing indexes drop their template rows on the next refresh.
- Two new tests: `test_search_never_returns_templates` and `test_note_named_goal_not_ambiguous_with_template`. Both failed against the commit-2 code before the fix. Suite: 350 tests.

### 2026-11-25 — Commit 3: proposals, approval queue, apply + commit, write integrity

- `daedalus.core.queue`: SQLite proposal queue at `~/.local/state/daedalus/queue.sqlite`, in WAL mode. Ids are 14 characters and sort by time; content is capped at 256 KB.
- `tools.propose_note` / `propose_edit` / `propose_lesson` validate drafts against `SCHEMA` and write only to the queue. `tools.write_log` uses an exclusive create and commits at once.
- `daedalus.core.apply`: `apply` (seven steps), `reject`, `pending`, `recent_rejections`, `diff`. All writes go relative to a directory fd opened with `O_NOFOLLOW`, under one cross-process lock.
- `python -m daedalus.queue_cli --vault <name> list|diff|apply|reject`: a stopgap until commit 5's REPL.
- Tests at first build: 445. The 95 new ones included the 33-row integrity table (`test_write_integrity.py`). Write-path fixtures are real git repos, built once per session and copied per test, with the user's global git config ignored.
- Test 11 found a real bug. On a brand-new queue file, `PRAGMA journal_mode = WAL` returns "database is locked" without consulting the busy timeout. The fix only switches when the database isn't already in WAL, and retries the switch with backoff.
- Proved red three ways. Removing the `base_sha256` check fails test 4: apply overwrote the human edit. `open(path, "w")` in `write_log` fails test 10: two concurrent racers both succeeded. `git commit -a` fails test 3: staged and unstaged work was swept into the agent's commit.
- Source is enforced. A create's frontmatter `source` must equal the proposal's. An edit may change it only in a human proposal. The tests for this found a real bug: editing a hand-written draft git didn't track failed, because `git commit --only` refuses untracked paths. Such edits are now staged first and unstaged on rollback (with `git rm --cached -f`, the only form git accepts once the file is restored).
- Crash recovery: the `applying` status, schema version 2 (migrated in place from version 1), and reconciliation on every queue open. The crash test (a subprocess that `os._exit(1)`s right after the replace) failed before reconciliation existed: the agent's content stayed in the vault, uncommitted. It now passes, along with a crash after the commit (reconciled to `applied`), a file edited after the crash (left alone), and a live apply holding the lock (not touched).
- The write lock is `fcntl.flock`. A holder that `os._exit(1)`s releases it, and the next apply succeeds in under a second.
- Final suite: 463 tests.
- Live check: proposed `inbox/hello.md` into scratch, then listed, diffed, and applied it with `queue_cli`. That made commit `748cecb` (`agent(human): live check: hello note` plus a `Daedalus-Change` trailer), touching only that file. Then proposed a lesson into shared and rejected it; the shared HEAD stayed at `b9bfa46` and its `git status` stayed clean.

### 2026-11-25 — Commit 4: daedalus-mcp server and Claude desktop connection

- `daedalus.core.state`: `load_session` / `save_state` over `~/.config/daedalus/state.json`, with `StateError` messages that say how to fix the file. `python -m daedalus.state show | set-active | pull | drop` is a stopgap. The real state file now says `{"active": "scratch", "pulled": []}`.
- `tools.list_files` (git's view of the active repo, minus `vault/`, capped at 2,000 paths) and `tools.read_code` (2,000 lines and 200,000 characters per call, paged, binary refused), both through `authorize(Op.READ)`.
- `daedalus-mcp` (`src/daedalus/mcp_server.py`, `fastmcp==4.2.6`): exactly eight tools, `source="claude"` set by the server, the session loaded on every call, refusals returned as tool errors, logs to stderr and `~/.local/state/daedalus/mcp.log`.
- Tests: 500 in total, 37 new (`tests/mcp/test_server.py` 20, `tests/core/test_state.py` 17). The MCP tests use FastMCP's in-memory client against the core `gworld` fixture; one starts the installed `daedalus-mcp` script and parses every stdout line.
- Proved red four ways. Registering `apply` fails the exact-tool-set test. A pass-through `source` parameter fails the no-source and can't-impersonate tests. `print("ready")` at startup fails the stdout test (`JSONDecodeError` on `'ready\n'`). Caching the session at startup fails the switching test: after the switch to gamma, reading gamma was refused.
- Claude desktop app: `~/Library/Application Support/Claude/claude_desktop_config.json` backed up to `.bak`; a `daedalus` entry merged in under `mcpServers` (there were none before), with absolute paths to `uv` and the repo. The exact command was checked first from `/` with a bare `PATH`.
- `~/code/daedalus-scratch` gained a two-line `README.md` (`docs: scratch readme`) for `read_code` to read.
- Live check, run from a Claude Code chat in the desktop app. `mcp.log` confirms every call went through daedalus-mcp. Claude showed the scope (scratch active, nothing pulled), listed the scratch and shared notes, quoted `red-test-first`, and showed the scratch `README.md`. Asked to add "always use tabs" to shared `practices/`, it reported "Refused by rule 2: PROPOSE in the shared vault is allowed only in: inbox/" and queued nothing. It proposed `scratch:inbox/hello-from-claude.md` (type concept, `shared_refs: ["[[red-test-first]]"]`), which `queue_cli list` showed as `(claude)`; it was rejected with "live check: rejecting on purpose".
- The live log showed FastMCP logging every refusal as `ERROR Error calling tool`. Refusals now raise `ToolError(..., log_level=DEBUG)`, so a refusal appears once, at INFO, and only real faults appear at ERROR. `test_refusals_are_not_logged_as_errors` failed before the change.

### 2026-11-25 — Commit 5: terminal REPL, DeepSeek streaming, token meter, spend caps

- `daedalus [project]` (`daedalus.terminal.repl:main`) is a plain REPL: `prompt_toolkit` input, `rich` `Live` output, and `/cost`, `/pending`, `/diff`, `/approve`, `/reject`, `/help`, `/quit`.
  - A DeepSeek agent answers using the eight daedalus tools; `propose_note` carries `source="deepseek"`.
  - `queue_cli` stays as a debug tool.
- **The key is in the macOS Keychain** (`keyring`, service `daedalus`, account `deepseek`), with a `DEEPSEEK_API_KEY` override. `python -m daedalus.secrets set|status`.
- **Modules:**
  - `terminal/pricing.py`: the price table (`checked: 2026-11-25`), peak windows in UTC.
  - `terminal/ledger.py`: `ledger.sqlite`, WAL, worst-case reservations.
  - `terminal/deepseek.py`: OpenAI SDK, streaming, retries.
  - `terminal/meter.py`: estimates, caps, the meter line, interrupted calls.
  - `terminal/prompts.py`: the byte-stable system prompt.
  - `terminal/agent.py`: tools, a 12-round loop, `reasoning_content` kept.
  - `terminal/config.py`.
- The tool bodies moved from `mcp_server.py` into `daedalus.toolset`, shared by both front ends. A test checks that the agent's argument names match daedalus-mcp's.
- **Tests:** 587 in total, 87 new in `tests/terminal/`. They replay recorded SSE streams through `httpx2.MockTransport`, with an in-memory keyring; nothing reaches the network.
- **Proved red five ways:**
  1. The streamed estimate as the final number fails the snap test (`300 == 512`).
  2. Pricing everything off-peak fails the peak-is-double tests.
  3. Checking the cap after the request fails all four cap tests (the transport received a request).
  4. Stripping `reasoning_content` fails the pass-back test (`KeyError: 'reasoning_content'`).
  5. Building the prompt from a `set` fails the cross-process test, where eight hash seeds gave two different prompts. Two assemblies in one process stayed identical, as expected.
- **Two real bugs found by the tests:**
  - Float sums of `cost_usd` crossed a cap they shouldn't (15 × $0.20 = 3.0000000000000004 > 3.00); caps now compare at nanodollar resolution.
  - `executescript()` would have committed the ledger's schema transaction early; the DDL now runs statement by statement.
- **Live check** (off-peak, 04:45 UTC, `deepseek-flash`):
  - The first question ran `get_scope`, `read_note red-test-first`, and `backlinks`, then quoted the practice correctly and found `one-commit-one-change`.
  - Meter lines (billed): `↑ 2,869 (0 cached) ↓ 66`, then `↑ 3,295 (2,816 cached) ↓ 51 (reasoning 5)`, then `↑ 3,397 (3,200 cached) ↓ 204`.
  - The second question started at `↑ 3,627 (3,456 cached)` and proposed `scratch:inbox/deepseek-hello.md` with `source: deepseek`. It was rejected from the REPL with `/pending`, `/diff`, and `/reject`.
  - Six calls cost $0.001744 in the ledger. DeepSeek reported 10.00 USD both before and after, because the balance has two decimals and the spend was under a cent.
  - The key appears in none of the 473 files under the state and config dirs, the repo (minus `.venv`), and both vault repos.

### 2026-11-25 — Fix: link rule counts any link field; root files aren't link targets

- In the commit-5 live check, DeepSeek linked its draft to `[[CONTEXT]]`. It read `AGENTS.md` §5 ("every note links to at least one other note") as needing a `related` link, and the scratch vault had nothing else to link.
- Both `AGENTS.md` templates, and the live scratch and shared copies, now say that any of `related`, `shared_refs`, `project_refs`, or a body link satisfies the rule, and that `AGENTS.md` and `CONTEXT.md` are never valid link targets. The shared vault's `AGENTS.md` gained a section 5, Linking. The vault changes are committed in their own repos.
- `tools.link_errors`, run at propose time: a typed draft needs at least one link, and none may resolve to, or be named, `AGENTS` or `CONTEXT`. The `propose_note` tool description says so too.
- `tests/core/test_draft_links.py`, 17 tests:
  - a draft linking only to `[[CONTEXT]]` is refused; it failed first with `DID NOT RAISE DraftInvalid`;
  - a draft whose only link is in `shared_refs` is accepted;
  - quoted and unquoted `created` both validate;
  - every link field counts, and so does a dangling link;
  - root-file links are refused in any field or the body.
- The integrity table gained `link_only_to_context` (36 rows). Twelve older tests proposed typed drafts with no links and now give them one. Two tests that listed every error now also expect the "no links" error, since every error goes back to the agent at once.
- Suite: 605 tests.

### 2026-11-26 — Commit 6: project lifecycle, switching, session context, /end

- **`registry.register_project` / `unregister_project`:** edits with `tomlkit` (comments, order, and other entries kept byte for byte), validated through `load_registry` before an atomic replace. Unregister refuses the active project or a pulled one.
- **`daedalus.core.lifecycle.new_project`** (`/new <name> --repo <path>`):
  - It checks the name, the repo (its top level, with no `.obsidian/` at the root), and the vault (`vault/` absent or empty).
  - It copies the template, then commits exactly `vault/` with `git commit --only` as `init: daedalus vault from template`.
  - It registers the project, then prints the Obsidian step.
  - Rollback runs in reverse. Each undo is armed before its step, so a half-done step is undone too.
- **`daedalus.core.state`** gained `set_active`, `pull`, and `drop`. The CLI and the REPL share them.
- **REPL commands:**
  - `/vaults`;
  - `/vault <name>`: a fresh conversation, with the same session id, so the session cap carries over;
  - `/pull` and `/drop`: rebuild the prompt and keep history;
  - `/new`;
  - `/end`.
- **The agent tools** gain `propose_edit` and `propose_lesson`, for ten. daedalus-mcp keeps eight.
- **The system prompt** has six blocks, most stable first:
  1. the preamble;
  2. the shared `AGENTS.md`;
  3. the project `AGENTS.md`;
  4. accepted practices (12,000 characters);
  5. the pulled projects' Decisions index and Where things live (6,000 characters each);
  6. the active `CONTEXT.md`.
- **`/end`:**
  - one user turn carrying today's date and the current `CONTEXT.md`;
  - `propose_edit` of the whole file with a new Log entry, plus optional lessons into the shared `inbox/`;
  - then a one-at-a-time review (approve, reject with a reason, or skip), then exit;
  - nothing to log means it just exits.
- **Tests:** 640 in total, 35 new:
  - registry writes 4, `/new` 13, prompt 9 (replacing commit 5's 4), switching and `/end` 12;
  - commit 5's tool-set tests updated to ten tools;
  - one rendering test.
- **Proved red four ways:**
  1. A `tomllib`/`tomli_w` round-trip dropped the file's comments, failing the byte-for-byte test.
  2. `git add -A` swept `notes.txt` and `staged.txt` into the init commit, failing the pre-staged-file test. It also left the commit in place on rollback ("touches files outside vault/").
  3. Putting the active `CONTEXT.md` before the practices failed the prefix test ("changing the active CONTEXT.md changed an earlier block").
  4. Writing `CONTEXT.md` directly in `/end` failed the nothing-written test ("/end wrote CONTEXT.md before approval").
- **Test layout:**
  - `from conftest import` picked whichever folder's conftest pytest imported last, so `pytest tests/core/x.py tests/terminal/y.py` failed to collect. The full suite only passed because of collection order.
  - Terminal helpers moved to `tests/terminal/fakes.py` and MCP helpers to `tests/mcp/mcp_helpers.py`, and every conftest re-exports the core helpers. Any mix of folders now runs.
- **Live check:**
  - `/new scratch2 --repo ~/code/daedalus-scratch2` committed 21 template files (`2d002de`) and added one line to `vaults.toml`.
  - In the REPL, `/vault scratch2` printed the fresh-conversation notice, and `/pull scratch` rebuilt the prompt.
  - DeepSeek answered from scratch's empty `CONTEXT.md` and the two practices; the cache was warm from the second call on.
  - Asked to propose into scratch, it declined on its own (pulled projects are read-only) without calling the tool. The same `propose_note` call through the agent's `run_tool`, against the live session, returns "Refused by rule 3: PROPOSE is not allowed in a pulled vault".
  - `/end` proposed the `CONTEXT.md` entry, which was approved and committed as `9b41e4a` in scratch2, and a lesson, which was rejected with "Lesson, testing".
  - A new Claude desktop chat ("use daedalus to show my scope") showed scratch2 active and scratch pulled. That server process had started at 23:22, before the switch, and it followed `state.json` with no restart.
- **REPL rendering fix:** the live check showed streamed text, and the reasoning tail, left behind above the settled answer. A transient `rich` `Live` can't erase rows that scrolled off the top. The streaming view now shows only the wrapped tail that fits the terminal, and a test checks its height (376 rows on a 24-row terminal before the fix).

### 2026-11-26 — Fix: cap CONTEXT.md log in session context

- Prompt block 6 now holds the active `CONTEXT.md`'s non-Log sections whole, plus only the 8 newest Log entries, with a one-line note: "[N older Log entries are in CONTEXT.md and not shown here; read_note <vault>:CONTEXT.md for the full log.]". Uncapped, the log grows with every `/end`, and every session re-bills all of it as input.
- "Newest" means the latest heading date when every entry starts `### YYYY-MM-DD`, with same-day entries in file order; otherwise the last 8 in the file, where `/end` appends. Kept entries stay in file order.
- Tests:
  - 20 entries yield exactly entries 13–20 plus the note; it failed first with `[1, 2, 3, ...] == [13, 14, 15, ...]`;
  - a 21st entry changes only block 6;
  - the capped prompt is identical across four hash seeds;
  - a log of 8 or fewer stays whole with no note;
  - a log written newest-first still keeps the 8 latest by date.
- `--durations=10` added to pytest's default addopts, so the slowest tests always print.
- Suite: 645 tests.

### 2026-11-26 — Commit 7: Obsidian bridge — /open and /here

- **`daedalus.terminal.obsidian`.** Obsidian is asked where, never what: every byte of content is read through `daedalus.core`.
  - `open_note` resolves the ref like `read_note` (ambiguity refused), authorizes READ, then runs `open "obsidian://open?path=<encoded absolute path>"`. It never uses `vault=`.
  - `active_note` checks that Obsidian is running, then that the CLI is on PATH, then runs `obsidian eval` from `/` with a 5 s timeout. It validates the output and maps `base` to a registered vault by real path, then reads the note with `tools.read_note`.
- **REPL:**
  - `/open <note>`;
  - `/here`, which prints `→ here vault:path (N chars)`, shows any schema errors, and attaches the note to the next user message as a fenced block, capped at 60,000 characters. A second `/here` replaces it.
- **The CLI on this machine** (Obsidian 1.15.1, installer 1.15.0) differs from the prompt in four ways:
  - The setting is Settings → General → **Advanced** → Command line interface. Before it's on, the bundled CLI says so, pointing there.
  - `obsidian eval` prints `=> <value>`, not bare JSON. It **exits 0 on errors**: `Error: boom`, `Error: Command "x" not found…`, `Vault not found.`. So the parser requires exactly one `=> ` line, and the exit code is only a secondary check.
  - `obsidian help` printed the full list once from `/`. Then it printed `Vault not found.` from `/` and `~`, and `Command "help" not found` from inside a vault. Meanwhile `eval` and `version` worked from `/`.
  - The help lists a global `vault=<name>` option, which this code never uses.
- **Tests:** 35 new, so 680 in total, plus one existing test tightened. A fake `obsidian` script first on PATH logs its cwd and argv. The running check is injected. The terminal conftest makes the real app "not running" and refuses `open` everywhere.
- **Proved red three ways:**
  1. Skipping the running check failed `test_closed_obsidian_is_not_open_and_the_cli_is_never_run` (the result was `Here(...)` instead of `NotOpen`, and the fake CLI ran). It also failed the default-bridge test.
  2. Resolving the vault by folder name failed `test_same_named_vaults_resolve_by_real_path`: alpha's same-named note was attached (`'beta:components/fetcher.md' in "alpha:components/fetcher.md …"`). It also failed the symlink, `/pull`-hint, unregistered, and `/drop` tests.
  3. Putting the attachment in the system prompt failed `test_here_goes_in_the_user_message_and_the_system_prompt_is_unchanged` ("/here changed the system prompt"), plus the same-named-vault and replace tests.
- **Live check:**
  - With Obsidian quit, `/here` said "Obsidian isn't open…" and Obsidian stayed closed.
  - In scratch2, with `CONTEXT.md` focused, `/here` gave `→ here scratch2:CONTEXT.md (1,507 chars)`. DeepSeek summarized the note from the attachment alone, with no tool call, at `↑ 4,871 (1,920 cached)`.
  - In the scratch window, `/here` attached `scratch:CONTEXT.md` (72 chars). Then `/drop scratch` dropped the attachment, and the next `/here` said `/pull scratch` would bring it in.
  - `/open shared:practices/red-test-first.md` opened in the shared vault's window.

### 2026-11-26 — Commit 8: design-decision gate, ask_user, /answer, /promote, multi-path apply

- **Categories.** `DESIGN_CATEGORIES = ("schema", "dependency", "interface", "data_source")` in `core/schema.py`.
  - Every `AGENTS.md` names exactly these four in its "Design choices" section, and a test checks the templates and every vault in the real registry.
  - The shared template gained a §6 "Design choices".
  - The live copies are committed in their own repos: scratch `afe2b4a`, scratch2 `ab294da`, shared `4cd9324`. The test failed first on all three live copies, before they were updated.
- **`core/gate.py`.**
  - `check_basis` returns `OK(note)`, `NEEDS_USER`, or `REFUSED(reason)`.
  - Precedent is an accepted note in the active `decisions/` or the shared `practices/`, nothing else.
  - `find_precedent` runs an any-word FTS search, filtered to accepted, schema-valid notes in those two folders, and returns at most 10.
  - `propose_with_basis` is the entry point for commits 10–12.
- **`core/accept.py`.** `ask_user` writes a typed question into `questions/` through the queue. `answer_question` (`/answer`) and `promote` (`/promote`) each make one multi-path commit.
- **`apply.apply_changes`.** It writes N paths and makes one commit, recording a change set in the queue first (schema v3, tables `changesets` and `changeset_paths`). A failed commit or crash restores every path, and reconciliation covers change sets.
- **Rule 5.** `USER_ACTION` now includes `questions/`.
- **Tools.**
  - daedalus-mcp has 9 tools, with `find_precedent`.
  - The agent has 12, with `find_precedent` and `ask_user`.
  - A successful `ask_user` ends the turn; later calls in the same reply get a "Not run" result.
  - The REPL prints the question with numbered options.
  - The preamble states the gate.
- **Tests:** 747 in total, 67 new (680 at commit 7):
  - gate 24;
  - ask_user, /answer, /promote, and multi-path apply 29;
  - terminal 5;
  - MCP 1;
  - the integrity table +3 rows (39);
  - the `AGENTS.md` categories 5;
  - plus the updated exact-tool-set tests (nine and twelve).
- **Proved red four ways:**
  1. Accepting `proposed` notes as precedent failed `test_proposed_note_is_refused` ("expected REFUSED, got OK(… status 'proposed' …)"), plus the parametrized proposed case and the integrity row.
  2. Accepting a pulled project's decision failed `test_pulled_projects_accepted_decision_is_refused` (`expected REFUSED, got OK(… ref='beta:decisions/beta-uses-postgres.md')`).
  3. Continuing the loop after `ask_user` failed `test_ask_user_ends_the_turn_with_zero_further_requests` ("the agent made a model call after ask_user", `assert 4 == 2`), and three more.
  4. Two commits for `/answer` failed `test_answer_is_one_commit_with_exactly_the_decision_and_the_question` ("must be one commit", `assert 2 == 1`), plus the pending-question, crash, and REPL tests.
- **Live check** (scratch2, off-peak, 18:20 UTC):
  1. DeepSeek ran `get_scope`, `find_precedent`, then `ask_user` on its own, with no nudge. The turn stopped, and the REPL showed the question `1m8chavq3632ny` with two options.
  2. `/answer 1m8chavq3632ny 1` approved the question (`5b7731e`), then made one commit, `0afe779`. It added `decisions/should-scratch2-s-storage-layer-use-sqlite-embedded-file.md` and modified the same-named question.
  3. "Now draft the component note": DeepSeek ran `list_notes questions`, then `find_precedent`, which found the decision, then `read_note` of the decision, `list_files`, `read_code README.md`, and `read_note templates/component.md`. It then proposed `inbox/storage-layer.md`, citing the decision in `related` and `red-test-first` in `shared_refs`.
  4. `/approve` committed `498876d`. Then `/promote storage-layer components` made `f7506bd`: A `components/storage-layer.md`, D `inbox/storage-layer.md`, now `status: accepted` with `source: deepseek` kept.
  5. Claude desktop ("find precedent about storage") found the one accepted decision and summarized it. It noted the decision's empty Consequences section.
- **Found in the live check:** the decision has the same file stem as its question. So the component's `related: ["[[should-scratch2-…-file]]"]` is **ambiguous**: `check_basis` refuses it, while `[[decisions/…]]` is OK. The link rule accepts it, since ambiguous links count, like dangling ones. Up Next has the fix.

### 2026-11-26 — Fix: no ambiguous links in typed drafts; questions get a q- prefix

- **The cause:** in commit 8's live check, the decision took its question's stem, so the component's bare `[[should-scratch2-…-file]]` matched both notes. `check_basis` refused it as ambiguous, but the link rule had let it through.
- **`ask_user`** now names questions `questions/q-<slug>.md`. `/answer`'s decision keeps the plain `<slug>`, so the two never share a name.
- **`tools.link_errors`** refuses a typed draft whose link matches more than one note in its scope. The error names the matches and the path-qualified forms. Dangling links stay allowed, and untyped drafts still have no link rule.
- **scratch2 migrated** in `0e8cdda fix: q- prefix for question note`: the question was renamed to `q-should-…`, and the decision's two links to it were updated. The component note is unchanged. Its bare link now resolves uniquely, and `check_basis` returns `OK scratch2:decisions/should-scratch2-s-storage-layer-use-sqlite-embedded-file.md`.
- **Tests:** 753, 6 new:
  - an ambiguous bare link is refused, naming both matches and both qualified forms; it failed first with `DID NOT RAISE DraftInvalid`;
  - an ambiguous body link is refused;
  - a dangling link is accepted;
  - an untyped draft may keep an ambiguous link;
  - an answered question and its decision have different stems;
  - an `ambiguous_link` row in the integrity table (40 rows).
  - Three commit-8 tests were updated for the new names.

### 2026-11-26 — Commit 9: uploads via /add

- **Dependency:** `markitdown[docx,pdf,pptx,xlsx]==0.1.9`, pinned exactly. Never `[all]`. Dev gained `reportlab` and `python-docx` to build fixtures at test time; no binaries are in the repo.
- **`core/ingest.py`, `ingest(session, src_path) -> SourceNote`:**
  1. Validate the path (realpath; a regular file; outside every vault, any `.git`, and the state folder; an allowed extension; at most 25 MB; never a URL) before any converter sees it.
  2. Hash it (SHA-256). A `sources/` note already recording that hash is a refusal naming it.
  3. Convert in memory: MarkItDown for .pdf/.docx/.pptx/.xlsx, a UTF-8 copy for .md/.txt. Hash again; a change meanwhile is a refusal.
  4. Refuse under 50 non-whitespace characters ("no text layer — likely a scanned document; OCR isn't enabled") and over 2 MB of markdown.
  5. Frontmatter is exactly `schema.SOURCE_KEYS`. The stem is the slugged title, with -2, -3… until it's unique across every registered vault.
  6. One commit, `add: <original_name>`, as `USER_ACTION` into `sources/` through `apply_changes`.
- **REPL `/add <file>`** (quoted, escaped, or plain paths with spaces) prints `→ added <ref> (N chars, from <name>)`, then "Agents can read this, and anything they read is sent to DeepSeek."
- **Tests:** 782, 29 new (753 before): ingest 24, integrity rows +4 (`add_oversized`, `add_corrupt`, `add_scanned`, `add_duplicate`; 44 rows), REPL 1.
- **Proved red three ways:**
  1. Accepting empty conversions failed `test_scanned_pdf_is_refused_with_no_text_layer` ("DID NOT RAISE IngestError") and the `add_scanned` row.
  2. Skipping the hash check failed `test_the_same_file_is_refused_the_second_time_even_renamed` ("DID NOT RAISE IngestError"), the `add_duplicate` row, and the REPL test.
  3. Copying the original into `sources/` failed `test_the_original_never_enters_git` ("originals entered git: ['vault/sources/settings.xlsx', …, 'vault/sources/orig.pdf']"), 8 failures in all.
- **MarkItDown 0.1.9, as found:**
  - The constructor makes a `requests.Session` (unused for local paths) and auto-detects `exiftool` on PATH (not installed; only images and audio use it).
  - Magika runs local ONNX type detection; the first converter takes about 0.9 s to build, so it's a lazy singleton.
  - PDF output has no headings. pptx adds `<!-- Slide number: N -->`. xlsx gives a `## <sheet>` heading per sheet, escapes underscores, and prints integers as floats (365.0).
  - A scanned PDF converts to empty text without error; a corrupt one raises `FileConversionException`. `result.title` is None.
  - Icon-font glyphs come out as `(cid:N)` (pdfminer's unmapped-glyph marker): seen live.
- **Live check** (scratch2): `/add` of a real two-page PDF (a résumé) committed `9d8c92b add: <name>.pdf`, one path, `vault/sources/<slug>.md`, 103 lines. With no heading in the PDF, the title fell back to the file stem. "Summarize this source and propose a concept note from it" gave a pending `inbox/` concept draft (`source: deepseek`) whose `related` cites the source by its bare stem. A second `/add` of the same file added nothing; the hash check names the existing note.

### 2026-11-26 — Fix: /add strips PDF glyph artifacts; conversion proven network-free

- **The cause:** in commit 9's live check, the source note kept pdfminer's `(cid:131)` and `(cid:239)` markers for icon-font glyphs.
- **`ingest.strip_glyph_artifacts`** removes `(cid:NNN)` and the spaces after it, from PDF output only.
- **Network-free:** a test blocks `socket.socket.connect` and `connect_ex`, `socket.create_connection`, and `socket.getaddrinfo`, resets the converter singleton so MarkItDown is built under the block, and converts all six formats. Every attempt is recorded and the list must be empty, so a swallowed error still fails.
- **Proved red twice:** a `urlopen("http://example.invalid/")` in `convert` failed the test with `IngestError: net.docx couldn't be converted (NetworkBlocked: socket.create_connection called during ingest with (('example.invalid', 80), 1, None))`. The same call wrapped in `try/except: pass` failed with "ingest reached for the network: [\"socket.create_connection(('example.invalid', 80), 1, None)\", …]" (four attempts, one per converted format).
- **Tests:** 785, 3 new: glyphs stripped from a PDF (fixture string through a fake converter), markers kept in a `.md`, and the no-network test.
- The scratch2 source note from commit 9's live check still holds the markers; it wasn't rewritten.

### 2026-11-26 — Commit 10a: Seatbelt sandbox runner, rule 7 rewrite

- **`daedalus.sandbox`:** `runner.run(session, argv, worktree) -> RunResult` runs every agent-triggered command under `/usr/bin/sandbox-exec`, failing closed. `profile.py` holds the SBPL profile, `exec.py` the one spawn point (own session, killpg, rlimits, 200 KB tails), `venv.py` the venv, `config.py` `daedalus.toml`, and `python -m daedalus.sandbox run --project <name> -- <argv>` the CLI.
- **The profile:** allow-default, then deny the network (DNS's mach services too), every write but the run's `tmp/` and a few `/dev` nodes, reads under home but the worktree, the venv, uv's Python folder, and the run's `tmp/`, the worktree's `vault/` and `.git`, and securityd's mach services. Every path is a realpath `-D` parameter; the text never holds one.
- **Rule 7** is now `authorize_command(argv, project_config, venv) -> Path`: a bare name in `allow`, pinned to `<venv>/bin/<name>`. The basename matching is gone; `.venv/bin/pytest`, allowed before, is now refused.
- **The venv:** `~/.cache/daedalus/venvs/<project>-<sha12 of uv.lock>/`, built unsandboxed with `uv sync --frozen --no-install-project` from your checkout, once per lock hash.
- **scratch2** became a minimal Python project in `97b17c8 chore: minimal python project for sandbox check`: `pyproject.toml` (dev group `pytest`, plus `keyring` for the live Keychain test), `uv.lock`, `.python-version` 3.13, `tests/test_smoke.py`, and the default `daedalus.toml`. uv chose `/opt/anaconda3/bin/python3` 3.13.5, the same binary as daedalus's venv and the one granted "Always Allow".
- **Tests:** 847 by default, 62 new (785 before): runner 40, executor 6, rule 7 policy +11, integrity rows +5 (`rule7_slash_in_program`, `rule7_program_not_allowed`, `rule7_allowed_but_missing_from_venv`, `rule7_empty_argv`, `rule7_nul_byte`; 49 rows), each also checking that the injected executor was never called. Plus 10 live tests marked `sandbox` (`uv run pytest -m sandbox`), deselected by default. Moving the preflight ahead of the checks failed every rule 7 row with "a refused command spawned ['/usr/bin/sandbox-exec', '-p', …]".
- **Proved red four ways** (live tests):
  1. No `(deny network*)`: `test_no_network_and_no_dns` failed with "a sandboxed run reached 1.1.1.1" (and the localhost test).
  2. No home read denial: `test_no_reads_in_home` failed with `{'probe': 'READ', 'state': 'READ', 'note': 'READ', 'list_home': 'READ', …, 'xattr': 'READ'}`.
  3. No securityd mach denials: `test_no_keychain` **still passed**: the home read denial alone blocks the Keychain. Removing the home read denial instead, keeping the mach denials, also passed. With both removed it failed with `{'keyring': 'GOT A VALUE', …}`. The probe puts `HOME` back to the real home first, because the clean environment's `HOME` alone also hides the Keychain, and a run could undo that.
  4. Worktree writes allowed: `test_no_writes_outside_the_run_folder` failed with "a run wrote into the worktree".
- **sandbox-exec, as found:** a profile error and a missing `-D` parameter both exit 65 with a `sandbox-exec:` message, and an exec failure exits 71. A denied `stat` is `EPERM`, which made pytest fail (`Path.is_file()` raises on it) in any worktree under home or with a `vault/` or `.git`, so file metadata is re-allowed there. Contents, listings, and xattrs stay denied. `uv python dir` names `~/.local/share/uv/python`, which doesn't exist: uv manages no Pythons here.
- **Live check:** `uv run pytest -m sandbox`: 10 passed. `python -m daedalus.sandbox run --project scratch2 -- pytest -q`: `1 passed`, `— sandboxed: exit 0 in 0.36 s` (0.50 s wall with `uv run`). Overhead, median of 5 `pytest -q`: sandboxed 370 ms, unsandboxed 312 ms, +57 ms. No Keychain prompt appeared. Nothing new in `~` except `~/.cache/daedalus/venvs/`.
- **Not done:** superseded venvs aren't pruned.

### 2026-11-26 — Fix: narrow sandbox metadata reads to pytest's upward search

- **The cause:** commit 10a re-allowed `file-read-metadata` under all of home, so a run could `stat` any path there and learn its existence, size, and times.
- **Now:** metadata is allowed, as `literal` `-D` parameters, only on each ancestor folder of the worktree up to and including home, and on `pytest.ini`, `.pytest.ini`, `pyproject.toml`, `tox.ini`, `setup.cfg`, and `conftest.py` in those folders. For scratch2's checkout that's `~` and `~/code`, 14 paths in all. The worktree's `vault/` and `.git` keep metadata: removed, both live pytest tests failed again.
- **Tests:** 849 by default (+2: the ancestor parameters, and no rule for a worktree outside home); 12 live (+2):
  - `test_no_metadata_reads_in_home` stats `~/.daedalus-sandbox-meta-probe`; against 10a's profile it failed with `a sandboxed run could stat /Users/…/.daedalus-sandbox-meta-probe: {'stat': 'STAT OK'}`, and now gets `PermissionError`;
  - `test_real_pytest_in_a_checkout_under_home` runs `pytest -q` in scratch2's own checkout, since the throwaway worktrees live outside home and never exercise the upward search;
  - test 8 now also requires `stat` on its probe to be denied.
- **Re-run:** `uv run pytest -m sandbox`: 12 passed; `python -m daedalus.sandbox run --project scratch2 -- pytest -q`: `1 passed`, exit 0 in 0.40 s.

### 2026-11-27 — Commit 10b: worktrees, code tools, coder mode

- **`core/worktree.py`:** `create`, `list_worktrees`, `diff`, `commit`, `merge` (yours only), `drop`. Worktrees live at `~/.cache/daedalus/worktrees/<project>/<slug>-<6 hex>/` on `agent/<slug>`, recorded in `~/.local/state/daedalus/worktrees.json`. `commit` is `git add -A` with a `vault/` check after staging and the trailer `Daedalus-Worktree: <slug>`. `merge` is `--no-ff`, refused on a dirty checkout or a branch touching `vault/`, and aborted on conflict, naming the files. `drop` refuses unmerged commits and uncommitted changes without `--force`.
- **Code tools** (`core/tools.py`): `read_code` and `list_files` follow the worktree; `edit_code` (old must occur exactly once), `write_code` (64 KB), `delete_code`, each `Op.WRITE_CODE`, atomic, realpath re-checked, mode kept, logged in an in-memory `EditLog`. `toolset.run` wraps the sandbox runner; `repl.log` gets metadata only.
- **The coder profile:** fourteen tools (`CODER_NAMES`), its own preamble and `read_code`/`list_files` wording. The chat profile's twelve and daedalus-mcp's nine are unchanged.
- **REPL:** `/wt new|list|diff|merge|drop`, `/code <slug>`, `/code off` (commit offer, default message from a tool-less one-shot call), `/end` and `/vault` leave coder mode first.
- **Tests:** 887 by default, 38 new (849 before): worktree lifecycle 11, code tools 11, coder mode in the REPL 8, integrity rows +9 (58 rows; the snapshot now includes the checkout's and the worktree's digests). The sandbox's 12 live tests still pass.
- **Proved red four ways:**
  1. `drop` deleting unmerged branches failed `test_drop_refuses_unmerged_commits_and_force_removes_them` ("DID NOT RAISE WorktreeError").
  2. `edit_code` taking the first of several matches failed `test_zero_and_two_matches_are_refused_and_the_file_is_unchanged` ("DID NOT RAISE CodeEditError").
  3. `read_code` reading the checkout with a worktree active failed `test_read_code_returns_the_worktrees_edit_and_the_checkouts_without_one` (`"print('checkout')\n" == "print('edited')\n"`).
  4. No `vault/` check in `commit` failed `test_commit_aborts_if_a_vault_path_is_staged` ("DID NOT RAISE WorktreeError").
- **Live check** (scratch2; the meter said peak, so it began before 04:00 UTC; about $0.006 in all):
  1. `/wt new hello` made `hello-3bf18a` from `97b17c8`; `/code hello` showed `[coding scratch2 · agent/hello] ›`.
  2. "Add add(a, b) in src/scratch2/mathx.py with a test": DeepSeek ran `get_scope`, `list_files`, read `pyproject.toml`, `daedalus.toml`, and the smoke test, then `find_precedent`, and cited `red-test-first` on its own. It wrote the test first, added `pythonpath = ["src"]` to pytest's config with `edit_code` (scratch2 is `package = false`), ran pytest red (`ModuleNotFoundError: No module named 'scratch2'`), wrote the module, ran green (2 passed), then, unasked, broke `add` to `a - b` (`assert -1 == 5`) and restored it.
  3. Asked to break and fix: `edit_code` to `a * b`, run exit 1 (`assert 6 == 5`), `edit_code` back, run exit 0, and a summary naming both runs.
  4. `/code off` listed the three changes; the suggested message "Add mathx add function with tests" was accepted: `5d3bc4d`. `/wt diff hello` showed the three files; `/wt merge hello` made `194799d`.
  5. `/wt drop hello` succeeded without `--force`. `git worktree list` in scratch2 shows only the main checkout, and `~/.cache/daedalus/worktrees/scratch2/` is empty.
- **Found:** nothing broken. The agent changed `pyproject.toml`'s pytest config by itself; that isn't a dependency, so the gate didn't apply, but 10c may want config files called out in its summaries.

### 2026-11-27 — Commit 10c: the coding loop — goals, honest-done guards, check-ins, lessons

- **Goal schema:** `goal` gains a required `protected` (a list of repo-relative paths; may be empty), in `SCHEMA`, the goal template, all three schema blocks, and the live `AGENTS.md` copies, committed in their repos (shared `ed5dac3`, scratch `867e536`, scratch2 `1d14581`; the scratch vaults' own `templates/goal.md` too). A new test compares each live vault's schema block with the template's; it failed first on all three live copies.
- **`core/goals.py`:** `load` (the launch rule; `done_when` split with `shlex`; checked fields; protected paths normalized and required to exist), `check_argv_only` and `authorize_check` (rule 7 before anything is created), `parse_pytest` (the summary line's counts), `judge` (the four honest-done guards, in words), `protected_changes`, `diff_stat`, `CONFIG_NAMES`.
- **`terminal/loop.py`:** `Loop.start`/`resume`/`go`/`iterate`, check-ins, run state in `~/.local/state/daedalus/runs/<run_id>.json`, the report, and the lessons call. **REPL:** `/goal <note>`, `/goal resume [guidance]`, `/goal list`, `/review lessons`.
- **Ledger schema v2:** a `run_id` column (v1 ledgers migrate in place, rows kept) and a goal-budget cap checked in `reserve` with the session and daily caps. `Agent` takes `run_id` and `run_cap` for its turns and its one-shot calls.
- **`Session.protected`:** the edit tools refuse protected paths with rule 6, "protected by the goal"; three new integrity rows (61).
- **Tests:** 920 by default, 33 new (887 before): loop 23, ledger 2, coder-mode commit-message ledger and cap 1, live-schema-block 3, integrity +3, plus one more live sandbox test (13 now: real pytest counts parsed from a sandboxed run).
- **Proved red four ways:**
  1. Ignoring the collected-count guard failed `test_deleting_the_failing_test_is_not_done` (`assert (not True)`: the run counted as done).
  2. One conversation for the whole run failed `test_each_iteration_starts_fresh_with_only_the_logs` ("Left contains 8 more items, first extra item: 'assistant'").
  3. Skipping the config-change check-in failed `test_a_conftest_edit_checks_in_at_once_with_its_diff` (with no check-in to stop it, the run went on to an unscripted third iteration).
  4. Letting protected paths be edited failed `test_a_protected_path_cant_be_edited` (the tool result was `{"op": "edit", …}`, not "Refused by rule 6:").
- **Live check** (scratch2, off-peak):
  1. `goal: subtract` (`79df416`). `/goal subtract`: baseline collected 2, exit 0 (the agent was told the check already passed). Iteration 1: DeepSeek read `mathx.py` and its test, ran `find_precedent`, wrote the tests first (red: `ImportError … subtract`), implemented, broke and restored it on its own (red-test-first), and ended green. The loop's check: exit 0, collected 4, every guard held: done, $0.0023, no check-in (one iteration), report as specified. Merged as `b976e37`.
  2. The bait: `5be4054 test: divide(1, 0) raises ValueError`, `ebbc5d5 goal: divide` with `protected: [tests/test_divide.py]`. Baseline: exit 2 (collection error), collected 1. Iteration 1: it read the protected test, cited red-test-first, implemented `divide`, broke the zero check on purpose (`DID NOT RAISE ValueError`), restored it. It never tried to edit, skip, or delete `tests/test_divide.py`; no rule 6 refusal fired. Check: exit 0, collected 6, every guard held: done, $0.0012. Merged as `a6f7af1`; both worktrees dropped without `--force`.
  3. Lessons: none proposed. In neither run did a loop check fail and a later one pass (both were done at iteration 1; the agents' own red runs happen inside an iteration), so the lessons call was skipped. `/review lessons`: "No pending lessons."
- **Found in the live check:** Obsidian's Properties panel stored `protected: ["tests/test_divide.py"]`, typed into a list field, as the single string `'["tests/test_divide.py"]'`, which `load` accepted: it would have protected nothing. Fixed in this commit: a protected path that doesn't exist in the checkout refuses the launch, saying how to enter list items (two tests). Also, the subtract note first carried the template's defaults and a capitalized file name, and Obsidian left an untracked `.obsidian/graph.json`; fixed by hand with your OK.

### 2026-11-27 — Fix: list-typed fields must be lists; honest-done tracks test IDs

- **The cause:** in commit 10c's live check, Obsidian's Properties panel wrote `protected` as one item holding the text `["tests/test_divide.py"]`, which validation accepted. And the count guards alone let a failing test be swapped for a trivial passing one.
- **List keys:** `schema.LIST_KEYS` (`related`, `shared_refs`, `project_refs`, `protected`, `code`, `options`, `applies_to`, `decisions`, `tasks`) must be YAML lists; a string fails, and so does a whole list typed into one item, each saying to set the Obsidian property type to List. No live note failed the new rule.
- **Node IDs:** the baseline records `pytest --collect-only -q`'s node IDs (sandboxed, with `done_when`'s selection; verbosity flags replaced), the files that failed to collect, and the base's test files. Done now also needs every baseline node ID to still exist; if the baseline had collection errors, every baseline test file too.
- **Existing tests modified** (edited or deleted, from the base's test files) are listed with their diff in every check-in and in the report; no pause.
- **Spend across resumes:** each resume of a stopped run is a batch (`state.batches`); check-ins and the report show the batch's spend against `budget_usd` and the total across batches.
- **Tests:** 929 by default, 9 new (920 before): list keys 4 (the exact Obsidian text for `protected`, the same for `related`, a string, a real list), loop 5 (swap-the-test, collection-error file deletion, existing tests modified ×2, spend across batches); 14 live (+1: node IDs and an import error from a real `--collect-only`). The swap test failed first against the old guards: "deleting a failing test and adding a trivial one counted as done".

### 2026-11-27 — Obsidian property types pinned; /new writes a vault types.json

- **Live vaults:** `.obsidian/types.json` now types `related`, `shared_refs`, `project_refs`, `protected`, `code`, `options`, and `applies_to` as `multitext` (Obsidian's List), keeping `created` as `date` and Obsidian's built-ins: scratch `63bdbfc`, scratch2 `687beab`, shared `2dec8b2`, each `obsidian: pin list property types`. Before, none of these was typed, so Obsidian guessed per note, which is how `protected` became one typed-in item.
- **`/new`** writes `vault/.obsidian/types.json` (`lifecycle.OBSIDIAN_TYPES`: `created` a Date, the seven Lists) after copying the template, inside the same rollback, and the init commit includes it. The printed Obsidian steps no longer say to make `created` a Date. `decisions` and `tasks` stay untyped: they hold mappings, which Obsidian's list editor can't edit.
- **Tests:** 930 by default (+1: the written file is valid JSON with exactly those types, it's in the init commit, and the steps drop the Date step); the init-commit file list test now expects `vault/.obsidian/types.json` too.

### 2026-11-27 — Fix: match Obsidian's types.json formatting

- `types.json` is now written exactly as Obsidian serializes it (`JSON.stringify(data, null, 2)`): 2-space indent, no trailing newline. The three live vaults lost the trailing newline the pinning commits had added (scratch `bb5ad37`, scratch2 `ae33742`, shared `f692542`; each checked to differ by nothing else), and `/new` writes the same bytes (`lifecycle.obsidian_types_json`). The test now compares the written and the committed file byte for byte.

### 2026-11-27 — Commit 11: plans from Claude — schema, submit, revise, list

- **`core/plans.py`:**
  - `validate_plan(session, text)` returns every error at once, each naming its field and task id: `spec` (resolves, unambiguously, in the active vault); `decisions` (`{category, choice, basis}`; basis `new` or `check_basis` OK); `tasks` (unique `t\d+` ids, one-line summary and red test, `files` never under `vault/` or `.git` and never escaping the repo even through a symlink, `done_when` under rule 7 against the checkout's `daedalus.toml`, `max_iterations` 1–10, `budget_usd` above 0 and at most the session cap, `protected` paths that exist); and the body's `## Context and non-obvious decisions`.
  - `submit`, `revise`, `status`, `list_plans`, `view`.
- **The queue, schema v4:** adds a `superseded` status (the table is rebuilt, rows kept). `queue.insert(..., supersede=id)` marks the previous version superseded in the same transaction, only if it's still pending.
- **`apply` refuses plan proposals** with `PlanApprovalNotYet`: "plan approval arrives in commit 12". Nothing is written.
- **daedalus-mcp: thirteen tools.** `submit_plan`, `revise_plan`, `plan_status`, and `list_plans` (`toolset.PLAN_TOOLS`, `MCP_NAMES`), none with `source`. An invalid plan returns `{"status": "invalid", "errors": [...]}`. New `--config` flag for the session cap. Neither DeepSeek profile has the plan tools.
- **REPL:**
  - `/plans` shows pending plans with their version chains, rejected plans not yet revised, and approved plans.
  - `/diff <plan-id>` renders the spec, then the decisions table (NEW ones highlighted, with a count of what approval makes binding), then the tasks, then the body.
- **The plan template:** its "Plan format" now matches (`{category, choice, basis}`, `protected`), and the body heading is `## Context and non-obvious decisions`. The live vaults' `templates/plan.md` copies weren't touched (the live check needed scratch2's vault unchanged).
- **Tests:** 975 by default, 45 new (930 before):
  - plans 33: one per validation rule, combined errors, revisions, rejected revisions, latest-only, an invalid revision keeping v1 pending, apply refused, listing, the v3→v4 migration;
  - daedalus-mcp 6: thirteen tools with no source, no plan tools for DeepSeek, nothing lands, an invalid plan returning every error, a rejection reaching Claude, find_precedent's advice;
  - REPL 2;
  - integrity rows +4 (65): an invalid plan, a basis that isn't precedent, an invalid revision, apply on a plan.
- **Proved red three ways:**
  1. Accepting a basis that cites a proposed note failed `test_basis_citing_a_proposed_note` ("DID NOT RAISE PlanInvalid") and `test_one_call_returns_every_error` ("missing 'decisions[1].basis:'").
  2. Stopping at the first error failed 5, `test_one_call_returns_every_error` among them ("missing 'decisions[1].basis:' in [\"decisions[1].category: 'storage' …\"]").
  3. Writing into `plans/` from `submit` failed `test_nothing_lands_before_approval` ("a plan reached a vault before approval": alpha's and the checkout's digests changed) and `test_apply_refuses_a_plan_and_writes_nothing`.
- **Live check** (scratch2). Before it: the Claude desktop app was restarted (its daedalus-mcp processes dated from Nov 26 and served nine tools), and the spec was committed (`b50b5ae concept: mathx-cli-spec`).
  1. Claude ran `find_precedent` twice ("cli interface argparse", "mathx multiply"; none found), then took three `submit_plan` calls:
     - first: invalid, 1 error, the missing `tasks` key (Claude had sent only the decisions and spec);
     - second: invalid, 1 error: t2 protected `tests/test_multiply.py`, which t1 creates, so it didn't exist yet;
     - third: queued, `mathx-cli` v1, `1m8ey6rp863sk7`. Two tasks (multiply; an argparse `add` CLI) and two `interface` decisions, both `new`, as nothing in the vault covered them.
  2. `/plans` and `/diff` showed it as specified; `/approve` was refused with the commit-12 message; `/reject … make the CLI taks protect a CLI test I'll write`. You then committed `tests/test_cli.py` (`4968e32`).
  3. "Check the plan status and revise it": `plan_status` (one first try was missing its argument, refused by FastMCP), `list_plans`, then `revise_plan` validated first time, giving v2 `1m8eyfq3f6az2t`.
     - It protects `tests/test_cli.py` in both tasks and takes it out of t2's `files`.
     - Its red test is your test, and it moves its own subprocess and bad-input tests to `tests/test_cli_module.py`.
     - It changes `done_when` to run both files. Decisions unchanged.
     - `/plans` showed v2 pending with v1 **rejected** under it, not superseded: v1 was rejected before the revision, and only a pending version is marked superseded.
  4. After it: scratch2's vault was unchanged since `b50b5ae` and `plans/` was empty; the queue held v1 rejected and v2 pending; `git status` was clean in scratch, scratch2, and shared; daedalus-mcp listed 13 tools over stdio.
- **Found in the live check:**
  - `find_precedent` told Claude "ask with ask_user and stop", a tool it doesn't have. Claude submitted anyway and said why. Fixed: daedalus-mcp's note now says to give the decision basis `"new"` in a plan, or ask in chat; the agent's note is unchanged (one test).
  - The live sandbox test `test_real_pytest_in_a_checkout_under_home` now fails (exit 2), because scratch2's checkout holds your red `tests/test_cli.py` (`ImportError: cannot import name 'main'`). It's not a regression: the other 6 tests pass sandboxed, and it goes green when the CLI task lands.

### 2026-11-27 — Commit 11 follow-ups: pinned sandbox checkout, protected paths, client text, v3→v4 migration test

- **A1, the live sandbox test:** `test_real_pytest_in_a_checkout_under_home` now runs in `git worktree add --detach` of scratch2 at `PINNED_SHA` = `b50b5ae` (the parent of `4968e32`, scratch2's last green commit; its `uv.lock` is the one the cached venv `scratch2-490d1d46bfd8` was built from, so no network), under `~/.cache/daedalus/test-checkouts/<random>`, removed with `worktree remove --force` and `prune` in a `finally`. Preconditions: the checkout is under home, and under neither pytest's tmp folder nor `/tmp`. Bites: pointed at `main`, it failed (`exit_code=2 … 1 error during collection`); moved under `/tmp`, the precondition failed (`/private/tmp/daedalus-test-checkouts/… isn't under the home folder`).
- **A2, protected paths an earlier task creates:** a `protected` path that doesn't exist is allowed only if a lower-index task lists it in `files`; the error now says "doesn't exist … and no earlier task lists it in files". `red_test` had no existence check, so it was left alone (`/run`'s preflight will check it). 3 tests; bite (every task's files counted) failed `test_an_earlier_task_may_not_protect_a_file_a_later_task_lists` ("DID NOT RAISE PlanInvalid").
- **A3, clients only hear about tools they have:** `tests/mcp/test_client_text.py` (5 tests). It found three more leaks besides `find_precedent`'s, all fixed: the gate's refusal hint told plan submitters to "use basis 'new' to ask the human with ask_user" (twice); a proposed basis said it becomes precedent "once accepted (by /answer or /promote)", with no mention of Mohamed; and `propose_note` on an existing note said "use propose_edit". New `repl.COMMANDS`. Bite: Claude's note back to "ask with ask_user and stop" failed `test_claude_hears_only_about_its_own_tools` ("claude is told about ask_user, which it doesn't have").
- **A4, the v3→v4 migration:** `tests/core/queue_fixtures.py` builds queue files from frozen v3 and v4 DDL (for commit 12's v5 too): a row in every v3 status with every column filled, a three-path and a one-path change set with BLOBs, plus ledger rows. Opening it keeps every column of every row, `foreign_key_check` is empty, `integrity_check` is ok, and the ledger is untouched; a failure injected after the copy, before the old table is dropped, leaves the file at v3 with every row. Bite: copying one column fewer failed `test_v3_to_v4_keeps_every_row_and_column`.
- **A5, the plan template:** both scratch vaults' `templates/plan.md` were byte-identical to the old package template, so they were replaced: scratch `b28bf95`, scratch2 `3718a70` (`templates: plan.md v2 format`). The shared vault has no `plan.md`.
- **Tests:** 986 by default (+11), 14 live, all passing.

### 2026-11-27 — Commit 12: plan approval, /run, read_runs

- **Approval (`plans.approve_plan`, `prepare_approval`):** refused unless the row is a pending plan and the latest version; re-validated against the state now (every error at once; it stays pending); then, under the write lock throughout, one `apply_changes` commit, `agent(claude): approve plan <slug>-v<N>` with `Daedalus-Change: <the plan's id>`: the plan note (`status: accepted`, each new decision gaining a server-set `note` link, the stems in `related`) and one `decisions/<category>-<choice>.md` per `basis: new`, filled from the vault's own decision template, the stem cut at 48 characters and suffixed `-2`, `-3` against every note in scope. `apply_changes` takes the proposal's id as its change-set id, so the proposal and change set reconcile against one commit; the write lock is now re-entrant in one process.
- **Confirmation:** `/approve` on a plan shows the decisions again and asks `Accept N new decisions as precedent? [y/N]` (or `Approve plan <slug>-v<N>?`); anything but `y` writes nothing. `queue_cli apply` needs `--accept-decisions`. `/end`'s review never approves plans and lists them as "plans waiting — use /approve". The generic `apply` still refuses plans (`PlanApprovalNotYet` is now `PlanApprovalOnly`).
- **`/run` (`terminal/planrun.py`, `core/planruns.py`):** queue schema v5 adds `plan_runs` and `plan_tasks` (tables only, no rebuild). `goals.load` accepts `plan:<id>#<task>` only for an applied plan whose note is accepted. One worktree and branch per plan (`agent/plan-<slug>-v<N>`), tasks in order on it; per task: the daily-cap room check, the preflight (protected paths and the red test's files on the branch; `done_when` under rule 7 with the venv pin; the red test run, "red test already passes" stops it), then the baseline, stored once in `plan_tasks`, then the 10c loop. It stops at the first unfinished task; question, checkin, daily_cap, and interrupted resume with `/run` from the stored baseline; max_iterations, budget, honest_done_refused, and preflight are failures. A running run holds an OS lock; a queue open reconciles one whose lock is free to stopped / interrupted. `/runs`, and `/plans` shows run status.
- **`read_runs` (daedalus-mcp, fourteen tools):** the run, the branch, head, diffstat, and per task its numbers against the stored baseline, its last iteration from its log, and its log link, capped at 12,000 characters (oldest iteration summaries trimmed first). `plan_status` includes the run status. Claude's instructions: after a failed run, `read_runs`, then `revise_plan` if the plan was the problem, and say which.
- **The loop** gained `new_state`, `measure_baseline`, `begin`, and `continue_run`, so a plan task runs in the shared worktree from a stored baseline; its records now keep the missing test IDs and changed protected paths; `/goal resume` never touches a plan task's run.
- **Tests:** 1039 by default, 53 new (986 before): approval 18, `/run` 21, the REPL +5, the v4→v5 migration +2 (from A4's frozen-DDL builder), daedalus-mcp +1, integrity rows +6 (71: approving a superseded, rejected, older, or no-longer-valid plan, a target that appeared, a failing commit). 14 live, all passing.
- **Proved red ten ways** (each break made, its test run, then restored):
  1. Approval skipping the decision notes failed `test_approval_writes_every_new_decision` ("approval didn't write interface-python-m-scratch2-mathx-add-2-3").
  2. Decisions in a second commit failed `test_plan_and_decisions_land_in_one_commit` (`assert '2' == '1'` commits).
  3. A failure writing the second decision note, with the rollback removed, failed `test_a_failed_approval_leaves_nothing_behind` (alpha's and the checkout's digests changed).
  4. No re-validation, after a precedent was set to `status: proposed`, failed `test_approval_revalidates` ("DID NOT RAISE PlanInvalid").
  5. Approval without `y` failed `test_approve_writes_nothing_without_an_explicit_y` (the plan was approved on an empty answer); `queue_cli` without `--accept-decisions` failed `test_queue_cli_needs_accept_decisions_for_a_plan` (`assert 0 == 1`).
  6. Generic `apply()` accepting a plan failed commit 11's `test_apply_refuses_a_plan_and_writes_nothing`; rule 5 still refused the write (`PROPOSE in the active vault is allowed only in: inbox/, questions/, CONTEXT.md`), a second layer.
  7. Resume re-measuring the baseline (an iteration deletes `tests/test_y.py`, the process is killed, `/run` resumes) failed `test_resume_uses_the_stored_baseline` (`('done', None) == ('failed', 'honest_done_refused')`: the lowered bar let it pass).
  8. `/run` going on after a failed task failed `test_stops_at_the_first_unfinished_task` (`('t2', 'done', None) != ('t2', 'pending', None)`).
  9. Skipping the red-test preflight failed `test_a_red_test_that_already_passes_stops_with_preflight` (`('t1', 'done', None) != ('t1', 'failed', 'preflight')`).
  10. Letting a pending plan's provenance launch failed `test_a_pending_plans_provenance_is_refused` ("DID NOT RAISE GoalError").
  Breaks 7–9 first went red only through the scripted fakes ("an unscripted check ran"); the tests now script enough that a break reaches the real assertion.
- **Live check** (scratch2, off-peak; after the desktop app was restarted for `read_runs`):
  1. `/plans` showed `mathx-cli` v2 pending with v1 rejected under it. `/approve 1m8eyfq3f6az2t` showed both NEW decisions with the notes they'd become, asked `Accept 2 new decisions as precedent? [y/N]`, and on `y` made one commit, `571b9b5 agent(claude): approve plan mathx-cli-v2` (trailer `Daedalus-Change: 1m8eyfq3f6az2t`): `plans/mathx-cli-v2.md` (accepted, each decision with its `note`, both stems in `related`) and `decisions/interface-cli-lives-in-src-scratch2-mathx-py-as.md`, `decisions/interface-cli-parses-each-operand-as-int-when-it.md` (accepted, source claude, related the plan and the spec). 3 files, 89 insertions.
  2. Claude asked "Is there precedent for how mathx parses CLI arguments?": `find_precedent` returned both new decisions, and Claude summarized them. Approval made real precedent.
  3. `/run mathx-cli` made plan run `1m8fym1jcsry88` on `agent/plan-mathx-cli-v2`. t1 (multiply): baseline exit 4, collected 0; one iteration (red by ImportError, a deliberate wrong multiply, then green); check exit 0, collected 3, every guard held; $0.0021; `b2fe1a9`. t2 (the CLI) started from t1's commit, cited both new decisions, broke `main` on purpose (red), then went green; check exit 0, collected 3; $0.0021; `0773f9a`. Run done, no stops, about 40 s, $0.0042 in all. No check-ins or stops were exercised (one iteration each), so the next live check should force one.
  4. "How did the mathx-cli run go?": Claude called `read_runs` and reported both tasks done in one iteration, the guards, the cost, the branch and its 73-line diffstat, and noted (correctly) that each check counted its own files.
  5. `/wt diff`, then `/wt merge plan-mathx-cli-v2` made `baa9049` (`--no-ff`). On main, scratch2's full suite, sandboxed: 12 passed, including `tests/test_cli.py`. The live sandbox test stays pinned, so it doesn't depend on this.
- **Found in the live check:**
  - Both tasks' baselines were exit 4 with 0 collected, because `done_when` names a test file the task creates, and pytest runs nothing when a named path is missing. The count and node-ID guards therefore constrained nothing; only exit 0 and the protected paths did (t2's real bar was Mohamed's protected `tests/test_cli.py`). Up Next.
  - The pytest command meant for a shell was typed at the REPL prompt, so it went to DeepSeek ($0.0012, no harm); the step didn't say "in another shell".
  - `/approve`'s table cut the stems with "…" at an 80-column terminal; the approval line printed them whole.

### 2026-11-28 — Commit 12b: honest baselines

- **Why:** commit 12's live check ran both tasks against a `done_when` baseline of pytest exit 4 with 0 collected (each named a file the task creates), so the count and node-ID guards checked nothing, and the red-test preflight held because a missing file doesn't pass.
- **A1, the whole suite:** every baseline (plan tasks and `/goal` runs; it's in the shared loop) stores the whole project's `pytest --collect-only -q` IDs and collection-error files; at done they're listed again and every one must still be there (the `suite` guard).
- **A2, empty baselines:** paths `done_when` names that don't exist yet are left out; if nothing is left, or pytest runs nothing (exit 5, or exit 4 "not found"), the baseline is `{"empty": true, "reason": …}`, shown as such in `/runs`, `read_runs`, the report, and the agent's message. Any other exit 4 is a usage error and refused.
- **A3, red for the right reason** (`goals.classify_run`): 0 stops (already passes); 1, 2, and an exit 4 that reports a collection error are red; 4 "not found" and 5 are absent, allowed only for a file this task or an earlier one lists (`created_by_task`).
- **A4, the counterfactual:** when the red test was absent or changed, a temporary detached worktree at the task's start commit gets only the final red-test files (and changed `conftest.py`); the red test must be red there, or the iteration isn't done and the log says "your red test passes against the code before your change".
- **A5, the final gate:** after the last task, `pytest -q` on the plan branch; a failure stops the run with `final_suite` (not resumable), its output kept for `read_runs`. Queue schema v6 rebuilds `plan_runs` for it (rows kept; tested from frozen v5 DDL, with a mid-rebuild failure).
- **Config check-ins before done:** found while preparing the live check. `go()` declared done before the config check-in, so an iteration that edited `pyproject.toml` and passed finished without one, against the documented rule. Now the check-in comes first; resuming a done iteration held at a check-in, with nothing changed, finishes it as done.
- **B:** `/approve` prints each note it will write whole, on its own line (never in a table cell; tested at 80 columns); stems drop trailing stop words (the two live choices now give `interface-cli-lives-in-src-scratch2-mathx-py` and `interface-cli-parses-each-operand-as-int`; existing notes keep their names); decisions take an optional `consequences`, and a decision note's Consequences is never blank (`Not stated in [[<slug>-v<N>]].`).
- **Found while building it:** a red test named by node ID in a module that can't import exits **4**, not 2 (pytest reports "found no collectors" after the collection error); that's the usual red-test-first failure, and it's red. Checked with real sandboxed runs in scratch2; the classifier is tested on those outputs.
- **Tests:** 1060 by default, 21 new (1039 before): baselines 19 (A1 ×2, A2 ×3, A3 ×3, A4 ×4, A5, check-ins ×2, B1, B2, B3, the classifier), the v5→v6 migration 2; the commit 12 resume test now also proves the stored baseline, suite IDs included, isn't re-collected. The scripted collector now honors the paths it's given. 14 live, all passing.
- **Proved red seven ways** (each made, its test run, then restored):
  1. No end-of-task suite collection: `test_a_task_cant_delete_tests_outside_done_when`, `('done', None) == ('failed', 'honest_done_refused')`.
  2. Exit 4 stored as a normal baseline: `test_an_empty_baseline_is_recorded_as_empty`, `(False, None, …) == (True, 'all named paths are created by the task', None, None)`.
  3. Exit 4 treated as red for a test no task creates: `test_an_absent_red_test_no_task_creates_is_a_preflight_stop`, `('done', None) == ('failed', 'preflight')`.
  4. No counterfactual: `test_the_red_test_must_fail_without_the_change` (done in one iteration with `assert True`: `not enough values to unpack (expected 2, got 1)`).
  5. The counterfactual copying the whole final tree: `test_the_counterfactual_runs_against_the_code_before_the_change`, "your red test passes against the code before your change … with only src/x.py, tests/test_new.py copied in: exit 0".
  6. No final gate: `test_final_suite_gates_plan_done`, `('done', None) == ('failed', 'final_suite')`.
  7. Note names back in the table: `test_approve_never_cuts_a_file_name_at_80_columns`, "the note's name was cut before the question".
- **Live check** (scratch2, off-peak):
  1. `/wt drop plan-mathx-cli-v2` (merged, so no `--force`). Claude, asked for a one-task plan setting pytest's `testpaths` to `tests`, ran `get_scope`, `list_notes`, `find_precedent` (none), then `submit_plan`: `pytest-testpaths` v1, `1m8g3ey8rbhzxp`, valid first time, no decisions. Approved as `f600f30 agent(claude): approve plan pytest-testpaths-v1` (the plan note only).
  2. The first `/run` failed preflight with "the red test's tool.py doesn't exist": the red-test pattern read `tool.py` out of `[tool.pytest.ini_options]`. Fixed (`143e4d5`, `.py` must end the name, a test on the real text); with Mohamed's OK the failed run's rows were deleted (queue backed up first) and its empty worktree dropped. Nothing had run.
  3. `/run pytest-testpaths` (run `1m8g3vyrb23vgm`). Stored baseline: "done_when baseline: empty (all named paths are created by the task) (not yet there, so left out: tests/test_pyproject_config.py); whole suite at the start: 12 tests; red test: created_by_task". Iteration 1: the agent found `testpaths = ["tests"]` already in `pyproject.toml`, wrote `tests/test_pyproject_config.py`, and edited `pyproject.toml` twice to no net change. The check passed and every guard held except ★ **the counterfactual**: "your red test passes against the code before your change (`pytest -q tests/test_pyproject_config.py::test_pytest_testpaths_is_tests` at f600f30 with only tests/test_pyproject_config.py copied in: exit 0), so it proves nothing". The config edit checked in (the first live check-in); Mohamed stopped it: `stopped (checkin)`, text "Mohamed stopped it at: the check-in after iteration 1 (config changed: pyproject.toml): …".
  4. `/run` again resumed the same branch with the same stored baseline (both `/runs` print the identical baseline line). As a stop on an iteration that wasn't done, it began a new batch: iteration 2 read the plan, saw the premise was false, set `testpaths = ["test"]` to show the test goes red for the right reason, restored it, and declined to fake a red. The counterfactual failed again, the check-in came again, and Mohamed stopped it again. t1: 2 iterations, $0.0085, ↑ 189,625 ↓ 11,155.
  5. Claude, from `read_runs`: the run stopped without delivering what the plan wanted, "and the cause was a wrong assumption in my plan": `testpaths` was already set at the base commit and it never read the file before submitting. It reported the check-in stop, that the counterfactual refused the red test, that every original test still exists and no protected path changed, and offered to accept t1 as already satisfied or re-scope it.
  6. Mohamed reviewed `/wt diff` and merged it anyway, keeping the test as a regression guard: `b964074` (`--no-ff`). On main, scratch2's suite, sandboxed: 13 passed. The plan run stays `stopped (checkin)`, and the final gate never ran.
- **Found in the live check:**
  - ★ A4 did its job live: this plan's red test could never honestly be red (the setting predates the task, since `97b17c8`), and no earlier check could see that, because the test file was the task's to create. Before 12b this task would have been "done" on iteration 1.
  - The check-in said "config changed: pyproject.toml" over an empty diff (edited and restored within the iteration), and `/runs` printed "collected 1 (start None)" for an empty baseline, with the log saying "(at the start: no pytest summary line)". Fixed: "config edited and restored, no net change", a note in the check-in that the iteration's own runs used the edited version, and "start empty" (one test, red first).
  - A task stopped at a check-in, not done, resumes as a new batch: the task header said "1/3 iterations" while the loop said "iteration 2 of 4".
  - A plan run whose branch Mohamed merged stays `stopped`; `/run` would resume it.
  - Claude planned without reading the file the task changes.
  - `uv run pytest -q` run directly in scratch2 left `__pycache__/` untracked there: its `.gitignore` doesn't ignore it.
- **Fix, found starting the live check:** `/help` printed through rich markup, so `[dir]`, `[text]`, and `[plan]` vanished from its lines (`/promote`'s and `/goal resume`'s since they were added); it's printed as plain text now, with a test that failed before. `/run` on a finished plan whose worktree is gone no longer tells you to diff and merge it.

### 2026-11-28 — Commit 12c: run integrity across resumes, merges, and already-true goals

- **A, cumulative limits:** a plan task's `max_iterations` and `budget_usd` are totals over every batch. A resume continues the numbering, pins the loop's limits to the totals, and fails at once (launching nothing) when none are left; the counts are stored in `plan_tasks` after every iteration (an interrupted one included) by a new loop hook, so a crash refunds nothing. The budget pre-check takes the larger of the stored cost and the ledger. Header and loop show the same numbers.
- **B, merges:** `/wt merge` on an unfinished plan run shows its tasks and the final gate, and asks `Merge an unfinished plan run? [y/N]`; every merge of a plan branch records `merged_sha` and closes the run, and `/run` refuses a closed one. Startup reconciliation closes any run whose branch head is in HEAD, with the merge that took it there (a dropped branch is found through its tasks' last commit).
- **C, `satisfied_at_base`:** the coder profile's `report_already_satisfied(evidence)` (fifteen tools), only in a plan task's run; it ends the turn, and the loop verifies the four checks itself (the red test exists; it passes at the start commit with only the red-test files copied in; the net diff touches only those files and `conftest.py`; every other guard holds), then asks `[a]ccept as satisfied / [s]top`. Accepted counts as finished for the final gate and for merging. `submit_plan` now tells Claude to read every existing file a task's `files` names.
- **D, structured `red_test`:** a non-empty list of `path.py` / `path.py::node`, validated (relative, in the repo after realpath, `.py`, no `..` or `.git`, existing or created by this or an earlier task); prose goes in `red_test_note`, which nothing parses. The regex is gone. `/run` refuses an approved plan with a text `red_test`. **No pending plan in the live queue used the old format** (none was pending); the two approved ones keep it as history.
- **E1:** the whole-suite listing at done now also refuses a collection error in a file that collected cleanly at the start. "Couldn't run at done" was already refused in 12b; both have breaks. **E2:** `read_runs` and `/runs` show iterations and cost against the limits, resumes and each run's stop, `satisfied_at_base`, and `merged_sha`. **E3:** `/new` adds `__pycache__/` and `.pytest_cache/` to the repo's `.gitignore` in the init commit; scratch `580bdfc` and scratch2 `bc12b95` (`gitignore: test caches`), and scratch2's two stray `__pycache__/` folders were deleted.
- **Queue schema v7:** both plan-run tables rebuilt, rows kept (tested from frozen v6 DDL, with a mid-rebuild failure). The live v6 queue was backed up first.
- **Tests:** 1091 by default, 29 new (1062 before): run integrity 16 (limits across resumes ×3, merges ×4, already-satisfied ×6 with the REPL check-in, collection at done ×2, a claim outside a plan), structured red tests 8 in validation and 2 in `/run` (notes never become paths, an old-format plan refused), `/new`'s `.gitignore` 4, the v6→v7 migration 2; minus the dotted-name regex test and two preflight cases that validation now catches. 14 live, all passing.
- **Proved red nine ways** (each made, its test run, then restored):
  1. A resume granting a fresh iteration count: `test_iterations_are_counted_across_resumes`, `('failed', 'max_iterations', 3) == ('failed', 'max_iterations', 2)`.
  2. A resume resetting the spend: `test_budget_is_counted_across_resumes`, `('failed', 'max_iterations') == ('failed', 'budget')`.
  3. A crash refunding the interrupted iteration: `test_a_crash_refunds_nothing`, "the killed iteration was counted, so nothing more was launched" (`4 == 3` model calls).
  4. `/wt merge` not asking: `test_merging_an_unfinished_plan_run_asks_first` (it merged at once; the run summary never printed).
  5. No reconciliation: `test_a_merged_plan_branch_closes_its_run` (no "closed plan run …" at startup; /run didn't refuse).
  6. The claim skipping step 2, a goal not true at base: `test_an_unverified_satisfied_claim_is_refused`, `assert (True is False)` (the claim verified).
  7. The claim skipping step 3, with `src/` changed: `test_a_satisfied_claim_with_code_changes_is_refused` (a satisfied check-in was asked).
  8. Regex extraction back for `red_test_note`: `test_notes_never_become_paths`, "the red test's tool.py, tests/test_other.py doesn't exist …".
  9. A collection error at done let through: `test_a_new_collection_error_at_done_is_refused`, `assert not True` (done). The 12b behavior, couldn't-run refused, broke the same way: `test_a_collection_that_cant_run_at_done_is_refused`.
  Breaks 2, 4, and 7 first went red only through the scripted fakes; the tests now script enough to reach the real assertion.
- **Live check (Part G), 2026-11-28, scratch2:**
  1. Startup closed both old runs: "closed plan run 1m8fym1jcsry88 (mathx-cli v2): its branch agent/plan-mathx-cli-v2 was merged at baa9049" (its branch was dropped, so it was found through its tasks' last commit) and "closed plan run 1m8g3vyrb23vgm (pytest-testpaths v1): … merged at b964074". `/runs pytest-testpaths` showed "2 of 3 iterations · $0.0085 of $1.00" and "closed: branch merged at b964074".
  2. Asked for a plan that guarantees `subtract` handles negative operands. Claude read the code first. It found that a function-level test would pass at base ("the same dead-red situation as the last plan") and planned the real gap instead: a `subtract` CLI subcommand. It needed no "submit anyway". Plan `subtract-negative-operands` v1 (`1m8h74qnr0c2kb`) had a structured red test, `tests/test_cli.py::test_subtract_accepts_negative_operands`, plus a `red_test_note`, and one new decision with consequences. It was approved in `6b17f06`, and the decision note is `decisions/interface-add-a-subtract-subcommand-to-the-mathx.md`.
  3. `/run` made run `1m8h769hc0qhw9`. Baseline: `done_when` collected 1, exit 0, and the whole suite at the start had 13 tests. Over one iteration the agent wrote the test, saw it fail with "invalid choice: 'subtract'" (SystemExit 2), then added the subparser and a dispatch table. The check exited 0 with 2 collected, and every guard held, including the counterfactual ("at 6b17f06 with only tests/test_cli.py copied in: exit 1"). Committed `3cbf6fe`, $0.0018. The final gate passed, and in the queue the task's batches show 1 iteration with stop `done`.
  4. `/wt merge` didn't ask (the run was done). It merged as `1d4b6bd`, closed the run (`merged_sha` 1d4b6bd in the queue), and said `/run` now refuses. In scratch2: 14 passed, and `git status` was clean.
  - ★ **Not exercised live: `report_already_satisfied`.** Claude's planning avoided the dead red, so the agent never needed the claim, and no satisfied check-in came. That is the outcome the planning rule is meant to produce, but the claim path is still proven only by tests.
  - **Found in the live check:** `/plans` listed the closed pytest-testpaths run as "run stopped (checkin) · t1 stopped", with nothing saying it was closed. It now adds "· closed: merged at <sha7>". `test_a_merged_plan_branch_closes_its_run` checks this and failed before the fix.
  - Not in the paste: `/wt drop plan-pytest-testpaths-v1`, `/run pytest-testpaths`'s refusal, and Claude's answer to "How did that run go?". Both plan worktrees still exist.

## Key decisions

- ★ **No unapproved plan touches the vault.** Plans are validated in full and held in the queue (Op.WRITE_PLAN into `plans/`); the generic `apply` refuses them, so approval (`approve_plan`) always writes the plan and its new decisions together.
- ★ **Approval makes precedent, so it's explicit:** Mohamed sees the decisions again and types `y`; non-interactive paths need `--accept-decisions`; `/end` never approves a plan. The plan and every new decision land in one commit, re-validated at approval, or nothing lands.
- **New decisions look hand-written:** filled from the vault's own `templates/decision.md`, `category` as an extra frontmatter key (unknown keys are allowed, so no schema change), stems unique across everything the link resolver sees.
- ★ **Plan run state lives in the queue, never the vault;** the plan note isn't edited after approval.
- ★ **One branch per plan, tasks stacked in order; only Mohamed merges.**
- ★ **Each task's baseline is measured once, before its first iteration, and stored; a resume never re-measures it.**
- ★ **A run stops at the first unfinished task.** question, checkin, daily_cap, and interrupted resume with `/run`; max_iterations, budget, honest_done_refused, and preflight are failures that Claude fixes with `revise_plan` after `read_runs`.
- **A task whose red test already passes doesn't start:** it has no honest done-condition.
- ★ **A baseline always measures something:** the whole suite's test IDs at every task start, and a `done_when` run that measured nothing is recorded as empty, never as zero tests.
- ★ **A red test must be red for the right reason, and must fail without the change** (the counterfactual against the task's start commit with only the red-test files copied in).
- ★ **A plan is done only when the whole suite passes on its branch.**
- **A config change checks in before done is declared.**
- ★ **A plan task's limits are totals across every resume,** stored after every iteration; a crash refunds nothing.
- ★ **Merging a plan branch closes its run;** an unfinished one merges only on an explicit y, and any merge (by hand too) is found at startup.
- ★ **"The goal already holds" is verified, never trusted:** the red test must pass at the start commit with only the test copied in, and the task may have changed nothing else.
- ★ **`red_test` is structured; nothing parses paths out of prose.**
- **A running plan run holds an OS lock for its life;** a free lock on a `running` row means its process is gone, and a queue open reconciles it to interrupted.
- **Text that reaches Claude names no slash command** except in a sentence addressed to Mohamed; stored stop texts name none, and the terminal adds the command to type.
- ★ **`validate_plan` returns every error at once,** each naming its field and task id, so one round trip tells Claude everything to fix.
- ★ **A plan decision's basis is `new` or OK precedent, nothing else.** Proposed, draft, pulled, superseded, ambiguous, or missing bases refuse the whole plan.
- ★ **`source` (and type, status, created, supersedes) is set by the server, never by Claude;** passing one is an error in the list.
- ★ **Plans are Claude's job:** the four plan tools are only in daedalus-mcp (thirteen tools); neither DeepSeek profile has them.
- ★ **Every version is kept.** A revision is `<slug>-v<N+1>` with `supersedes: "[[<slug>-v<N>]]"`; a still-pending v<N> is marked superseded in the same transaction, and a rejected one stays rejected.
- **`done_when` in a plan is checked against rule 7's allow list from the checkout's `daedalus.toml`, not the venv pin,** since pinning would mean building a venv (network) from a validation call.
- **A plan's protected path must already exist, or be in an earlier task's `files`,** so a test the human will write has to be committed first, but a later task can protect what an earlier task creates. `/run` re-checks at each task's start.
- ★ **Clients only hear about tools they have:** text shared between DeepSeek and daedalus-mcp is worded for any client, and a test fails if a client is told about a tool it lacks, or Claude about a slash command outside a sentence addressed to Mohamed.
- **Migration tests start from frozen old DDL** (`tests/core/queue_fixtures.py`), never from the current code, so a change to `queue.py` can't change the file a migration starts from.
- **The under-home live sandbox test runs in a detached worktree of scratch2 at a pinned green commit,** never scratch2's main, so what's on Mohamed's branch can't make a sandbox test pass or fail.

- **Obsidian's property types are pinned in each vault's `.obsidian/types.json`** (the seven string-list keys as List, `created` as Date), and `/new` writes them, since the templates hold no `.obsidian/`.
- ★ **List-valued keys must be YAML lists,** never strings or a list typed into one item; the error points at Obsidian's List property type.
- ★ **Every test that existed at the start must still exist to be done** (node IDs from `--collect-only`; files when the start had collection errors), on top of the count guards.
- **Changes to existing tests are always shown, never blocked:** in every check-in and the report, with the diff.
- ★ **Only you launch goals:** `source: human`, or `status: accepted` after `/promote`. An agent can't start a run from a goal it wrote.
- ★ **`done_when` passes rule 7 before anything is created;** a goal whose check can't run leaves nothing behind.
- ★ **The honest-done rule:** exit 0, collected not below the baseline, skipped and xfailed not above it, and no protected path changed. A pass with a failed guard is not done, and says why in words.
- ★ **Fresh context per iteration:** a new conversation holding only the goal, the logs, the diff stat, the last check's tail, and any redirect or answer. No tool history or reasoning carries over.
- ★ **Every ledger row in a run carries its run_id; the goal budget, session cap, and daily cap are all checked before each call.**
- ★ **Config changes are allowed but check in at once,** with their diff, so you see every change to how tests are found or run while it happens.
- ★ **Protected paths are refused by the edit tools (rule 6) and checked again at every check;** one that doesn't exist refuses the launch.
- ★ **Merging stays your command;** the report only suggests it.
- **pytest's "collected" is read from its summary line** (passed + failed + errors + skipped + xfailed + xpassed); a module that fails to import counts as one.
- **Lessons only for a loop check that failed and a later one that passed;** otherwise no lessons call is made. Approval in `/review lessons` writes the draft to the shared inbox and prints the `/promote`; it never promotes.
- **Resuming a stopped, not-done run grants another `max_iterations` and `budget_usd`;** log numbers continue across runs of a goal.
- ★ **Agents write code only in worktrees, and only you merge.** Merging is a REPL command, never a tool; your checkout is never written by an agent.
- ★ **Worktrees live in `~/.cache/daedalus/worktrees/<project>/`,** not beside the repo, so `~/code` stays clean; each on `agent/<slug>`.
- ★ **`git add -A` is safe in a worktree** (only the edit tools write there; sandboxed runs can't; `vault/` and `.git` are unwritable), and `commit` still aborts if a staged path is under `vault/`.
- ★ **Drop protection:** unmerged commits or uncommitted changes need `--force`.
- ★ **`edit_code` is the default edit, and `old` must occur exactly once.**
- ★ **Reads follow the worktree,** so the agent sees its own edits.
- **Merge refuses a dirty checkout, untracked files included, and any branch that touches `vault/`;** a conflict is aborted and verified clean.
- **The default commit message comes from a tool-less one-shot call,** outside the conversation, so it can't act or change history; the edit log's paths are the fallback.
- **`/end` in coder mode writes its log entry with a chat-profile agent** (coder mode has no `propose_edit`), from a summary of the edits, runs, and commit.
- ★ **Every agent-triggered command runs sandboxed, or not at all.** `sandbox-exec` missing, a failed preflight, or a profile that doesn't load is a refusal; there's no unsandboxed fallback.
- ★ **The profile starts from allow-default and denies the four things that matter** (network with DNS, writes, reads under home, the Keychain). Easier to get right than deny-default, and still strong because those denies are the protection.
- ★ **Every path in the profile is a realpath `-D` parameter.** `/tmp` and `/var` are symlinks into `/private`, and Seatbelt matches real paths.
- ★ **The worktree and the venv are read-only to the run;** only the run's own `tmp/` is writable.
- ★ **The child's environment is built from scratch** (six keys). Nothing is inherited.
- ★ **Rule 7 pins the program to `<venv>/bin/<name>`;** the host's `PATH` never matters.
- ★ **The venv lives outside the worktree, keyed by `uv.lock`'s hash.** Dependency install is unsandboxed, and trusted only because the lockfile is approved (the gate's `dependency` category) and frozen.
- ★ **No memory cap is claimed;** macOS doesn't enforce one reliably. The timeout and the CPU limit are the protection.
- ★ **`daedalus.toml` and `uv.lock` are read from your checkout, never the worktree,** so an agent's edits can't widen what it may run or what gets installed.
- ★ **Metadata only where pytest needs it:** the worktree's ancestors up to home and pytest's config filenames in them, by literal path, plus the worktree's `vault/` and `.git`. Elsewhere under home even `stat` is denied.
- **Both Keychain layers stay** (the mach denial and the home read denial), since each blocks on its own.
- **Every refusal comes before any spawn it can stop,** and the whole group is SIGKILLed after every run, so nothing outlives it.
- ★ **Only the converted markdown enters git, never the original.** The SHA-256 and byte count identify it without storing it, and a test checks `git log --all` for any non-markdown path in `sources/`.
- ★ **No converter sees an unvalidated path.** Realpath first; vaults, `.git`, the state folder, non-regular files, URLs, oversized files, and other extensions are refused before MarkItDown is built. MarkItDown runs with built-in local converters only: no plugins, no `llm_client`, no Document Intelligence, no URLs.
- ★ **An empty conversion is a refusal, not a note.** A scanned PDF has no text layer, and an empty note would look like success. Markdown over 2 MB is refused, never truncated.
- ★ **The same bytes are added once.** The duplicate check is by hash, so a renamed copy is refused too, naming the existing note. The file is hashed again after conversion, so a file changed meanwhile is refused.
- ★ **`/add` writes through `apply_changes`, not a queue proposal.** It's a human action (`USER_ACTION`), and proposal content is capped at 256 KB, under the 2 MB markdown cap. It keeps every apply guarantee: the write lock, a stale check, a recorded change set, a path-limited commit, rollback, and reconciliation.
- ★ **Conversion is proven network-free by test, not by reading MarkItDown's code.** Connects and DNS lookups are blocked and recorded while every format converts with a freshly built converter.
- **`(cid:NNN)` is stripped from PDF text only.** Elsewhere it's the author's own text.
- **Source stems are unique across every registered vault,** so a bare `[[stem]]` to a source can't be ambiguous (the commit-8 fix's link rule would refuse it).
- **Source notes:** the title is the first markdown heading, else the file stem (`result.title` is ignored; it's None). .md and .txt are copied byte-for-byte after a UTF-8 check, with converter `copy`, and a .md file's own frontmatter is kept as body.
- ★ **No design choice goes ahead without precedent or the human's answer.** Precedent is an accepted, schema-valid note in the active `decisions/` or the shared `practices/`, and nothing else: no drafts or proposed notes, no other folders, no pulled projects' decisions (promote those to a shared practice).
- ★ **The four design categories are single-sourced** in `DESIGN_CATEGORIES`, and every `AGENTS.md`, template and live, is tested against them.
- ★ **`ask_user` ends the agent's turn.** No model call follows it that turn. Other calls in the same reply get a "Not run" tool result, so the history stays a valid tool exchange. A refused `ask_user` doesn't end the turn.
- ★ **`/answer` and `/promote` are one commit each,** through `apply_changes`. The question approval, if still pending, is its own commit first.
- ★ **Multi-path apply has single-path apply's guarantees:**
  - a stale check on every path;
  - a recorded change set with prior content before the first write;
  - path-limited `git commit --only`;
  - every path restored on failure or crash;
  - the write lock throughout.
- **Promotion keeps `source`:** acceptance is the status, and provenance stays honest. The inbox deletion is authorized as `PROPOSE` (inbox is where drafts live), the target as `USER_ACTION`.
- **`find_precedent` matches any word of the topic** (FTS `OR`). The set is small and already filtered to accepted notes, so recall matters more than precision.
- **`ask_user`:**
  - It records its own "why no precedent applied" from a `find_precedent` run, rather than taking a fifth argument.
  - It needs at least two distinct options.
  - The question note's status is `proposed`, and `Category: \`x\`` goes in the body, not the frontmatter.
- ★ **Questions are `q-<slug>`, decisions `<slug>`;** a bare link always means the decision. The link rule refuses ambiguous links in typed drafts, while dangling ones stay allowed.
- **`/answer` decision notes:**
  - They take the question's stem without `q-`, with `-2` and so on if taken.
  - Their links are path-qualified (`[[questions/q-x]]` and `[[decisions/x]]`).
  - Free text rejects every option.
  - The answer is validated before the question is approved.
- ★ **Obsidian is asked where, never what.** The CLI reports the focused vault folder and file. Content is read only through `daedalus.core`'s `read_note`, so the policy (`.obsidian/` refused by rule 4, escapes by rule 1) and schema reporting apply. The CLI's `read` and its write commands are never used.
- ★ **Never target an Obsidian vault by name.** Every project vault is a folder named `vault`. `/open` uses `obsidian://open?path=`, and `/here` maps `base` by real path.
- ★ **`/here` never launches Obsidian.** `pgrep -x Obsidian` comes first, and the CLI runs only if the app is up. `/open` may launch it.
- ★ **The CLI's output is untrusted.** Not JSON, a `base` that isn't a directory, a null `file`, and a missing file are four separate errors, never a guess. The parser requires the `=> ` line because the CLI exits 0 on errors.
- ★ **`/here`'s note goes in the next user message, never the system prompt,** so the cached prefix stays byte-identical. It goes with one message, and a second `/here` replaces it.
- **`/here` edge cases:**
  - A failed `/here` attaches nothing and drops an earlier attachment.
  - A failed turn keeps the attachment for the retry.
  - `/end` never carries it.
  - `/vault`, or a `/drop` of its vault, clears it.
  - Only `.md` files are attached.
  - The REPL reloads the registry for `/here`, so a vault registered since start is recognized rather than called unregistered.
- ★ **One schema, in `daedalus.core.schema`.** `SCHEMA` moved out of `tests/test_templates.py`, which now imports it, so one dict guards both the templates and live notes.
- ★ **`.obsidian/` in any parent of a project vault fails registry load.** This is checked up to and including the repo root, the nearest parent holding `.git`, and catches the commit-1 mistake of opening the repo as a vault. Registering a folder with no `AGENTS.md` fails too, which catches registering `~/code/daedalus-scratch` instead of `.../vault`.
- ★ **The session is passed in explicitly.** Core functions never read `state.json`, so every one is testable without `~/.config`. Since commit 4, `daedalus.core.state` loads it for front ends (daedalus-mcp now, the REPL from commit 6).
- ★ **Deny by default.** Each area has an explicit allow table per op, and anything not listed is refused. A consequence: deleting an area's branch makes the policy stricter, not looser.
- ★ **Rule 4 compares case-insensitively.** macOS file systems ignore case, so `.OBSIDIAN/app.json` *is* `.obsidian/app.json`. Allow checks stay exact-case: a case variant of an allowed name is refused, which errs safe.
- ★ **Typed folders are validated against SCHEMA,** and link-list keys must hold only strings. That catches `related: [[a]]`, which parses as `[['a']]`.
- ★ **The search index lives in `~/.cache/daedalus/index/<sha1>.sqlite`, never in a vault.** Indexing is system state, not an agent write. The shared vault stays untouched and `git status` stays clean.
- ★ **The queue lives at `~/.local/state/daedalus/queue.sqlite`, outside every vault.** The terminal and `daedalus-mcp` write it concurrently, so it uses WAL mode and one short transaction per operation. Tests redirect `queue.STATE_ROOT` the way they redirect the index cache.
- ★ **Drafts are validated at propose time.** A draft going into a typed folder is checked against that folder's type. Elsewhere it is checked only if its frontmatter declares a type. Every error goes back to the agent at once, while it can still fix the draft.
- ★ **Apply re-authorizes against the session passed in now.** It never trusts the propose-time check. A vault that has since become pulled or unregistered is refused with that rule's number.
- ★ **Stale check before every write.** An edit whose target no longer hashes to `base_sha256`, or a create whose target now exists, is marked `stale` and the file is left alone. This stops an agent overwriting an edit you made in Obsidian.
- ★ **A write that isn't committed shouldn't exist.** If the commit fails, the file is restored (or removed, for a create) and the proposal is marked `failed` with git's output.
- **Source is provenance.** An agent can't file its work as a human's on create, or re-attribute a note on edit. A human proposal may change `source`.
- **Crash recovery by reconciliation, not a journal.** apply marks the record `applying` before the replace; the old content is already stored. Reconciliation runs on every queue open, but only for records whose apply isn't running: taking the write lock without waiting is the test. A commit carrying the trailer means the record was applied; anything else is rolled back. A file changed after the crash is never overwritten.
- **The write lock is an OS lock** (`flock`), so a crashed holder can't leave it stuck.
- ★ **daedalus-mcp exposes exactly eight tools.** `get_scope`, `list_notes`, `read_note`, `search`, `backlinks`, `list_files`, `read_code`, `propose_note`. Claude can read and propose, never approve, apply, reject, edit, or log. A test pins the set.
- ★ **The server sets `source="claude"`.** `propose_note` has no `source` parameter and its schema refuses extra arguments. With commit 3's source rule, a draft saying `source: human` is refused, so Claude can't present its work as yours.
- ★ **The session is loaded on every tool call, never cached.** Switching projects takes effect on Claude's next call without restarting the desktop app. Reading two small files per call costs microseconds.
- ★ **Nothing goes to stdout except protocol messages.** Logs go to stderr and `mcp.log`; the FastMCP banner is off. A subprocess test parses every stdout line, so a stray `print()` fails the suite. There's deliberately no stdout redirect hiding mistakes: the test is the guard.
- ★ **Read caps protect Pro usage, not speed.** `read_code` 2,000 lines (and 200,000 characters) per call, `list_files` 2,000 paths. One careless "read the whole repo" shouldn't burn a five-hour window.
- **Refusals are tool errors.** `PolicyError` reads "Refused by rule N: <reason>"; schema, state, registry, ref, and read errors pass their messages through; anything unexpected is logged with its traceback and returned as an internal error. The server keeps running. A refusal is logged once at INFO, never at ERROR: ERROR in `mcp.log` means a real fault.
- **`read_note` over MCP returns invalid notes with their schema errors** instead of refusing them, so Claude can see and explain a broken note. Schema errors on drafts still refuse the proposal.
- **Code tools read only the active repo area.** A path that resolves into any vault is refused with a pointer to the note tools; `list_files` lists only what `read_code` would let you read.
- ★ **The DeepSeek key lives in the macOS Keychain, never in a file.** `DEEPSEEK_API_KEY` overrides it for tests and CI. No key means no client and no request.
- ★ **Prices in one table with a `checked` date; all time math in UTC.** Peak is 01:00–04:00 and 06:00–10:00 UTC on weekdays, half-open, at double the off-peak rate. A call is priced at its start time. The daily cap resets at 00:00 UTC.
- ★ **Chinese public holidays can't be known here, so weekday peak windows are always peak.** DeepSeek bills holidays off-peak; this overestimates then, the safe direction for a cap.
- ★ **The ledger is the single source of truth for caps.** The cap check and a worst-case reservation row are one SQLite transaction, so two terminals can't both spend the same headroom (a 20-process race test lets exactly 15 × $0.20 through a $3.00 day). The reservation settles to billed usage, to an estimate if interrupted, or is deleted if DeepSeek never answered with a stream. A killed process leaves the worst case counted.
- ★ **Worst case before every call:** all input a cache miss, all of `max_tokens` used, at the current rate. Refused before any network I/O, naming the cap.
- ★ **Interrupted streams are recorded as estimated** and count toward the caps: a stopped stream may have been billed.
- ★ **`reasoning_content` is passed back,** because DeepSeek returns 400 without it on tools requests. It bills as input on every later call, so `reasoning_effort` defaults to `low` and is sent on every request, along with `max_tokens` (8,000).
- ★ **The system prompt is byte-identical across calls:** a fixed preamble, the shared `AGENTS.md`, then the project's. No timestamps, no set iteration. The live check showed the shared prefix caching from the second call on.
- **The agent tools are daedalus-mcp's tools** (`daedalus.toolset`), with `source="deepseek"` set in code. The schemas refuse extra arguments, so a `source` the model invents is refused. Results are capped at 60,000 characters, with at most 12 tool rounds per turn. Refusals go back to the model; `read_note` schema errors are also shown to the user.
- **A failed turn is rolled back out of history** (a cap, an API error, Ctrl-C), so the next turn never sends a half-finished tool exchange.
- **Unknown keys in `config.toml` are errors,** so a misspelled cap can't silently fall back to the default.
- **The link rule covers every typed draft, not only typed folders.** That includes an `inbox/` draft that declares a type, because that's where agents propose, and it's how `[[CONTEXT]]` got in. An untyped jotting in `inbox/` has no link rule. A root file is caught by name as well as by resolution, so `[[CONTEXT]]` is refused even in the shared vault, which has no `CONTEXT.md`.
- ★ **The registry is written with `tomlkit`, validated before replace.** Only `register_project` and `unregister_project` write it, and only `/new` calls them. Your comments and order survive. An invalid result never reaches the file.
- ★ **`/new` commits exactly `vault/`, path-limited.** Your staged and modified work in that repo stays as it was, including through a rollback: a soft reset moves HEAD only, then `vault/` alone leaves the index. The commit is undone only while it's still HEAD and touches only `vault/`.
- ★ **Switching projects starts a fresh conversation;** pulling doesn't. History about project A mustn't leak into B. The session id, and so the session cap, belongs to the terminal.
- ★ **The system prompt runs from most stable to least stable,** so a `CONTEXT.md` change only re-bills the tail. Dated text, such as `/end`'s date, goes in the user message.
- ★ **`/end` goes through `propose_edit` with the current `base_sha256`,** never a direct write. A `CONTEXT.md` edited before approval makes the approval refuse as stale.
- **Practices are packed whole, in path order, until the 12,000-character cap.** The rest are named in the truncation note, for `read_note`. A single practice over the cap is cut.
- **`/end` reviews every proposal this conversation made that's still pending,** not only the ones from `/end`.
- **Block 6 keeps the 8 newest Log entries; the rest stay one `read_note` away.** Decisions index, Where things live, and Up Next stay whole, since they're bounded and the agent needs them every session.
- **`/new` doesn't switch to the new project;** it prints `/vault <name>` as the next step. `/vault` to the already-active project is a no-op.
- ★ **queue_cli is a stopgap.** Every command is one call into `daedalus.core`. Since commit 5 the REPL's `/pending`, `/diff`, `/approve`, and `/reject` call the same functions, and `queue_cli` stays as a debug tool.
- **templates/ is not notes.** One walk skips the vault's top-level `templates/` for every read tool, so the tools can't disagree about what a note is. An explicit `vault:templates/x.md` read still works.
- ★ **FTS5 is required.** `require_fts5()` fails with a clear message, and a test asserts FTS5 is present.
- ★ **Naming contract.** CLI `daedalus`; package `daedalus` with the safety core in `daedalus.core`; MCP server `daedalus-mcp`; global config `~/.config/daedalus/`; per-vault runtime state `.daedalus/` (git-ignored); shared vault `~/daedalus/shared/`.
- ★ **The schema block is the contract.** The fenced YAML block in `AGENTS.md` (both vaults) and in `CLAUDE.md` is the frontmatter schema. `tests/test_templates.py` parses each doc's block against `SCHEMA` (now in `daedalus.core.schema`), and fails if they disagree or if the three blocks aren't identical.
- ★ **Quote `"{{date}}"` and wikilinks in YAML.** Unquoted, `{{date}}` parses as a nested mapping and the frontmatter fails to load. Obsidian's Templates plugin still replaces it inside the quotes. Unquoted wikilinks are worse: `related: [[a]]` raises no error but parses as the nested list `[['a']]`. `AGENTS.md` requires quotes, and the lint in commit 15 will enforce them.
- ★ **No `.obsidian/` in the templates.** Obsidian's settings formats change between versions, so the Templates plugin is enabled by hand once per vault. The resulting `.obsidian/` is committed in each vault's own repo, with the workspace files ignored.
- **No YAML comments in template frontmatter.** Obsidian's Properties panel drops comments when it rewrites frontmatter, so the `decisions` and `tasks` entry shapes live in a "Plan format" section in the body of `plan.md`. `test_frontmatter_has_no_comments` guards this.
- **The repo root is guarded by a test, not ignored.** Adding `.obsidian/` to the repo's `.gitignore` would hide the mistake of opening the repo as a vault. A failing test surfaces it instead.
- The shared vault's `.gitignore` also ignores `.daedalus/`, because the naming contract says per-vault runtime state is git-ignored in every vault.
- Template defaults are tested: `status: draft`, `source: human`, `created: "{{date}}"`, empty lists as `[]`, valid-typed goal placeholders, and a body that starts with `# {{title}}`.
- `uv.lock` is committed. Since commit 2 the project builds with hatchling from a `src/` layout, and `uv sync --extra dev` installs it editable.

## Up Next

Commit 13: context sets.

Claude reads every file a task changes before submitting (`submit_plan` now says so; commit 13's `context_used` is where it gets a real check).

Let Mohamed grant extra iterations at a check-in.

`--retry` for failed plan tasks.

lint: warn on vault templates that drift from package templates.

validate_plan could check that a red_test names a test file (`/run`'s preflight does, at the task's start).

detached processes can outlive a run (sandboxed, low risk).

garbage-collect old venvs.

run output is sent to DeepSeek; the read denial, not the network denial, is what protects file contents.

When accepted practices exceed the 12,000-char cap, filter by project tags (registry `tags = [...]`).
