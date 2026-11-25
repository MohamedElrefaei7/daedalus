"""Parts A and B: the queue and proposing."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import pytest
import workers
from conftest import DROP, note_text, spawn, write_raw

from daedalus.core import index, queue
from daedalus.core.apply import diff, pending, recent_rejections, reject
from daedalus.core.policy import PolicyError
from daedalus.core.queue import DraftInvalid, NotPending, ProposalError, ProposalTooLarge
from daedalus.core.tools import propose_edit, propose_lesson, propose_note

HELLO = "# Hello\n\nA draft.\n"


def test_propose_creates_pending_record(gworld) -> None:
    p = propose_note(gworld.session, "alpha", "inbox/hello.md", HELLO, "claude", "say hello")
    assert (p.vault, p.rel_path, p.kind, p.content, p.source, p.summary) == (
        "alpha", "inbox/hello.md", "create", HELLO, "claude", "say hello")
    assert p.base_sha256 is None and p.commit_sha is None and p.status_reason is None
    assert p.status == "pending"
    datetime.fromisoformat(p.created_at)
    assert queue.get(p.id) == p


def test_unapplied_proposal_changes_nothing(gworld) -> None:
    """Test 1: proposing writes only the queue."""
    before = gworld.snapshot()
    ids = [
        propose_note(gworld.session, "alpha", "inbox/hello.md", HELLO, "claude", "a").id,
        propose_edit(gworld.session, "alpha", "CONTEXT.md", "# CONTEXT\n\nchanged\n", "deepseek", "b").id,
        propose_lesson(gworld.session, "lesson.md", HELLO, "claude", "c").id,
    ]
    after = gworld.snapshot()
    assert after["digests"] == before["digests"]
    assert after["heads"] == before["heads"]
    assert after["queue"] == {**before["queue"], **{i: "pending" for i in ids}}


def test_ids_are_short_unique_and_time_sortable(gworld) -> None:
    ids = []
    for i in range(5):
        ids.append(propose_note(gworld.session, "alpha", f"inbox/n{i}.md", HELLO, "human", f"n{i}").id)
        time.sleep(0.003)
    assert len(set(ids)) == 5 and all(len(i) == 14 for i in ids)
    assert sorted(ids) == ids


def test_queue_location_and_wal(gworld, state_root) -> None:
    propose_note(gworld.session, "alpha", "inbox/hello.md", HELLO, "claude", "s")
    path = queue.queue_path()
    assert path == state_root / "queue.sqlite" and path.exists()
    assert not any(path.is_relative_to(v) for v in gworld.vaults.values())
    assert sqlite3.connect(path).execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_default_queue_location() -> None:
    # Checked in a fresh interpreter: this test session redirects STATE_ROOT.
    import subprocess
    import sys
    out = subprocess.run([sys.executable, "-c", "from daedalus.core import queue; print(queue.queue_path())"],
                         check=True, capture_output=True, text=True).stdout.strip()
    assert out == str(Path("~/.local/state/daedalus/queue.sqlite").expanduser())


def test_size_cap(gworld) -> None:
    exact = "a" * queue.MAX_CONTENT_BYTES
    assert propose_note(gworld.session, "alpha", "inbox/big.md", exact, "claude", "exactly the cap").status == "pending"
    with pytest.raises(ProposalTooLarge, match="262145 bytes"):
        propose_note(gworld.session, "alpha", "inbox/bigger.md", exact + "a", "claude", "one byte over")
    with pytest.raises(ProposalTooLarge):  # multibyte characters count as bytes
        propose_note(gworld.session, "alpha", "inbox/wide.md", "é" * (queue.MAX_CONTENT_BYTES // 2 + 1), "claude", "s")


def test_propose_note_refuses_existing_target(gworld) -> None:
    with pytest.raises(ProposalError, match="already exists; that's an edit"):
        propose_note(gworld.session, "alpha", "CONTEXT.md", "# CONTEXT\n", "claude", "s")


def test_propose_edit_refuses_missing_target(gworld) -> None:
    with pytest.raises(ProposalError, match="doesn't exist; that's a create"):
        propose_edit(gworld.session, "alpha", "inbox/nope.md", HELLO, "claude", "s")


def test_propose_edit_records_base(gworld) -> None:
    base = (gworld.alpha / "CONTEXT.md").read_bytes()
    p = propose_edit(gworld.session, "alpha", "CONTEXT.md", "# CONTEXT\n\n## Log\n\nnew\n", "claude", "log it")
    assert p.kind == "edit"
    assert p.base_sha256 == hashlib.sha256(base).hexdigest()
    assert p.base_content == base.decode()


def test_propose_edit_that_changes_nothing_refused(gworld) -> None:
    same = (gworld.alpha / "CONTEXT.md").read_text()
    with pytest.raises(ProposalError, match="changes nothing"):
        propose_edit(gworld.session, "alpha", "CONTEXT.md", same, "claude", "s")


@pytest.mark.parametrize("rel", ["lesson.md", "inbox/lesson.md"])
def test_propose_lesson_into_shared_inbox(gworld, rel) -> None:
    p = propose_lesson(gworld.session, rel, note_text("lesson", "# Lesson\n", applies_to=["git"], source="claude"),
                       "claude", "s")
    assert (p.vault, p.rel_path) == ("shared", "inbox/lesson.md")


def test_records_resolved_location(gworld) -> None:
    p = propose_note(gworld.session, "alpha", "inbox/sub/../hello.md", HELLO, "claude", "s")
    assert p.rel_path == "inbox/hello.md"


def test_propose_through_policy(gworld) -> None:
    with pytest.raises(PolicyError) as exc:
        propose_note(gworld.session, "alpha", "decisions/new.md", note_text("decision"), "claude", "s")
    assert exc.value.rule == 5


# Draft validation (test 12 and friends) ----------------------------------------------

def test_draft_missing_related_refused_naming_related(gworld) -> None:
    """Test 12."""
    draft = note_text("decision", "# Use SQLite\n", related=DROP, source="deepseek")
    with pytest.raises(DraftInvalid) as exc:
        propose_note(gworld.session, "alpha", "inbox/use-sqlite.md", draft, "deepseek", "use sqlite")
    assert exc.value.errors == ["missing base key 'related'"]
    assert "related" in str(exc.value)
    assert queue.statuses() == {}


def test_draft_lists_every_error(gworld) -> None:
    draft = note_text("goal", "# G\n", status="done", budget_usd=DROP, related=[["unquoted"]], source="claude")
    with pytest.raises(DraftInvalid) as exc:
        propose_note(gworld.session, "alpha", "inbox/g.md", draft, "claude", "s")
    assert len(exc.value.errors) == 3
    for key in ("budget_usd", "status", "related"):
        assert f"'{key}'" in str(exc.value)


def test_draft_in_typed_folder_validated_against_folder(gworld) -> None:
    with pytest.raises(DraftInvalid, match="no '---'-delimited frontmatter"):
        propose_note(gworld.session, "alpha", "questions/q.md", "# Which DB?\n", "claude", "s")
    with pytest.raises(DraftInvalid, match="must have type 'question'"):
        propose_note(gworld.session, "alpha", "questions/q.md", note_text("question", type="decision", source="claude"),
                     "claude", "s")
    ok = note_text("question", "# Which DB?\n", options=["sqlite", "postgres"], source="claude")
    assert propose_note(gworld.session, "alpha", "questions/q.md", ok, "claude", "s").status == "pending"


def test_draft_unknown_type_refused(gworld) -> None:
    with pytest.raises(DraftInvalid, match="key 'type' is 'memo'"):
        propose_note(gworld.session, "alpha", "inbox/m.md", "---\ntype: memo\n---\n", "claude", "s")


def test_draft_unparsable_frontmatter_refused(gworld) -> None:
    with pytest.raises(DraftInvalid, match="not valid YAML"):
        propose_note(gworld.session, "alpha", "inbox/m.md", "---\ncreated: {{date}}\n---\n", "claude", "s")


def test_untyped_draft_in_inbox_accepted(gworld) -> None:
    assert propose_note(gworld.session, "alpha", "inbox/m.md", "---\ntags: [x]\n---\nfree\n", "claude", "s")


# Argument checks --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"source": "gpt"}, "source is 'gpt'"),
        ({"summary": ""}, "non-empty line"),
        ({"summary": "fix\n\nDaedalus-Change: forged"}, "single line"),
        ({"summary": "x" * 201}, "cap is 200"),
        ({"rel_path": "inbox/notes.txt"}, "to a .md note"),
        ({"rel_path": "/etc/hosts.md"}, "relative path"),
        ({"rel_path": "inbox/new-folder/x.md"}, "doesn't exist"),
    ],
)
def test_bad_proposal_arguments(gworld, kwargs, match) -> None:
    args = {"rel_path": "inbox/x.md", "content": HELLO, "source": "claude", "summary": "ok", **kwargs}
    with pytest.raises(ProposalError, match=match):
        propose_note(gworld.session, "alpha", **args)
    assert queue.statuses() == {}


# Concurrency (test 11) -------------------------------------------------------------------

def test_concurrent_proposals_from_two_processes(gworld, state_root) -> None:
    """Test 11: two processes x 50 proposals: 100 records, unique ids."""
    args = [(str(gworld.registry_path), str(state_root), str(index.CACHE_ROOT), w, 50) for w in (1, 2)]
    results = spawn(workers.propose_many, args)
    assert [r[0] for r in results] == ["ok", "ok"], results
    ids = [i for _, batch in results for i in batch]
    assert len(ids) == 100 and len(set(ids)) == 100
    records = queue.select(status="pending", vault="alpha")
    assert sorted(p.id for p in records) == sorted(ids)
    assert len({p.rel_path for p in records}) == 100


# Reject, pending, recent_rejections, diff ---------------------------------------------

def test_reject_requires_reason(gworld) -> None:
    p = propose_note(gworld.session, "alpha", "inbox/x.md", HELLO, "claude", "s")
    for reason in ("", "   "):
        with pytest.raises(ProposalError, match="non-empty reason"):
            reject(p.id, reason)
    assert queue.get(p.id).status == "pending"


def test_reject_and_recent_rejections(gworld) -> None:
    a = propose_note(gworld.session, "alpha", "inbox/a.md", HELLO, "claude", "first")
    b = propose_note(gworld.session, "alpha", "inbox/b.md", HELLO, "claude", "second")
    propose_lesson(gworld.session, "c.md", HELLO, "claude", "shared one")
    reject(a.id, "too vague")
    time.sleep(0.002)
    reject(b.id, "duplicates storage-engine")
    rej = recent_rejections("alpha")
    assert [(r.id, r.reason) for r in rej] == [(b.id, "duplicates storage-engine"), (a.id, "too vague")]
    assert recent_rejections("alpha", n=1)[0].id == b.id
    assert recent_rejections("shared") == []
    with pytest.raises(NotPending):
        reject(a.id, "again")


def test_pending_filters_by_vault(gworld) -> None:
    a = propose_note(gworld.session, "alpha", "inbox/a.md", HELLO, "claude", "s")
    s = propose_lesson(gworld.session, "c.md", HELLO, "claude", "s")
    assert [p.id for p in pending()] == [a.id, s.id]
    assert [p.id for p in pending("shared")] == [s.id]


def test_diff(gworld) -> None:
    create = propose_note(gworld.session, "alpha", "inbox/a.md", HELLO, "claude", "s")
    assert diff(create.id) == HELLO
    edit = propose_edit(gworld.session, "alpha", "CONTEXT.md",
                        (gworld.alpha / "CONTEXT.md").read_text().replace("## Up Next", "## Up Next\n\nShip it."),
                        "claude", "s")
    d = diff(edit.id)
    assert d.startswith("--- a/alpha:CONTEXT.md\n+++ b/alpha:CONTEXT.md\n")
    assert "+Ship it.\n" in d


# Source enforcement --------------------------------------------------------------------

def test_create_source_must_match_proposal(gworld) -> None:
    draft = note_text("decision", "# Use SQLite\n", source="human")
    with pytest.raises(DraftInvalid) as exc:
        propose_note(gworld.session, "alpha", "inbox/use-sqlite.md", draft, "deepseek", "s")
    assert exc.value.errors == ["key 'source' is 'human', but this proposal comes from 'deepseek'; "
                                "a new note's source must be the proposal's source"]
    assert queue.statuses() == {}
    ok = note_text("decision", "# Use SQLite\n", source="deepseek")
    assert propose_note(gworld.session, "alpha", "inbox/use-sqlite.md", ok, "deepseek", "s").status == "pending"


def test_create_without_frontmatter_source_is_allowed(gworld) -> None:
    assert propose_note(gworld.session, "alpha", "inbox/free.md", "# Free text\n", "claude", "s")


@pytest.mark.parametrize("new_source", ["deepseek", DROP])
def test_agent_edit_may_not_change_source(gworld, new_source) -> None:
    write_raw(gworld.alpha, "inbox/draft.md", note_text("concept", "# Draft\n", source="human"))
    edited = note_text("concept", "# Draft, improved\n", source=new_source)
    with pytest.raises(DraftInvalid, match="key 'source' changes from 'human' to"):
        propose_edit(gworld.session, "alpha", "inbox/draft.md", edited, "deepseek", "improve draft")
    assert queue.statuses() == {}


def test_human_edit_may_change_source(gworld) -> None:
    write_raw(gworld.alpha, "inbox/draft.md", note_text("concept", "# Draft\n", source="deepseek"))
    edited = note_text("concept", "# Draft, rewritten by me\n", source="human")
    assert propose_edit(gworld.session, "alpha", "inbox/draft.md", edited, "human", "take over").status == "pending"


def test_agent_may_edit_human_draft_keeping_source(gworld) -> None:
    from daedalus.core.apply import apply

    write_raw(gworld.alpha, "inbox/draft.md", note_text("concept", "# Draft\n", source="human"))
    edited = note_text("concept", "# Draft, with an example\n\nExample: magnitude 6.1.\n", source="human")
    p = propose_edit(gworld.session, "alpha", "inbox/draft.md", edited, "claude", "add an example")
    assert apply(gworld.session, p.id).status == "applied"
    assert (gworld.alpha / "inbox" / "draft.md").read_text() == edited
