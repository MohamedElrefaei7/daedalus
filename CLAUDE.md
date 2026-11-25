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
├── pyproject.toml            # hatchling build, src/ layout; runtime dep pyyaml, dev dep pytest
├── uv.lock
├── config/
│   └── vaults.example.toml   # shape of ~/.config/daedalus/vaults.toml
├── scripts/
│   └── smoke_read.py         # live read-layer smoke test against the real registry
├── src/daedalus/
│   └── core/                 # the safety core
│       ├── schema.py         # SCHEMA: single source of truth for frontmatter
│       ├── registry.py       # vaults.toml loading + validation (RegistryError)
│       ├── session.py        # Session dataclass, Area enum
│       ├── policy.py         # Op enum, authorize(), authorize_command(): the seven rules
│       ├── notes.py          # frontmatter parsing + schema validation (NoteSchemaError)
│       ├── links.py          # wikilink extraction + Obsidian-style resolution
│       ├── index.py          # SQLite FTS5 search index
│       └── tools.py          # list_notes, read_note, backlinks, search
├── templates/
│   ├── project-vault/        # copied wholesale to create a project vault
│   └── shared-vault/         # copied to create ~/daedalus/shared/
└── tests/
    ├── test_templates.py     # template contract test
    └── core/                 # one file per core part, plus test_bench.py
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

The `Session` is passed in explicitly; core never reads `state.json`.

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
