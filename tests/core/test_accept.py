"""ask_user's question, /answer, /promote, and the multi-path apply under them."""

from __future__ import annotations

import multiprocessing

import pytest
import workers
import yaml
from conftest import git, head, note_text, write_note, write_raw
from test_apply import commits_since, install_hook

from daedalus.core import accept, index, notes, queue
from daedalus.core.accept import AnswerError, PromoteError, answer_question, ask_user, promote
from daedalus.core.apply import Change, apply, apply_changes
from daedalus.core.errors import CommitFailed, DraftInvalid, StaleProposal, UnknownProposal
from daedalus.core.policy import Op, PolicyError, authorize
from daedalus.core.tools import propose_note

QUESTION = "Should alpha store its data in SQLite or Postgres?"


def ask(w, options=("SQLite", "Postgres"), refs=("[[storage-engine]]",), question=QUESTION):
    return ask_user(w.session, "deepseek", question, "data_source", list(options), list(refs))


def fm(path) -> dict:
    raw, _ = notes.split_frontmatter(path.read_text())
    return yaml.safe_load(raw)


def changed_files(repo, rev="HEAD") -> list[tuple[str, str]]:
    out = git(repo, "show", "--name-status", "--no-renames", "--format=", rev)
    return sorted(tuple(line.split("\t")) for line in out.splitlines() if line.strip())


# ask_user ------------------------------------------------------------------------------------

def test_ask_user_proposes_a_typed_linked_question(gworld) -> None:
    asked = ask(gworld)
    p = queue.get(asked.proposal.id)
    assert (p.vault, p.rel_path, p.kind, p.status, p.source) == (
        "alpha", "questions/should-alpha-store-its-data-in-sqlite-or-postgres.md", "create", "pending", "deepseek")
    raw, body = notes.split_frontmatter(p.content)
    meta = yaml.safe_load(raw)
    assert meta["type"] == "question" and meta["source"] == "deepseek"
    assert meta["options"] == ["SQLite", "Postgres"] and meta["answered"] is False
    assert meta["related"] == ["[[storage-engine]]"]
    assert QUESTION in body and "Category: `data_source`" in body and "1. SQLite" in body
    assert "Why no precedent applied:" in body
    auth = authorize(gworld.alpha / p.rel_path, Op.PROPOSE, gworld.session)
    assert notes.draft_errors(p.content, auth) == []


@pytest.mark.parametrize("kwargs,match", [
    ({"refs": ()}, "context_refs must name"),
    ({"refs": ("[[CONTEXT]]",)}, "AGENTS.md and CONTEXT.md are never link targets"),
    ({"options": ("only one",)}, "at least two distinct"),
    ({"options": ("a", "a")}, "at least two distinct"),
    ({"question": "  "}, "needs the question"),
])
def test_ask_user_refusals_queue_nothing(gworld, kwargs, match) -> None:
    with pytest.raises(Exception, match=match):
        ask(gworld, **kwargs)
    assert queue.statuses() == {}


def test_ask_user_unknown_category(gworld) -> None:
    with pytest.raises(Exception, match="unknown design category"):
        ask_user(gworld.session, "deepseek", QUESTION, "database", ["a", "b"], ["[[storage-engine]]"])


# /answer ---------------------------------------------------------------------------------------

def test_answer_is_one_commit_with_exactly_the_decision_and_the_question(gworld) -> None:
    asked = ask(gworld)
    q = apply(gworld.session, asked.proposal.id)  # the question was approved earlier
    repo, base = gworld.alpha_repo, head(gworld.alpha_repo)

    done = answer_question(gworld.session, asked.proposal.id, "1")

    assert commits_since(repo, base) == 1, "the decision and the question update must be one commit"
    stem = q.rel_path[len("questions/"):-3]
    assert changed_files(repo) == [("A", f"vault/decisions/{stem}.md"), ("M", f"vault/{q.rel_path}")]
    assert git(repo, "log", "-1", "--format=%s").strip().startswith("answer: Should alpha store")
    assert f"Daedalus-Change: " in git(repo, "log", "-1", "--format=%B")
    assert git(repo, "status", "--porcelain") == ""

    decision = fm(gworld.alpha / "decisions" / f"{stem}.md")
    assert (decision["type"], decision["status"], decision["source"]) == ("decision", "accepted", "human")
    assert decision["related"] == [f"[[questions/{stem}]]"]
    body = (gworld.alpha / "decisions" / f"{stem}.md").read_text()
    assert "## Decision\n\nSQLite\n" in body and "## Rejected options\n\n- Postgres\n" in body
    assert QUESTION in body and "Category: `data_source`" in body

    question = fm(gworld.alpha / q.rel_path)
    assert question["answered"] is True
    assert question["related"] == ["[[storage-engine]]", f"[[decisions/{stem}]]"]
    assert question["source"] == "deepseek"  # provenance kept
    assert f"SQLite. Decided in [[decisions/{stem}]]." in (gworld.alpha / q.rel_path).read_text()
    assert done.decision_ref == f"alpha:decisions/{stem}.md" and done.approved_question is None


