"""The design-decision gate: what counts as precedent, and nothing else."""

from __future__ import annotations

import pytest
from conftest import note_text, write_note, write_raw

from daedalus.core import gate, queue
from daedalus.core.gate import NEEDS_USER, OK, REFUSED, check_basis, find_precedent, propose_with_basis
from daedalus.core.schema import DESIGN_CATEGORIES


def verdict(w, basis, category="data_source", choice="store data in SQLite"):
    return check_basis(w.session, category, choice, basis)


def refused(v) -> str:
    assert isinstance(v, REFUSED), f"expected REFUSED, got {v!r}"
    return v.reason


# Passing ------------------------------------------------------------------------------------

def test_accepted_active_decision_is_precedent(gworld) -> None:
    v = verdict(gworld, "[[storage-engine]]")
    assert isinstance(v, OK) and v.ref == "alpha:decisions/storage-engine.md"
    assert v.note.frontmatter["status"] == "accepted"


def test_accepted_shared_practice_is_precedent(gworld) -> None:
    v = verdict(gworld, "[[red-test-first]]", category="interface")
    assert isinstance(v, OK) and v.ref == "shared:practices/red-test-first.md"


def test_new_needs_the_user(gworld) -> None:
    assert verdict(gworld, "new") is NEEDS_USER
    assert verdict(gworld, " new ") is NEEDS_USER


# Refused: status ------------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["proposed", "draft", "superseded"])
def test_non_accepted_status_is_refused(gworld, status) -> None:
    write_note(gworld.alpha, f"decisions/{status}-db.md", "decision", f"# {status}\n", status=status)
    reason = refused(verdict(gworld, f"[[{status}-db]]"))
    assert f"is {status!r}, not accepted" in reason
    if status == "superseded":
        assert "superseded it" in reason
    else:
        assert "/answer or /promote" in reason


def test_proposed_note_is_refused(gworld) -> None:
    """Break 1 target: accepting proposed notes as precedent must fail here."""
    write_note(gworld.alpha, "decisions/maybe-postgres.md", "decision", "# Maybe Postgres\n", status="proposed")
    assert "'proposed', not accepted" in refused(verdict(gworld, "[[maybe-postgres]]"))


# Refused: place -------------------------------------------------------------------------------

@pytest.mark.parametrize("rel,note_type", [("components/usgs-fetcher.md", "component"),
                                           ("concepts/db-idea.md", "concept")])
def test_accepted_note_in_another_folder_is_refused(gworld, rel, note_type) -> None:
    if not (gworld.alpha / rel).exists():
        write_note(gworld.alpha, rel, note_type, "# x\n")
    stem = rel.rsplit("/", 1)[1][:-3]
    assert "isn't in this project's decisions/" in refused(verdict(gworld, f"[[{stem}]]"))


def test_shared_lesson_is_not_precedent(gworld) -> None:
    write_note(gworld.shared, "lessons/hooks-path.md", "lesson", "# Hooks\n", applies_to=["git"])
    assert "isn't in the shared practices/" in refused(verdict(gworld, "[[hooks-path]]"))


def test_pulled_projects_accepted_decision_is_refused(gworld) -> None:
    """Break 2 target: another project's decisions don't bind this one."""
    write_note(gworld.beta, "decisions/beta-uses-postgres.md", "decision", "# Postgres\n")
    reason = refused(verdict(gworld, "[[beta-uses-postgres]]"))
    assert "pulled project 'beta'" in reason and "don't bind 'alpha'" in reason
    assert "shared practice" in reason


def test_inbox_draft_is_refused(gworld) -> None:
    write_raw(gworld.alpha, "inbox/idea.md", note_text("decision", "# idea\n"))
    assert "isn't in this project's decisions/" in refused(verdict(gworld, "[[inbox/idea]]"))


# Refused: the link ----------------------------------------------------------------------------

def test_missing_link_is_refused(gworld) -> None:
    reason = refused(verdict(gworld, "[[no-such-decision]]"))
    assert "doesn't name a note" in reason and "'new'" in reason


def test_ambiguous_link_is_refused(gworld) -> None:
    write_note(gworld.shared, "practices/storage-engine.md", "practice", "# also\n", applies_to=["db"])
    reason = refused(verdict(gworld, "[[storage-engine]]"))
    assert "ambiguous" in reason and "alpha:decisions/storage-engine.md" in reason
    assert "[[decisions/storage-engine]]" in reason
    assert isinstance(verdict(gworld, "[[decisions/storage-engine]]"), OK)  # by path, it resolves


