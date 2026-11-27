"""Part D: frontmatter parsing and schema validation of live notes."""

from __future__ import annotations

import pytest
from conftest import DROP, TEMPLATES, write_note, write_raw

from daedalus.core.notes import NoteSchemaError, read_note
from daedalus.core.policy import PolicyError
from daedalus.core.schema import PROJECT_TYPED_FOLDERS, SCHEMA, SHARED_TYPED_FOLDERS


def errors_for(world, path) -> list[str]:
    with pytest.raises(NoteSchemaError) as exc:
        read_note(path, world.session)
    assert exc.value.path == path
    return exc.value.errors


def test_valid_note_reads(world) -> None:
    note = read_note(world.alpha / "decisions" / "storage-engine.md", world.session)
    assert note.frontmatter["type"] == "decision"
    assert note.body == "\n# Storage engine\n\nSee [[usgs-fetcher]].\n"  # verbatim after the closing ---
    assert {(l.target, l.key) for l in note.links} == {
        ("usgs-fetcher", "related"), ("red-test-first", "shared_refs"), ("usgs-fetcher", "body"),
    }


@pytest.mark.parametrize("key", SCHEMA["base"])
def test_missing_base_key(world, key) -> None:
    path = write_note(world.alpha, "decisions/d.md", "decision", **{key: DROP})
    assert errors_for(world, path) == [f"missing base key '{key}'"]


@pytest.mark.parametrize("note_type,key", [(t, k) for t, keys in SCHEMA["extra"].items() for k in keys])
def test_missing_type_specific_key(world, note_type, key) -> None:
    folders = {**{v: k for k, v in PROJECT_TYPED_FOLDERS.items()}, **{v: k for k, v in SHARED_TYPED_FOLDERS.items()}}
    vault = world.shared if note_type in SHARED_TYPED_FOLDERS.values() else world.alpha
    path = write_note(vault, f"{folders[note_type]}/n.md", note_type, **{key: DROP})
    assert errors_for(world, path) == [f"missing key '{key}' required for type '{note_type}'"]


def test_type_must_match_folder(world) -> None:
    path = write_note(world.alpha, "decisions/d.md", "concept")
    assert errors_for(world, path) == ["key 'type' is 'concept', but notes in decisions/ must have type 'decision'"]


def test_type_must_match_folder_in_shared(world) -> None:
    path = write_note(world.shared, "lessons/l.md", "practice")
    errs = errors_for(world, path)
    assert "key 'type' is 'practice', but notes in lessons/ must have type 'lesson'" in errs


@pytest.mark.parametrize("key,value", [("status", "done"), ("status", "Accepted"), ("source", "gpt"), ("source", None)])
def test_bad_enum_value(world, key, value) -> None:
    path = write_note(world.alpha, "concepts/c.md", "concept", **{key: value})
    assert errors_for(world, path) == [f"key '{key}' is {value!r}, expected one of {SCHEMA['enums'][key]}"]


def test_unquoted_wikilink_in_related_fails(world) -> None:
    # related: [[a]] parses as [['a']] without any YAML error.
    text = (TEMPLATES / "project-vault" / "templates" / "decision.md").read_text()
    text = text.replace('created: "{{date}}"', 'created: "2026-11-25"').replace("related: []", "related: [[usgs-fetcher]]")
    path = write_raw(world.alpha, "decisions/unquoted.md", text)
    errs = errors_for(world, path)
    assert len(errs) == 1 and errs[0].startswith("key 'related' has non-string item(s) [['usgs-fetcher']]")


@pytest.mark.parametrize("key", ["related", "shared_refs", "project_refs"])
def test_non_string_link_item(world, key) -> None:
    path = write_note(world.alpha, "concepts/c.md", "concept", **{key: ["[[ok]]", 7]})
    assert errors_for(world, path)[0].startswith(f"key '{key}' has non-string item(s) [7]")


def test_link_key_must_be_a_list(world) -> None:
    path = write_note(world.alpha, "concepts/c.md", "concept", related="[[a]]")
    (error,) = errors_for(world, path)
    assert error.startswith("key 'related' must be a YAML list, not a string ('[[a]]')") and "type to List" in error


def test_error_names_file_and_every_bad_key(world) -> None:
    path = write_note(world.alpha, "goals/g.md", "goal", status="nope", source=DROP, budget_usd=DROP)
    with pytest.raises(NoteSchemaError) as exc:
        read_note(path, world.session)
    msg = str(exc.value)
    assert str(path) in msg
    for key in ("status", "source", "budget_usd"):
        assert f"'{key}'" in msg
    assert len(exc.value.errors) == 3