def test_answer_approves_a_pending_question_first(gworld) -> None:
    asked = ask(gworld)
    repo, base = gworld.alpha_repo, head(gworld.alpha_repo)
    done = answer_question(gworld.session, asked.proposal.id, '"Postgres, with a pool"')
    assert commits_since(repo, base) == 2  # the approval, then the answer
    assert queue.get(asked.proposal.id).status == "applied" and done.approved_question
    body = (gworld.alpha / done.decision_ref.split(":", 1)[1]).read_text()
    assert "## Decision\n\nPostgres, with a pool\n" in body
    assert "- SQLite\n- Postgres\n" in body  # free text: every option was rejected


def test_the_decision_is_then_precedent(gworld) -> None:
    from daedalus.core.gate import OK, check_basis, find_precedent

    done = answer_question(gworld.session, ask(gworld).proposal.id, "1")
    found = find_precedent(gworld.session, "sqlite storage", "data_source")
    assert found[0].ref == done.decision_ref
    stem = done.decision_ref.split(":", 1)[1][:-3]
    assert isinstance(check_basis(gworld.session, "data_source", "SQLite", f"[[{stem}]]"), OK)


def test_failing_hook_rolls_back_both_files(gworld) -> None:
    asked = ask(gworld)
    q = apply(gworld.session, asked.proposal.id)
    qpath = gworld.alpha / q.rel_path
    before_q, base = qpath.read_bytes(), head(gworld.alpha_repo)
    decisions_before = sorted(p.name for p in (gworld.alpha / "decisions").iterdir())
    install_hook(gworld.alpha_repo)

    with pytest.raises(CommitFailed, match="hook says no"):
        answer_question(gworld.session, asked.proposal.id, "1")

    assert qpath.read_bytes() == before_q, "the question was left edited"
    assert sorted(p.name for p in (gworld.alpha / "decisions").iterdir()) == decisions_before, "decision left behind"
    assert head(gworld.alpha_repo) == base
    assert git(gworld.alpha_repo, "status", "--porcelain") == ""
    (cs,) = queue.changesets()
    assert cs.status == "failed" and "hook says no" in cs.status_reason


def test_crash_between_writes_and_commit_restores_both(gworld, state_root) -> None:
    asked = ask(gworld)
    q = apply(gworld.session, asked.proposal.id)
    qpath = gworld.alpha / q.rel_path
    before_q, base = qpath.read_bytes(), head(gworld.alpha_repo)
    stem = q.rel_path[len("questions/"):-3]

    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=workers.answer_and_crash,
                       args=(str(gworld.registry_path), str(state_root), str(index.CACHE_ROOT), asked.proposal.id))
    proc.start()
    proc.join(timeout=60)
    assert proc.exitcode == 1
    assert (gworld.alpha / "decisions" / f"{stem}.md").exists(), "the crash should land after the writes"
    assert qpath.read_bytes() != before_q

    queue.connect().close()  # a fresh queue open reconciles

    assert not (gworld.alpha / "decisions" / f"{stem}.md").exists()
    assert qpath.read_bytes() == before_q
    assert head(gworld.alpha_repo) == base
    assert git(gworld.alpha_repo, "status", "--porcelain") == ""
    (cs,) = queue.changesets()
    assert (cs.status, cs.status_reason) == ("failed", "interrupted")


