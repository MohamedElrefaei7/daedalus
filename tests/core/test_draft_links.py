"""The link rule at propose time: a typed draft links at least one note, and never a root file.

The rule is satisfied by any of related, shared_refs, project_refs, or a body
link. AGENTS.md and CONTEXT.md are never valid link targets. Typed drafts are
the ones validated against a type: drafts for a typed folder, and drafts that
declare a type anywhere else (an inbox/ draft headed for concepts/).
"""

from __future__ import annotations

import pytest
from conftest import note_text

from daedalus.core import queue
from daedalus.core.notes import validate
from daedalus.core.queue import DraftInvalid
from daedalus.core.tools import propose_note


def propose(w, rel, content, vault="alpha", source="claude"):
    return propose_note(w.session, vault, rel, content, source, "s")


def concept(**fm):
    return note_text("concept", "# A concept\n", **{"source": "claude", **fm})


def test_link_only_to_context_is_refused(world) -> None:
    with pytest.raises(DraftInvalid) as exc:
        propose(world, "inbox/c.md", concept(related=["[[CONTEXT]]"]))
    assert "CONTEXT" in str(exc.value) and "never" in str(exc.value)
    assert queue.statuses() == {}


def test_only_link_in_shared_refs_is_accepted(world) -> None:
    p = propose(world, "inbox/c.md", concept(shared_refs=["[[red-test-first]]"]))
    assert queue.get(p.id).status == "pending"


@pytest.mark.parametrize("created", ['"2026-11-25"', "2026-11-25"], ids=["quoted", "unquoted"])
def test_quoted_and_unquoted_created_both_validate(world, created) -> None:
    text = concept(shared_refs=["[[red-test-first]]"]).replace("created: '2026-11-25'", f"created: {created}")
    assert f"created: {created}\n" in text
    p = propose(world, f"inbox/c-{len(created)}.md", text)
    assert queue.get(p.id).status == "pending"
    import yaml
    fm = yaml.safe_load(text.split("---\n")[1])
    assert validate(fm, "concept") == []


def test_no_link_at_all_is_refused(world) -> None:
    with pytest.raises(DraftInvalid) as exc:
        propose(world, "inbox/c.md", concept())
    assert "related, shared_refs, or project_refs" in str(exc.value)


@pytest.mark.parametrize("fm, body", [
    ({"related": ["[[AGENTS]]"]}, ""),
    ({"related": ["[[CONTEXT.md]]"]}, ""),
    ({"related": ["[[context]]"]}, ""),
    ({"shared_refs": ["[[red-test-first]]"]}, "See [[CONTEXT]].\n"),
    ({"shared_refs": ["[[AGENTS]]"]}, ""),
], ids=["agents", "context_path", "context_lowercase", "body_link_to_context", "shared_agents"])
def test_any_link_to_a_root_file_is_refused(world, fm, body) -> None:
    with pytest.raises(DraftInvalid) as exc:
        propose(world, "inbox/c.md", note_text("concept", f"# x\n\n{body}", source="claude", **fm))
    assert "never" in str(exc.value)


@pytest.mark.parametrize("fm, body", [
    ({"related": ["[[storage-engine]]"]}, ""),
    ({"project_refs": ["[[quake-magnitude]]"]}, ""),
    ({}, "Builds on [[storage-engine]].\n"),
    ({"related": ["[[not-written-yet]]"]}, ""),  # a dangling link still counts: Obsidian allows them
], ids=["related", "project_refs", "body", "dangling"])
def test_any_link_field_satisfies_the_rule(world, fm, body) -> None:
    propose(world, "inbox/c.md", note_text("concept", f"# x\n\n{body}", source="claude", **fm))


def test_typed_folder_draft_needs_a_link(world) -> None:
    q = note_text("question", "# Which store?\n", source="claude")
    with pytest.raises(DraftInvalid):
        propose(world, "questions/which.md", q)
    propose(world, "questions/which.md", note_text("question", "# Which store?\n", source="claude",
                                                   related=["[[storage-engine]]"]))


def test_untyped_inbox_draft_has_no_link_rule(world) -> None:
    propose(world, "inbox/scratch.md", "just a jotting, no frontmatter\n")


def test_shared_inbox_lesson_needs_a_link(world) -> None:
    lesson = note_text("lesson", "# L\n", source="claude", applies_to=["testing"])
    with pytest.raises(DraftInvalid):
        propose(world, "inbox/l.md", lesson, vault="shared")
    propose(world, "inbox/l.md", note_text("lesson", "# L\n", source="claude", applies_to=["testing"],
                                           related=["[[red-test-first]]"]), vault="shared")
