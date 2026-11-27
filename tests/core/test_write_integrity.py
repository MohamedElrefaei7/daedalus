"""Test 13: a refused write leaves everything unchanged.

Every refusal path through a public write function (propose_note,
propose_edit, propose_lesson, write_log, apply, reject, and since commit 8
gate.propose_with_basis, answer_question, promote, and since commit 9 ingest)
is a row below. For
each row, all of these hold:
  1. the right error is raised;
  2. every vault's digest is unchanged (all files except .git/ and ignored files);
  3. every repo's HEAD is unchanged;
  4. no queue record was created, and existing records changed status only
     where the row says so (a stale apply marks its proposal stale).
  5. any extra check the row carries (for commands: no process was spawned).

Since commit 10a, sandbox.runner.run has rows for rule 7. A refused command
must also spawn no process: each row's injected executor records every spawn
(the preflight, `uv sync`, `uv python dir`, the command) and must stay empty.
Since commit 10b, edit_code has rows for rules 1, 4, and 6, and for its
exactly-once rule. The snapshot covers the checkout's digest (and the
worktree's) as well as every vault's.
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
from daedalus.core.ingest import IngestError, ingest
from daedalus.core.apply import apply, reject
from daedalus.core.policy import PolicyError
from daedalus.core.queue import (
    CommitFailed, DraftInvalid, NotPending, ProposalError, ProposalTooLarge, StaleProposal, UnknownProposal,
)
from daedalus.core.registry import load_registry
from daedalus.core.session import Session
from daedalus.core.tools import CodeEditError, edit_code, propose_edit, propose_lesson, propose_note, write_log

HELLO = "# Hello\n"


@dataclass
class Case:
    call: Callable[[], object]
    error: type[Exception]
    rule: int | None = None
    status_changes: dict[str, str] = field(default_factory=dict)
    after: Callable[[], None] | None = None  # an extra check once the call is refused


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


def ambiguous_link(w):
    write_note(w.alpha, "questions/pick-db.md", "question", "# Pick a database?\n", options=["a", "b"])
    write_note(w.alpha, "decisions/pick-db.md", "decision", "# Pick a database\n")
    git(w.alpha_repo, "add", "-A")
    git(w.alpha_repo, "commit", "-q", "-m", "a question and a decision with one name")
    draft = note_text("concept", "# Hello\n", source="claude", related=["[[pick-db]]"])
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


def _docs(w):
    d = w.root / "docs"  # outside every vault
    d.mkdir(exist_ok=True)
    return d


def add_oversized(w):
    import doc_fixtures

    src = doc_fixtures.make_big(_docs(w) / "huge.pdf", 26 * 1024 * 1024)
    return Case(lambda: ingest(w.session, src), IngestError)


def add_corrupt(w):
    import doc_fixtures

    src = doc_fixtures.make_corrupt_pdf(_docs(w) / "broken.pdf")
    return Case(lambda: ingest(w.session, src), IngestError)


def add_scanned(w):
    import doc_fixtures

    src = doc_fixtures.make_scanned_pdf(_docs(w) / "scan.pdf")
    return Case(lambda: ingest(w.session, src), IngestError)


def add_duplicate(w):
    import doc_fixtures

    src = doc_fixtures.make_docx(_docs(w) / "design.docx")
    ingest(w.session, src)  # the first add is setup
    return Case(lambda: ingest(w.session, src), IngestError)


class NoSpawn:
    """An executor that records every spawn it's asked for, and runs nothing."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv, **_kwargs):
        self.calls.append(list(argv))
        raise AssertionError(f"a refused command spawned {argv}")

    def check(self) -> None:
        assert self.calls == [], f"a refused command spawned processes: {self.calls}"


def _command(w, argv):
    from daedalus.sandbox import runner, venv

    (w.alpha_repo / "daedalus.toml").write_text('[sandbox]\nallow = ["pytest", "ruff"]\n')
    (w.alpha_repo / "uv.lock").write_text("version = 1\n")
    built = venv.venv_path("alpha", w.alpha_repo)  # built, with pytest but not ruff
    (built / "bin").mkdir(parents=True)
    (built / "bin" / "pytest").write_text("#!/bin/sh\n")
    (built / "bin" / "pytest").chmod(0o755)
    (built / venv.READY).write_text("x\n")
    spawns = NoSpawn()
    return Case(lambda: runner.run(w.session, argv, w.worktree, executor=spawns), PolicyError, rule=7,
                after=spawns.check)


def rule7_slash_in_program(w):
    return _command(w, ["/tmp/evil/pytest", "-q"])