@pytest.mark.parametrize("answer,match", [("3", "option 3 doesn't exist"), ("0", "option 0 doesn't exist"),
                                          ("''", "give an option number")])
def test_bad_answers_write_nothing(gworld, answer, match) -> None:
    asked = ask(gworld)
    base = head(gworld.alpha_repo)
    with pytest.raises(AnswerError, match=match):
        answer_question(gworld.session, asked.proposal.id, answer)
    assert head(gworld.alpha_repo) == base and queue.get(asked.proposal.id).status == "pending"


def test_answer_refusals(gworld) -> None:
    with pytest.raises(UnknownProposal):
        answer_question(gworld.session, "nosuchid", "1")
    note = propose_note(gworld.session, "alpha", "inbox/x.md", "# x\n", "claude", "s")
    with pytest.raises(AnswerError, match="isn't a question"):
        answer_question(gworld.session, note.id, "1")
    asked = ask(gworld)
    answer_question(gworld.session, asked.proposal.id, "1")
    with pytest.raises(AnswerError, match="already answered"):
        answer_question(gworld.session, asked.proposal.id, "2")
    other = ask(gworld, question="Which cache?")
    from daedalus.core.apply import reject
    reject(other.proposal.id, "not now")
    with pytest.raises(AnswerError, match="rejected"):
        answer_question(gworld.session, other.proposal.id, "1")


def test_answer_names_a_second_decision_apart(gworld) -> None:
    write_note(gworld.alpha, "decisions/should-alpha-store-its-data-in-sqlite-or-postgres.md", "decision", "# old\n")
    git(gworld.alpha_repo, "add", "-A")
    git(gworld.alpha_repo, "commit", "-q", "-m", "an older decision of the same name")
    done = answer_question(gworld.session, ask(gworld).proposal.id, "1")
    assert done.decision_ref == "alpha:decisions/should-alpha-store-its-data-in-sqlite-or-postgres-2.md"


# Policy -------------------------------------------------------------------------------------

def test_user_action_may_update_questions_but_agents_cant_write_decisions(gworld) -> None:
    authorize(gworld.alpha / "questions" / "q.md", Op.USER_ACTION, gworld.session)
    with pytest.raises(PolicyError) as e:
        authorize(gworld.alpha / "decisions" / "d.md", Op.PROPOSE, gworld.session)
    assert e.value.rule == 5
    draft = note_text("decision", "# d\n", source="deepseek", related=["[[storage-engine]]"])
    with pytest.raises(PolicyError) as e:
        propose_note(gworld.session, "alpha", "decisions/d.md", draft, "deepseek", "s")
    assert e.value.rule == 5


# /promote --------------------------------------------------------------------------------------

def approved_draft(w, rel="inbox/fetch-store.md", note_type="component", vault="alpha", **fm_):
    root = w.alpha if vault == "alpha" else w.shared
    extra = {"applies_to": ["db"]} if note_type in ("practice", "lesson") else {}
    content = note_text(note_type, "# Fetch store\n", **{"status": "draft", "source": "deepseek",
                                                          "related": ["[[storage-engine]]"], **extra, **fm_})
    p = propose_note(w.session, vault, rel, content, "deepseek", "draft")
    apply(w.session, p.id)
    return root / rel


def test_promote_is_one_commit_deleting_and_adding(gworld) -> None:
    draft = approved_draft(gworld)
    repo, base = gworld.alpha_repo, head(gworld.alpha_repo)
    done = promote(gworld.session, "fetch-store", "components")
    assert commits_since(repo, base) == 1
    assert changed_files(repo) == [("A", "vault/components/fetch-store.md"), ("D", "vault/inbox/fetch-store.md")]
    assert not draft.exists()
    meta = fm(gworld.alpha / "components" / "fetch-store.md")
    assert meta["status"] == "accepted" and meta["source"] == "deepseek"  # source kept: acceptance is the status
    assert done.target_ref == "alpha:components/fetch-store.md"
    assert git(repo, "status", "--porcelain") == ""


def test_promote_into_shared_practices(gworld) -> None:
    approved_draft(gworld, rel="inbox/wal-mode.md", note_type="practice", vault="shared")
    done = promote(gworld.session, "shared:inbox/wal-mode.md")
    assert done.target_ref == "shared:practices/wal-mode.md"
    assert fm(gworld.shared / "practices" / "wal-mode.md")["status"] == "accepted"


