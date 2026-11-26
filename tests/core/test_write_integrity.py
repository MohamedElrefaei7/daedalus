"""Test 13: a refused write leaves everything unchanged.

Every refusal path through a public write function (propose_note,
propose_edit, propose_lesson, write_log, apply, reject, and since commit 8
gate.propose_with_basis, answer_question, promote) is a row below. For
each row, all four hold:
  1. the right error is raised;
  2. every vault's digest is unchanged (all files except .git/ and ignored files);
  3. every repo's HEAD is unchanged;
  4. no queue record was created, and existing records changed status only
     where the row says so (a stale apply marks its proposal stale).

Rules 6 and 7 have no row: no public write function does WRITE_CODE or runs a
command yet. They are covered in test_policy.py and get rows when their
writers land.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import pytest
from conftest import DROP, git, note_text, write_note, write_raw, write_registry
from test_apply import install_hook

from daedalus.core import queue
from daedalus.core.accept import PromoteError, answer_question, promote
from daedalus.core.gate import GateRefused, propose_with_basis
from daedalus.core.apply import apply, reject
from daedalus.core.policy import PolicyError
from daedalus.core.queue import (
    CommitFailed, DraftInvalid, NotPending, ProposalError, ProposalTooLarge, StaleProposal, UnknownProposal,
)
from daedalus.core.registry import load_registry
from daedalus.core.session import Session
from daedalus.core.tools import propose_edit, propose_lesson, propose_note, write_log

HELLO = "# Hello\n"


@dataclass
class Case:
    call: Callable[[], object]
    error: type[Exception]
    rule: int | None = None
    status_changes: dict[str, str] = field(default_factory=dict)


def note(w, vault: str, rel: str, content: str = HELLO) -> Callable[[], object]:
    return lambda: propose_note(w.session, vault, rel, content, "claude", "s")


def proposed(w, rel: str = "inbox/hello.md") -> str:
    return propose_note(w.session, "alpha", rel, HELLO, "claude", "s").id


# Each builder does its setup, then returns the refused call to check.
def rule1_traversal_into_repo(w):
    return Case(note(w, "alpha", "inbox/../../README.md"), PolicyError, 1)


def rule1_traversal_outside(w):
    return Case(note(w, "alpha", "inbox/../../../elsewhere.md"), PolicyError, 1)


def rule1_unpulled_vault(w):
    return Case(note(w, "gamma", "inbox/x.md"), PolicyError, 1)


def rule1_unregistered_vault(w):
    return Case(note(w, "nope", "inbox/x.md"), PolicyError, 1)


def rule1_symlink_escape(w):
    (w.root / "outside").mkdir()
    (w.alpha / "inbox" / "out").symlink_to(w.root / "outside")
    return Case(note(w, "alpha", "inbox/out/x.md"), PolicyError, 1)


def rule2_shared_practices(w):
    return Case(note(w, "shared", "practices/x.md", note_text("practice")), PolicyError, 2)


def rule2_symlink_into_shared(w):
    (w.alpha / "inbox" / "link").symlink_to(w.shared / "practices")
    return Case(note(w, "alpha", "inbox/link/x.md"), PolicyError, 2)


def rule2_edit_shared_agents(w):
    return Case(lambda: propose_edit(w.session, "shared", "AGENTS.md", "# mine\n", "claude", "s"), PolicyError, 2)


def rule3_pulled_vault(w):
    return Case(note(w, "beta", "inbox/x.md"), PolicyError, 3)


def rule3_reauth_on_apply(w):
    pid = proposed(w)
    later = Session(w.registry, active="beta", pulled=("alpha",))
    return Case(lambda: apply(later, pid), PolicyError, 3)


def rule1_reauth_unregistered_on_apply(w):
    pid = proposed(w)
    registry = load_registry(write_registry(w.root / "later.toml", w.shared, {"beta": w.beta}))
    return Case(lambda: apply(Session(registry, active="beta"), pid), PolicyError, 1)


def rule2_symlink_swap_before_apply(w):
    pid = proposed(w)
    (w.alpha / "inbox").rename(w.alpha / "inbox-real")
    (w.alpha / "inbox").symlink_to(w.shared)
    return Case(lambda: apply(w.session, pid), PolicyError, 2)


def rule4_obsidian(w):
    return Case(note(w, "alpha", ".obsidian/x.md"), PolicyError, 4)


def rule4_obsidian_case_folded(w):
    return Case(note(w, "alpha", ".OBSIDIAN/x.md"), PolicyError, 4)


def rule4_git(w):
    return Case(note(w, "alpha", "../.git/x.md"), PolicyError, 4)


def rule5_propose_decision(w):
    return Case(note(w, "alpha", "decisions/x.md", note_text("decision")), PolicyError, 5)


def rule5_edit_agents(w):
    return Case(lambda: propose_edit(w.session, "alpha", "AGENTS.md", "# mine\n", "claude", "s"), PolicyError, 5)


def rule5_edit_template(w):
    return Case(lambda: propose_edit(w.session, "alpha", "templates/decision.md", "x\n", "claude", "s"),
                PolicyError, 5)


def rule5_propose_into_logs(w):
    return Case(note(w, "alpha", "logs/x.md"), PolicyError, 5)


def rule5_log_exists(w):
    return Case(lambda: write_log(w.session, "2026-11-25-run-1", "again\n", "deepseek"), PolicyError, 5)


def stale_edit(w):
    pid = propose_edit(w.session, "alpha", "CONTEXT.md", "# CONTEXT\n\nagent\n", "deepseek", "s").id
    (w.alpha / "CONTEXT.md").write_text("# CONTEXT\n\nhuman edit\n")
    return Case(lambda: apply(w.session, pid), StaleProposal, status_changes={pid: "stale"})


def stale_create_collision(w):
    pid = proposed(w)
    write_raw(w.alpha, "inbox/hello.md", "mine\n")
    return Case(lambda: apply(w.session, pid), StaleProposal, status_changes={pid: "stale"})


def double_apply(w):
    pid = proposed(w)
    apply(w.session, pid)
    return Case(lambda: apply(w.session, pid), NotPending)


def apply_rejected(w):
    pid = proposed(w)
    reject(pid, "no")
    return Case(lambda: apply(w.session, pid), NotPending)


def commit_failure(w):
    pid = proposed(w)
    install_hook(w.alpha_repo)
    return Case(lambda: apply(w.session, pid), CommitFailed, status_changes={pid: "failed"})


def log_commit_failure(w):
    install_hook(w.alpha_repo)
    return Case(lambda: write_log(w.session, "run-9", "body\n", "deepseek"), CommitFailed)


def oversized(w):
    return Case(note(w, "alpha", "inbox/big.md", "a" * (queue.MAX_CONTENT_BYTES + 1)), ProposalTooLarge)


def invalid_draft(w):
    return Case(note(w, "alpha", "inbox/d.md", note_text("decision", related=DROP)), DraftInvalid)


def invalid_lesson_draft(w):
    return Case(lambda: propose_lesson(w.session, "l.md", note_text("lesson", applies_to=DROP), "claude", "s"),
                DraftInvalid)


def source_mismatch_on_create(w):
    draft = note_text("concept", "# Mine, honest\n", source="human")
    return Case(lambda: propose_note(w.session, "alpha", "inbox/c.md", draft, "deepseek", "s"), DraftInvalid)


def agent_edit_changes_source(w):
    write_raw(w.alpha, "inbox/draft.md", note_text("concept", "# Draft\n", source="human"))
    edited = note_text("concept", "# Draft\n\nmore\n", source="claude")
    return Case(lambda: propose_edit(w.session, "alpha", "inbox/draft.md", edited, "claude", "s"), DraftInvalid)


def link_only_to_context(w):
    draft = note_text("concept", "# Hello\n", source="claude", related=["[[CONTEXT]]"])
    return Case(lambda: propose_note(w.session, "alpha", "inbox/c.md", draft, "claude", "s"), DraftInvalid)


def create_over_existing(w):
    return Case(note(w, "alpha", "CONTEXT.md"), ProposalError)


def edit_missing(w):
    return Case(lambda: propose_edit(w.session, "alpha", "inbox/none.md", HELLO, "claude", "s"), ProposalError)


def forged_trailer_summary(w):
    return Case(lambda: propose_note(w.session, "alpha", "inbox/x.md", HELLO, "claude", "s\n\nDaedalus-Change: x"),
                ProposalError)


def reject_without_reason(w):
    pid = proposed(w)
    return Case(lambda: reject(pid, " "), ProposalError)


def non_precedent_basis_in_proposal(w):
    write_note(w.alpha, "decisions/maybe-postgres.md", "decision", "# Maybe\n", status="proposed")
    git(w.alpha_repo, "add", "-A")
    git(w.alpha_repo, "commit", "-q", "-m", "a proposed decision")
    draft = note_text("component", "# Store\n", source="deepseek", related=["[[maybe-postgres]]"])
    return Case(lambda: propose_with_basis(w.session, "data_source", "Postgres", "[[maybe-postgres]]", "alpha",
                                           "inbox/store.md", draft, "deepseek", "s"), GateRefused)


def promote_type_mismatch(w):
    write_raw(w.alpha, "inbox/store.md", note_text("component", "# Store\n", status="draft",
                                                   related=["[[storage-engine]]"]))
    git(w.alpha_repo, "add", "-A")
    git(w.alpha_repo, "commit", "-q", "-m", "a component draft")
    return Case(lambda: promote(w.session, "alpha:inbox/store.md", "decisions"), PromoteError)


def answer_unknown_question(w):
    return Case(lambda: answer_question(w.session, "0000000000zzzz", "1"), UnknownProposal)


CASES = [
    rule1_traversal_into_repo, rule1_traversal_outside, rule1_unpulled_vault, rule1_unregistered_vault,
    rule1_symlink_escape, rule1_reauth_unregistered_on_apply,
    rule2_shared_practices, rule2_symlink_into_shared, rule2_edit_shared_agents, rule2_symlink_swap_before_apply,
    rule3_pulled_vault, rule3_reauth_on_apply,
    rule4_obsidian, rule4_obsidian_case_folded, rule4_git,
    rule5_propose_decision, rule5_edit_agents, rule5_edit_template, rule5_propose_into_logs, rule5_log_exists,
    stale_edit, stale_create_collision, double_apply, apply_rejected, commit_failure, log_commit_failure,
    oversized, invalid_draft, invalid_lesson_draft, source_mismatch_on_create, agent_edit_changes_source,
    link_only_to_context,
    create_over_existing, edit_missing, forged_trailer_summary,
    reject_without_reason,
    non_precedent_basis_in_proposal, promote_type_mismatch, answer_unknown_question,
]


@pytest.mark.parametrize("build", CASES, ids=[c.__name__ for c in CASES])
def test_refused_write_leaves_everything_unchanged(gworld, build) -> None:
    case = build(gworld)
    before = gworld.snapshot()

    # 1. The right error.
    with pytest.raises(case.error) as exc:
        case.call()
    if case.rule is not None:
        assert isinstance(exc.value, PolicyError) and exc.value.rule == case.rule, exc.value

    after = gworld.snapshot()
    # 2. Every vault's digest.
    assert after["digests"] == before["digests"], "a refused write changed a vault"
    # 3. Every repo's HEAD.
    assert after["heads"] == before["heads"], "a refused write moved a HEAD"
    # 4. The queue: no new record, and only the expected status changes.
    assert after["queue"] == {**before["queue"], **case.status_changes}