def rule7_program_not_allowed(w):
    return _command(w, ["rm", "-rf", "vault"])


def rule7_allowed_but_missing_from_venv(w):
    return _command(w, ["ruff", "check"])


def rule7_empty_argv(w):
    return _command(w, [])


def rule7_nul_byte(w):
    return _command(w, ["pytest", "-k", "a\0b"])


def _edit(w, rel, old="print('hi')", new="print('bye')", session=None):
    return lambda: edit_code(session or w.session, rel, old, new)


def rule6_edit_in_checkout(w):
    write_raw(w.alpha_repo, "src/app.py", "print('hi')\n")
    return Case(_edit(w, str(w.alpha_repo / "src" / "app.py")), PolicyError, rule=6)


def rule6_edit_into_checkout_by_dotdot(w):
    write_raw(w.alpha_repo, "src/app.py", "print('hi')\n")
    return Case(_edit(w, "../alpha/src/app.py"), PolicyError, rule=6)


def rule4_edit_worktree_vault(w):
    return Case(_edit(w, "vault/inbox/copy.md", old="a vault copy", new="changed"), PolicyError, rule=4)


def rule4_edit_worktree_git(w):
    return Case(_edit(w, ".git/config", old="x", new="y"), PolicyError, rule=4)


def rule1_dotdot_escape_from_worktree(w):
    write_raw(w.root, "outside.py", "print('hi')\n")
    return Case(_edit(w, "../outside.py"), PolicyError, rule=1)


def rule6_symlink_in_worktree_to_checkout(w):
    write_raw(w.alpha_repo, "src/app.py", "print('hi')\n")
    (w.worktree / "checkout").symlink_to(w.alpha_repo)
    return Case(_edit(w, "checkout/src/app.py"), PolicyError, rule=6)


def rule6_edit_with_no_worktree(w):
    session = Session(w.registry, "alpha", pulled=("beta",))
    return Case(_edit(w, "src/main.py", session=session), PolicyError, rule=6)


def edit_old_matches_zero_times(w):
    return Case(_edit(w, "src/main.py", old="print('nope')"), CodeEditError)


def edit_old_matches_twice(w):
    write_raw(w.worktree, "src/twice.py", "x = 1\nx = 1\n")
    return Case(_edit(w, "src/twice.py", old="x = 1", new="x = 2"), CodeEditError)


CASES = [
    rule1_traversal_into_repo, rule1_traversal_outside, rule1_unpulled_vault, rule1_unregistered_vault,
    rule1_symlink_escape, rule1_reauth_unregistered_on_apply,
    rule2_shared_practices, rule2_symlink_into_shared, rule2_edit_shared_agents, rule2_symlink_swap_before_apply,
    rule3_pulled_vault, rule3_reauth_on_apply,
    rule4_obsidian, rule4_obsidian_case_folded, rule4_git,
    rule5_propose_decision, rule5_edit_agents, rule5_edit_template, rule5_propose_into_logs, rule5_log_exists,
    stale_edit, stale_create_collision, double_apply, apply_rejected, commit_failure, log_commit_failure,
    oversized, invalid_draft, invalid_lesson_draft, source_mismatch_on_create, agent_edit_changes_source,
    link_only_to_context, ambiguous_link,
    create_over_existing, edit_missing, forged_trailer_summary,
    reject_without_reason,
    non_precedent_basis_in_proposal, promote_type_mismatch, answer_unknown_question,
    add_oversized, add_corrupt, add_scanned, add_duplicate,
    rule7_slash_in_program, rule7_program_not_allowed, rule7_allowed_but_missing_from_venv, rule7_empty_argv,
    rule7_nul_byte,
    rule6_edit_in_checkout, rule6_edit_into_checkout_by_dotdot, rule4_edit_worktree_vault, rule4_edit_worktree_git,
    rule1_dotdot_escape_from_worktree, rule6_symlink_in_worktree_to_checkout, rule6_edit_with_no_worktree,
    edit_old_matches_zero_times, edit_old_matches_twice,
]


@pytest.fixture(autouse=True)
def _sandbox_state(tmp_path, monkeypatch):
    """Command rows build their venv under tmp_path, and start with no cached preflight."""
    from daedalus.sandbox import runner, venv

    monkeypatch.setattr(venv, "VENV_ROOT", tmp_path / "venvs")
    monkeypatch.setattr(runner, "_preflight", None)
    monkeypatch.setattr(runner, "_uv_python_dir", None)


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
    # 5. Anything else the row checks (for commands: no process was spawned).
    if case.after is not None:
        case.after()