def test_promote_keeps_everything_but_status_byte_for_byte(gworld) -> None:
    draft = approved_draft(gworld)
    before = draft.read_text()
    promote(gworld.session, "fetch-store")
    after = (gworld.alpha / "components" / "fetch-store.md").read_text()
    assert after == before.replace("status: draft", "status: accepted")


def promote_refusals(w):
    def mismatch():
        approved_draft(w)
        return lambda: promote(w.session, "fetch-store", "concepts"), PromoteError, "goes in components/"

    def exists():
        approved_draft(w, rel="inbox/usgs-fetcher.md")
        return lambda: promote(w.session, "inbox/usgs-fetcher"), PromoteError, "already exists"

    def invalid():
        approved_draft(w)  # a valid component draft, approved
        path = w.alpha / "inbox" / "fetch-store.md"
        text = path.read_text().replace("code: []\n", "")
        path.write_text(text)
        git(w.alpha_repo, "commit", "-q", "-am", "draft loses code")
        return lambda: promote(w.session, "fetch-store"), DraftInvalid, "missing key 'code'"

    def untyped():
        write_raw(w.alpha, "inbox/jotting.md", "# just a jotting\n")
        git(w.alpha_repo, "add", "-A")
        git(w.alpha_repo, "commit", "-q", "-m", "jotting")
        return lambda: promote(w.session, "jotting"), PromoteError, "has no type"

    def shared_wrong_type():
        approved_draft(w, rel="inbox/shared-concept.md", note_type="concept", vault="shared")
        return lambda: promote(w.session, "shared:inbox/shared-concept.md"), PromoteError, "promotes only practice"

    def not_inbox():
        return lambda: promote(w.session, "storage-engine"), PromoteError, "isn't an inbox/ draft"

    return {"type_mismatch": mismatch, "target_exists": exists, "invalid_for_target": invalid,
            "no_type": untyped, "shared_inbox_wrong_type": shared_wrong_type, "not_inbox": not_inbox}


@pytest.mark.parametrize("case", ["type_mismatch", "target_exists", "invalid_for_target", "no_type",
                                  "shared_inbox_wrong_type", "not_inbox"])
def test_promote_refusals_change_nothing(gworld, case) -> None:
    call, error, match = promote_refusals(gworld)[case]()
    before = gworld.snapshot()
    inbox = {p: p.read_bytes() for root in (gworld.alpha, gworld.shared) for p in (root / "inbox").glob("*.md")}
    with pytest.raises(error, match=match):
        call()
    assert gworld.snapshot() == before
    assert {p: p.read_bytes() for p in inbox} == inbox
    assert all(cs.status != "applied" for cs in queue.changesets())


# Multi-path apply ------------------------------------------------------------------------------

def test_apply_changes_leaves_other_staged_work_alone(gworld) -> None:
    repo = gworld.alpha_repo
    write_raw(repo, "notes.txt", "mine\n")
    git(repo, "add", "notes.txt")
    a = authorize(gworld.alpha / "concepts" / "a.md", Op.USER_ACTION, gworld.session).path
    b = authorize(gworld.alpha / "concepts" / "b.md", Op.USER_ACTION, gworld.session).path
    apply_changes(repo, [Change(a, "# a\n", None), Change(b, "# b\n", None)], "two at once")
    assert changed_files(repo) == [("A", "vault/concepts/a.md"), ("A", "vault/concepts/b.md")]
    assert git(repo, "status", "--porcelain") == "A  notes.txt\n"


def test_apply_changes_stale_check_writes_nothing(gworld) -> None:
    a = authorize(gworld.alpha / "concepts" / "a.md", Op.USER_ACTION, gworld.session).path
    ctx = gworld.alpha / "CONTEXT.md"
    base = head(gworld.alpha_repo)
    with pytest.raises(StaleProposal):
        apply_changes(gworld.alpha_repo, [Change(a, "# a\n", None), Change(ctx, "x\n", "0" * 64)], "s")
    assert not a.exists() and head(gworld.alpha_repo) == base and queue.changesets() == []
