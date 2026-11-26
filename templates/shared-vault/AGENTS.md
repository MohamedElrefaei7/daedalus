# AGENTS.md — shared workflow vault

Every agent reads this file first, before any other note in this vault.

## 1. Purpose

This vault holds practices and lessons that apply across projects. A practice is a way of working that has earned its place; a lesson is a record of something that broke, why, and the fix. Project vaults cite these notes through `shared_refs`.

## 2. Agents propose into `inbox/` only

Agents may create notes in `inbox/` and nowhere else. They never edit or delete notes in `practices/`, `lessons/`, `templates/`, or this file.

## 3. The human promotes

Only the human promotes a note: they move it from `inbox/` to `practices/` or `lessons/` and set `status: accepted`.

## 4. Every practice and lesson has `applies_to` tags

`applies_to` is a list of lowercase topic tags, such as `[testing]` or `[workflow]`. Agents use these tags to find the notes that bear on the task at hand. An accepted practice or lesson has at least one tag.

## 5. Linking

Every note links to at least one other note, as in the project vaults. Any link counts: one in `related`, `shared_refs`, or `project_refs`, or a wikilink in the body. A practice or lesson usually links a related practice in `related`. `AGENTS.md` is never a valid link target: it describes the vault rather than being a note in it, so a draft that links `[[AGENTS]]` (or a project's `[[CONTEXT]]`) is refused.

## 6. Design choices

Accepted practices in `practices/` are precedent for design choices in every project, in these categories:

- `schema`: the shape of stored data, files, or messages
- `dependency`: adding a library, service, or tool
- `interface`: how two components talk to each other
- `data_source`: where data comes from

A project's own decisions bind only that project. When one should apply everywhere, promote it into a practice here.

## Frontmatter schema

Same contract as the project vaults. Wikilinks inside YAML must be quoted: `related: ["[[red-test-first]]"]`.

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
