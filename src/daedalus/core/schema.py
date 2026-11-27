"""The frontmatter schema: the single source of truth for note structure.

The schema blocks in CLAUDE.md and in both AGENTS.md templates document this
dict, and tests/test_templates.py fails if they drift apart. The same dict
validates the templates (tests/test_templates.py) and live notes
(daedalus.core.notes), so the two can't disagree.
"""

from __future__ import annotations

SCHEMA = {
    "base": ["type", "status", "created", "related", "shared_refs", "project_refs", "source"],
    "extra": {
        "component": ["code"],
        "goal": ["done_when", "max_iterations", "budget_usd", "checkin_every", "protected"],
        "question": ["options", "answered"],
        "plan": ["spec", "decisions", "tasks"],
        "practice": ["applies_to"],
        "lesson": ["applies_to"],
    },
    "enums": {
        "status": ["draft", "proposed", "accepted", "superseded"],
        "source": ["human", "deepseek", "claude"],
    },
}

# Design choices that need precedent (an accepted decision or shared practice) or the human's answer.
# Every AGENTS.md names exactly these in its "Design choices" section; tests/test_templates.py checks.
DESIGN_CATEGORIES = ("schema", "dependency", "interface", "data_source")

# The frontmatter /add writes on a source note, in this order. sources/ is exempt from the typed schema
# (a source is a converted document, not a note an agent writes), but ingest must write exactly these keys.
SOURCE_KEYS = ("title", "original_name", "original_sha256", "original_bytes", "converter", "added", "source")

# Frontmatter keys that hold lists of quoted wikilinks.
LINK_KEYS = ("related", "shared_refs", "project_refs")
# Every list-valued key: each must be a YAML list, never a string (Obsidian writes a Text property as one).
LIST_KEYS = LINK_KEYS + ("protected", "code", "options", "applies_to", "decisions", "tasks")
# Lists whose items are strings (decisions and tasks hold mappings).
STRING_LIST_KEYS = LINK_KEYS + ("protected", "code", "options", "applies_to")

# Folders whose notes are schema-validated, and the type each one holds.
# Every other location (root files, inbox/, logs/, sources/, ...) is not.
PROJECT_TYPED_FOLDERS = {
    "decisions": "decision",
    "components": "component",
    "experiments": "experiment",
    "concepts": "concept",
    "goals": "goal",
    "questions": "question",
    "plans": "plan",
}
SHARED_TYPED_FOLDERS = {
    "practices": "practice",
    "lessons": "lesson",
}


def required_keys(note_type: str) -> list[str]:
    """Base keys plus the extra keys for ``note_type``."""
    return SCHEMA["base"] + SCHEMA["extra"].get(note_type, [])
