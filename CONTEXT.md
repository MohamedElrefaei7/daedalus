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

## Key decisions

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

Commit 10c: the coding loop — goal notes, plan/edit/run/summarize iterations, caps, check-ins, lessons.

detached processes can outlive a run (sandboxed, low risk).

garbage-collect old venvs.

run output is sent to DeepSeek; the read denial, not the network denial, is what protects file contents.

When accepted practices exceed the 12,000-char cap, filter by project tags (registry `tags = [...]`).