def test_out_of_scope_link_is_refused(gworld) -> None:
    write_note(gworld.gamma, "decisions/gamma-db.md", "decision", "# g\n")
    assert "doesn't name a note" in refused(verdict(gworld, "[[gamma-db]]"))
    assert "not in this session" in refused(verdict(gworld, "[[gamma:decisions/gamma-db.md]]"))


def test_unknown_category_is_refused(gworld) -> None:
    assert "unknown design category 'database'" in refused(verdict(gworld, "[[storage-engine]]", category="database"))


def test_basis_must_be_a_wikilink_or_new(gworld) -> None:
    assert "must be a wikilink" in refused(verdict(gworld, "storage-engine"))
    assert "must be a wikilink" in refused(verdict(gworld, ""))


def test_invalid_decision_is_refused(gworld) -> None:
    write_raw(gworld.alpha, "decisions/broken.md", "---\ntype: decision\nstatus: accepted\n---\n# broken\n")
    assert "breaks the schema" in refused(verdict(gworld, "[[broken]]"))


def test_the_gate_reads_through_the_policy(gworld) -> None:
    """A link into .obsidian/ is refused by the read path, never read around it."""
    assert isinstance(verdict(gworld, "[[alpha:.obsidian/app.json]]"), REFUSED)


# propose_with_basis ---------------------------------------------------------------------------

def test_propose_with_basis_queues_only_with_precedent(gworld) -> None:
    draft = note_text("component", "# Store\n", source="deepseek", related=["[[storage-engine]]"])
    p = propose_with_basis(gworld.session, "data_source", "SQLite", "[[storage-engine]]", "alpha",
                           "inbox/store.md", draft, "deepseek", "storage component")
    assert queue.get(p.id).status == "pending"
    with pytest.raises(gate.GateRefused, match="ask_user"):
        propose_with_basis(gworld.session, "data_source", "SQLite", "new", "alpha", "inbox/store2.md", draft,
                           "deepseek", "s")
    assert len(queue.statuses()) == 1


# find_precedent -------------------------------------------------------------------------------

def test_find_precedent_returns_only_accepted_active_decisions_and_shared_practices(gworld) -> None:
    write_note(gworld.alpha, "decisions/sqlite.md", "decision", "# Use SQLite\n\nStore data in SQLite.\n")
    write_note(gworld.alpha, "decisions/pg.md", "decision", "# Postgres\n\nSQLite or Postgres?\n", status="proposed")
    write_note(gworld.alpha, "components/db.md", "component", "# DB\n\nSQLite wrapper.\n")
    write_note(gworld.beta, "decisions/beta-sqlite.md", "decision", "# SQLite in beta\n")
    write_note(gworld.shared, "practices/sqlite-wal.md", "practice", "# SQLite in WAL mode\n",
               applies_to=["sqlite"])
    write_note(gworld.shared, "lessons/sqlite-lock.md", "lesson", "# SQLite lock\n", applies_to=["sqlite"])
    found = find_precedent(gworld.session, "sqlite wrapper")
    assert {p.ref for p in found} == {"alpha:decisions/sqlite.md", "shared:practices/sqlite-wal.md"}
    one = next(p for p in found if p.ref == "alpha:decisions/sqlite.md")
    assert (one.title, one.status) == ("Use SQLite", "accepted")
    assert one.excerpt.startswith("# Use SQLite") and len(one.excerpt) <= 300
    for p in found:
        assert isinstance(check_basis(gworld.session, "data_source", "x", f"[[{p.ref.split(':')[1][:-3]}]]"), OK)


def test_find_precedent_caps_at_ten_and_puts_the_category_first(gworld) -> None:
    for i in range(14):
        body = f"# Cache {i}\n\nA cache decision.\n" + ("\nCategory: `dependency`\n" if i == 13 else "")
        write_note(gworld.alpha, f"decisions/cache-{i:02d}.md", "decision", body)
    found = find_precedent(gworld.session, "cache", "dependency")
    assert len(found) == 10 and found[0].ref == "alpha:decisions/cache-13.md"


def test_find_precedent_none_and_bad_input(gworld) -> None:
    assert find_precedent(gworld.session, "kubernetes") == []
    with pytest.raises(gate.GateRefused, match="unknown design category"):
        find_precedent(gworld.session, "x", "database")
    with pytest.raises(gate.GateRefused, match="needs a topic"):
        find_precedent(gworld.session, "  ")


def test_categories_are_the_four() -> None:
    assert DESIGN_CATEGORIES == ("schema", "dependency", "interface", "data_source")
