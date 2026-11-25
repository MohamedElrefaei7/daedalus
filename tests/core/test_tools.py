"""Part E: list_notes and read_note refs."""

from __future__ import annotations

import pytest
from conftest import write_note, write_raw

from daedalus.core.notes import NoteSchemaError
from daedalus.core.policy import PolicyError
from daedalus.core.tools import NoteEntry, RefError, list_notes, read_note


def test_list_covers_active_pulled_and_shared(world) -> None:
    vaults = {e.vault for e in list_notes(world.session)}
    assert vaults == {"alpha", "beta", "shared"}  # gamma is registered but not pulled


def test_list_entries(world) -> None:
    entries = list_notes(world.session)
    assert NoteEntry("alpha", "decisions/storage-engine.md", "decision", "accepted") in entries
    assert NoteEntry("shared", "practices/red-test-first.md", "practice", "accepted") in entries
    assert NoteEntry("alpha", "AGENTS.md", None, None) in entries


def test_list_skips_templates_and_dot_folders(world) -> None:
    write_raw(world.alpha, ".obsidian/snippets/x.md", "hidden\n")
    rels = {e.rel_path for e in list_notes(world.session)}
    assert not any(r.startswith(("templates/", ".obsidian/")) for r in rels)


def test_list_filters(world) -> None:
    write_note(world.alpha, "decisions/draft-one.md", "decision", status="draft")
    assert [e.rel_path for e in list_notes(world.session, folder="decisions", status="draft")] == ["decisions/draft-one.md"]
    assert {e.rel_path for e in list_notes(world.session, type="practice")} == {"practices/red-test-first.md"}
    assert {e.rel_path for e in list_notes(world.session, folder="components/")} == {"components/usgs-fetcher.md"}


def test_invalid_note_listed_not_raised(world) -> None:
    write_note(world.alpha, "decisions/broken.md", "decision", status="nope")
    write_raw(world.alpha, "concepts/unparsable.md", "---\ncreated: {{date}}\n---\n")
    write_raw(world.alpha, "concepts/binary.md", "x")
    (world.alpha / "concepts" / "binary.md").write_bytes(b"\xff\xfe\x00")
    invalid = {e.rel_path for e in list_notes(world.session, status="INVALID")}
    assert invalid == {"decisions/broken.md", "concepts/unparsable.md", "concepts/binary.md"}


def test_read_vault_qualified_ref(world) -> None:
    note = read_note(world.session, "shared:practices/red-test-first.md")
    assert note.frontmatter["applies_to"] == ["testing"]


def test_read_ref_strict_by_default(world) -> None:
    write_note(world.alpha, "decisions/broken.md", "decision", status="nope")
    with pytest.raises(NoteSchemaError):
        read_note(world.session, "alpha:decisions/broken.md")
    _, errors = read_note(world.session, "alpha:decisions/broken.md", strict=False)
    assert errors


@pytest.mark.parametrize("ref", ["gamma:concepts/secret.md", "nope:x.md", "alpha:", "alpha:/etc/hosts", "missing-note", "[[#heading]]"])
def test_bad_refs(world, ref) -> None:
    with pytest.raises(RefError):
        read_note(world.session, ref)


def test_ref_traversal_refused_by_policy(world) -> None:
    with pytest.raises(PolicyError) as exc:
        read_note(world.session, "alpha:../../gamma/vault/concepts/secret.md")
    assert exc.value.rule == 1
