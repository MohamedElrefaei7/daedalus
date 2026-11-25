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

## Key decisions

- ★ **One schema, in `daedalus.core.schema`.** `SCHEMA` moved out of `tests/test_templates.py`, which now imports it, so one dict guards both the templates and live notes.
- ★ **`.obsidian/` in any parent of a project vault fails registry load.** This is checked up to and including the repo root, the nearest parent holding `.git`, and catches the commit-1 mistake of opening the repo as a vault. Registering a folder with no `AGENTS.md` fails too, which catches registering `~/code/daedalus-scratch` instead of `.../vault`.
- ★ **The session is passed in explicitly.** Core never reads `state.json`, which is commit 6's job, so every core function is testable without `~/.config`.
- ★ **Deny by default.** Each area has an explicit allow table per op, and anything not listed is refused. A consequence: deleting an area's branch makes the policy stricter, not looser.
- ★ **Rule 4 compares case-insensitively.** macOS file systems ignore case, so `.OBSIDIAN/app.json` *is* `.obsidian/app.json`. Allow checks stay exact-case: a case variant of an allowed name is refused, which errs safe.
- ★ **Typed folders are validated against SCHEMA,** and link-list keys must hold only strings. That catches `related: [[a]]`, which parses as `[['a']]`.
- ★ **The search index lives in `~/.cache/daedalus/index/<sha1>.sqlite`, never in a vault.** Indexing is system state, not an agent write. The shared vault stays untouched and `git status` stays clean.
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

Commit 3: proposals, approval queue, apply + commit (adds the vault-hash-unchanged check to every policy test).

Commit 10: rule 7 must refuse program paths containing / and pin allowed programs to resolved locations; basename matching lets /tmp/evil/pytest through.
