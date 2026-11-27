---
type: plan
status: draft
created: "{{date}}"
related: []
shared_refs: []
project_refs: []
source: human
spec: ""
decisions: []
tasks: []
---

# {{title}}

## Context and non-obvious decisions

## Plan format

- `spec`: a wikilink to the note holding the specification, in this vault.
- `decisions`: each entry is `{category, choice, basis}`. `category` is one of `schema`, `dependency`, `interface`, `data_source`; `basis` is `"[[note]]"` (an accepted decision here, or an accepted shared practice) or `"new"`.
- `tasks`: each entry is `{id, summary, files, red_test, done_when, max_iterations, budget_usd}`, plus an optional `protected` list. `id` is `t1`, `t2`, …; `files` are repo-relative, never under `vault/` or `.git`; `max_iterations` is 1 to 10; `budget_usd` is at most the session cap.
