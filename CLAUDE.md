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
├── pyproject.toml            # uv project; runtime dep pyyaml, dev dep pytest
├── uv.lock
├── config/
│   └── vaults.example.toml   # shape of ~/.config/daedalus/vaults.toml
├── templates/
│   ├── project-vault/        # copied wholesale to create a project vault
│   └── shared-vault/         # copied to create ~/daedalus/shared/
└── tests/
    └── test_templates.py     # template contract test
```

Run the tests with `uv run pytest`.

## Templates are the source of truth

`templates/project-vault/` and `templates/shared-vault/` are the source of truth for vault structure: folders, `AGENTS.md`, `.gitignore`, and one note template per type. Change vault structure there and nowhere else. `tests/test_templates.py` guards them, and its `SCHEMA` dict must agree with the block below.

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
