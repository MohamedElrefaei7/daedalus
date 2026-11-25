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
        "goal": ["done_when", "max_iterations", "budget_usd", "checkin_every"],
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

# Frontmatter keys that hold lists of quoted wikilinks.
LINK_KEYS = ("related", "shared_refs", "project_refs")

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
