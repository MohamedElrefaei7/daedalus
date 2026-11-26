# AGENTS.md

Every agent reads this file first, before any other note in the vault.

## 1. Purpose

This vault is the project's knowledge graph. Agents read it before acting. Agents propose changes and never apply them: the table below says where each writer may write, and everything else waits for the human.

## 2. Folders and who writes them

| Path | Holds | Who writes |
|---|---|---|
| `AGENTS.md` | These rules | Human only |
| `CONTEXT.md` | Running log, decisions index, where things live, Up Next | Agents propose, human approves |
| `goals/` | One note per goal with a done-condition and caps | Human, or `/run` from an approved plan |
| `sources/` | Uploaded documents as markdown | `/add` only |
| `logs/` | One summary per agent iteration | Agents, create-only |
| `inbox/` | Drafts awaiting review | Agents and Claude |
| `questions/` | Design questions waiting on the human | Agents |
| `decisions/`, `components/`, `experiments/`, `concepts/` | Accepted notes | Human, by promoting from `inbox/` |
| `plans/` | Plans from Claude, every version kept | Claude via daedalus-mcp; human approves |
| `templates/` | One template per note type | Human only |

## 3. Frontmatter schema

Every note starts with a YAML frontmatter block. This block is the contract; `tests/test_templates.py` in the daedalus repo checks the templates against it.

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

## 4. Design choices

A design choice in one of these categories needs precedent before an agent acts on it:

- `schema`: the shape of stored data, files, or messages
- `dependency`: adding a library, service, or tool
- `interface`: how two components talk to each other
- `data_source`: where data comes from

Precedent is an accepted note in this project's `decisions/`, or an accepted practice in the shared vault's `practices/`, and nothing else. A draft or proposed note doesn't count, nor does a note in any other folder, nor another project's decisions: to make one apply everywhere, promote it to a shared practice. Cite precedent by linking it: a decision in `related`, a practice in `shared_refs`.

Look for precedent with `find_precedent`. With none, ask with `ask_user`, which proposes a note in `questions/`, and stop until the human answers. The answer becomes an accepted decision, which is precedent from then on.

## 5. Linking

Every note links to at least one other note. Any link counts: one in `related`, `shared_refs`, or `project_refs`, or a wikilink in the body. `AGENTS.md` and `CONTEXT.md` are never valid link targets. They describe the vault rather than being notes in it, so `[[CONTEXT]]` and `[[AGENTS]]` don't count, and a draft that links either one is refused.

Wikilinks inside YAML must be quoted: `related: ["[[storage-engine]]"]`. Unquoted, `related: [[storage-engine]]` raises no error but parses as a nested list, and the link is lost.