def test_unparsable_frontmatter(world) -> None:
    path = write_raw(world.alpha, "decisions/bad.md", "---\ntype: decision\ncreated: {{date}}\n---\nbody\n")
    errs = errors_for(world, path)
    assert len(errs) == 1 and errs[0].startswith("frontmatter is not valid YAML")


def test_missing_frontmatter_in_typed_folder(world) -> None:
    path = write_raw(world.alpha, "decisions/bare.md", "# No frontmatter\n")
    assert errors_for(world, path) == ["no '---'-delimited frontmatter block at the top of the file"]


def test_strict_false_returns_errors(world) -> None:
    path = write_note(world.alpha, "decisions/d.md", "decision", status="nope", type="concept")
    note, errors = read_note(path, world.session, strict=False)
    assert note.frontmatter["status"] == "nope"
    assert len(errors) == 2


def test_strict_false_valid_note_has_no_errors(world) -> None:
    note, errors = read_note(world.alpha / "decisions" / "storage-engine.md", world.session, strict=False)
    assert errors == [] and note.frontmatter["type"] == "decision"


@pytest.mark.parametrize("rel", ["AGENTS.md", "CONTEXT.md", "inbox/draft.md", "logs/run.md", "sources/paper.md"])
def test_untyped_locations_not_validated(world, rel) -> None:
    write_raw(world.alpha, rel, "---\ntype: whatever\nstatus: bogus\n---\nfree text\n")
    note = read_note(world.alpha / rel, world.session)
    assert note.frontmatter["status"] == "bogus"


def test_read_note_goes_through_policy(world) -> None:
    with pytest.raises(PolicyError) as exc:
        read_note(world.alpha / ".obsidian" / "app.json", world.session)
    assert exc.value.rule == 4


def test_typed_folders_match_templates() -> None:
    # Every typed folder exists in its template, and every template type has a typed folder.
    for vault, folders in (("project-vault", PROJECT_TYPED_FOLDERS), ("shared-vault", SHARED_TYPED_FOLDERS)):
        stems = {p.stem for p in (TEMPLATES / vault / "templates").glob("*.md")}
        assert set(folders.values()) == stems, f"templates/{vault}: typed folders {folders} != template types {stems}"
        for folder in folders:
            assert (TEMPLATES / vault / folder).is_dir(), f"templates/{vault}: typed folder '{folder}/' missing"


# List-typed keys must be lists (fix after commit 10c) ---------------------------------------
# What Obsidian's Properties panel wrote for `protected: ["tests/test_divide.py"]` typed into the field:
OBSIDIAN_PROTECTED = "protected:\n  - '[\"tests/test_divide.py\"]'\n"


def _goal(protected_yaml: str) -> str:
    return ("---\ntype: goal\nstatus: draft\ncreated: \"2026-11-27\"\nrelated: []\nshared_refs: []\n"
            "project_refs: []\nsource: human\ndone_when: pytest -q\nmax_iterations: 4\nbudget_usd: 0.1\n"
            f"checkin_every: 2\n{protected_yaml}---\n\n# Divide\n")


def _errors(text: str, note_type: str) -> list[str]:
    from daedalus.core.notes import load_frontmatter, split_frontmatter, validate

    raw, _ = split_frontmatter(text)
    fm, err = load_frontmatter(raw)
    assert err is None
    return validate(fm, note_type)


def test_the_exact_obsidian_text_fails_for_protected() -> None:
    errors = _errors(_goal(OBSIDIAN_PROTECTED), "goal")
    assert any("'protected'" in e and "whole list typed into one item" in e and "type to List" in e
               for e in errors), errors


def test_a_string_that_looks_like_a_list_fails_for_protected() -> None:
    errors = _errors(_goal('protected: \'["tests/test_divide.py"]\'\n'), "goal")
    assert any("key 'protected' must be a YAML list, not a string" in e and "type to List" in e
               for e in errors), errors


def test_the_same_text_fails_for_related() -> None:
    text = _goal("protected: []\n").replace("related: []", "related:\n  - '[\"tests/test_divide.py\"]'")
    errors = _errors(text, "goal")
    assert any("'related'" in e and "whole list typed into one item" in e for e in errors), errors
    text = _goal("protected: []\n").replace("related: []", "related: '[\"[[storage-engine]]\"]'")
    assert any("key 'related' must be a YAML list" in e for e in _errors(text, "goal"))


def test_a_real_list_passes() -> None:
    assert _errors(_goal('protected: ["tests/test_divide.py"]\n'), "goal") == []
    assert _errors(_goal("protected:\n  - tests/test_divide.py\n"), "goal") == []
    text = _goal("protected: []\n").replace("related: []", 'related: ["[[storage-engine]]"]')
    assert _errors(text, "goal") == [], "a wikilink item is a list item, not a typed-in list"
