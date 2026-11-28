"""Approving a plan (commit 12): the plan and its new decisions, one commit, re-validated, all or nothing."""

from __future__ import annotations

import pytest
from conftest import git, head, write_note
from test_plans import BODY, CAP, good, plan_world

from daedalus.core import apply as apply_mod, gate, notes, plans, queue, tools
from daedalus.core.apply import apply, reject
from daedalus.core.errors import PlanApprovalOnly
from daedalus.core.plans import PlanError, PlanInvalid

DAY = __import__("datetime").date(2026, 11, 28)


@pytest.fixture
def pw(gworld):
    return plan_world(gworld)


def two_new() -> dict:
    fm = good()
    fm["decisions"].append({"category": "schema", "choice": "results are JSON lines", "basis": "new"})
    return fm


def submit(w, fm=None, slug="mathx"):
    return plans.submit(w.session, "claude", slug, fm or two_new(), BODY, session_cap_usd=CAP).proposal


def approve(w, pid):
    return plans.approve_plan(w.session, pid, session_cap_usd=CAP, today=DAY)


def fm_of(path):
    raw, _ = notes.split_frontmatter(path.read_text())
    return notes.load_frontmatter(raw)[0]


# What approval writes -----------------------------------------------------------------------

def test_approval_writes_every_new_decision(pw) -> None:
    """Break 1 target: approval that skips the decision notes fails here."""
    p = submit(pw)
    done = approve(pw, p.id)
    stems = ["interface-python-m-scratch2-mathx-add-2-3", "schema-results-are-json-lines"]
    assert done.decision_refs == tuple(f"alpha:decisions/{s}.md" for s in stems)
    for stem, (category, choice) in zip(stems, [("interface", "python -m scratch2.mathx add 2 3"),
                                                 ("schema", "results are JSON lines")]):
        path = pw.alpha / "decisions" / f"{stem}.md"
        assert path.is_file(), f"approval didn't write {stem}"
        fm = fm_of(path)
        assert (fm["type"], fm["status"], fm["source"], fm["category"], fm["created"]) == \
            ("decision", "accepted", "claude", category, "2026-11-28")
        assert fm["related"] == ["[[mathx-v1]]", "[[mathx-cli-spec]]"]
        body = notes.split_frontmatter(path.read_text())[1]
        assert f"New in [[mathx-v1]], approved on 2026-11-28" in body and choice in body
        assert gate.check_basis(pw.session, category, choice, f"[[{stem}]]").verdict == "OK"  # real precedent now
    plan = fm_of(pw.alpha / "plans" / "mathx-v1.md")
    assert plan["status"] == "accepted"
    assert [d.get("note") for d in plan["decisions"]] == [None, f"[[{stems[0]}]]", f"[[{stems[1]}]]"]
    assert plan["related"] == ["[[mathx-cli-spec]]", f"[[{stems[0]}]]", f"[[{stems[1]}]]"]
    assert queue.get(p.id).status == "applied" and queue.get(p.id).commit_sha == done.commit_sha


def test_decisions_follow_the_vaults_decision_template(pw) -> None:
    """Filled from the vault's own templates/decision.md, so they look hand-written."""
    template = (pw.alpha / "templates" / "decision.md").read_text()
    template = template.replace("## Consequences", "## Consequences\n\n## Revisit when")
    (pw.alpha / "templates" / "decision.md").write_text(template)
    approve(pw, submit(pw).id)
    text = (pw.alpha / "decisions" / "schema-results-are-json-lines.md").read_text()
    assert [ln for ln in text.splitlines() if ln.startswith("## ")] == \
        ["## Context", "## Decision", "## Consequences", "## Revisit when"]
    assert "# results are JSON lines" in text and "{{" not in text
    t_keys = list(notes.load_frontmatter(notes.split_frontmatter(template)[0])[0])
    assert list(fm_of(pw.alpha / "decisions" / "schema-results-are-json-lines.md"))[:len(t_keys)] == t_keys


def test_plan_and_decisions_land_in_one_commit(pw) -> None:
    """Break 2 target: decisions in a second commit fail here."""
    before = head(pw.alpha_repo)
    p = submit(pw)
    done = approve(pw, p.id)
    assert git(pw.alpha_repo, "rev-list", "--count", f"{before}..HEAD").strip() == "1"
    assert head(pw.alpha_repo) == done.commit_sha
    files = git(pw.alpha_repo, "show", "--name-only", "--format=", "HEAD").split()
    assert sorted(files) == ["vault/decisions/interface-python-m-scratch2-mathx-add-2-3.md",
                             "vault/decisions/schema-results-are-json-lines.md", "vault/plans/mathx-v1.md"]
    message = git(pw.alpha_repo, "log", "-1", "--format=%B").strip().splitlines()
    assert message[0] == "agent(claude): approve plan mathx-v1"
    assert message[-1] == f"Daedalus-Change: {p.id}"


def test_a_plan_with_no_new_decisions_writes_only_the_plan(pw) -> None:
    fm = good()
    fm["decisions"] = fm["decisions"][:1]
    done = approve(pw, submit(pw, fm).id)
    assert done.decision_refs == ()
    assert git(pw.alpha_repo, "show", "--name-only", "--format=", "HEAD").split() == ["vault/plans/mathx-v1.md"]


def test_a_failed_approval_leaves_nothing_behind(pw, monkeypatch) -> None:
    """Break 3 target: a failure writing the second decision note, with no rollback, fails here."""
    p = submit(pw)
    before = pw.snapshot()
    real = apply_mod.atomic_write_at
    calls = []

    def flaky(fd, name, data, mode=None):
        calls.append(name)
        if len(calls) == 3:  # the plan, the first decision, then the second decision
            raise OSError("injected: disk full")
        return real(fd, name, data, mode)

    monkeypatch.setattr(apply_mod, "atomic_write_at", flaky)
    with pytest.raises(OSError, match="injected"):
        approve(pw, p.id)
    after = pw.snapshot()
    assert after["digests"] == before["digests"] and after["heads"] == before["heads"]
    assert not (pw.alpha / "plans" / "mathx-v1.md").exists()
    assert not list((pw.alpha / "decisions").glob("interface-*")) + list((pw.alpha / "decisions").glob("schema-*"))
    assert git(pw.alpha_repo, "status", "--porcelain", "--", "vault/plans", "vault/decisions/schema-*").strip() == ""
    assert git(pw.alpha_repo, "diff", "--cached", "--name-only").strip() == ""
    assert queue.get(p.id).status == "failed" and "injected" in queue.get(p.id).status_reason


def test_a_crash_after_the_commit_reconciles_to_applied(pw, monkeypatch) -> None:
    """The proposal and its change set share an id, so both settle against the one commit."""
    p = submit(pw)
    real = queue.transition

    def dies(change_id, frm, to, **kw):
        if to == "applied" and change_id == p.id:
            raise KeyboardInterrupt  # the process stops between the commit and the last status write
        return real(change_id, frm, to, **kw)

    monkeypatch.setattr(queue, "transition", dies)
    with pytest.raises(KeyboardInterrupt):
        approve(pw, p.id)
    monkeypatch.setattr(queue, "transition", real)
    import sqlite3

    raw = sqlite3.connect(queue.queue_path())  # a plain read: queue.connect() would reconcile first
    assert raw.execute("SELECT status FROM proposals WHERE id = ?", (p.id,)).fetchone() == ("applying",)
    raw.close()
    rec = queue.get(p.id)  # the next queue open reconciles it
    assert (rec.status, rec.commit_sha) == ("applied", head(pw.alpha_repo))
    assert (pw.alpha / "plans" / "mathx-v1.md").is_file()


# Refusals -----------------------------------------------------------------------------------

def test_approval_revalidates(pw) -> None:
    """Break 4 target: a precedent un-accepted after submission must refuse the approval."""
    p = submit(pw)
    path = pw.alpha / "decisions" / "storage-engine.md"
    path.write_text(path.read_text().replace("status: accepted", "status: proposed"))
    before = pw.snapshot()
    with pytest.raises(PlanInvalid) as exc:
        approve(pw, p.id)
    assert any(e.startswith("decisions[1].basis:") and "'proposed', not accepted" in e for e in exc.value.errors)
    assert "stays pending" in str(exc.value)
    assert pw.snapshot() == before and queue.get(p.id).status == "pending"


def test_revalidation_reports_every_error_at_once(pw) -> None:
    p = submit(pw)
    (pw.alpha / "decisions" / "storage-engine.md").unlink()
    (pw.alpha_repo / "daedalus.toml").write_text('[sandbox]\nallow = ["ruff"]\n')
    with pytest.raises(PlanInvalid) as exc:
        approve(pw, p.id)
    fields = sorted({e.split(":")[0] for e in exc.value.errors})
    assert fields == ["decisions[1].basis", "tasks[t1].done_when", "tasks[t2].done_when"], exc.value.errors


@pytest.mark.parametrize("how", ["superseded", "rejected", "older", "applied", "not_a_plan", "unknown"])
def test_only_the_latest_pending_plan_can_be_approved(pw, how) -> None:
    v1 = submit(pw)
    target = v1.id
    if how == "superseded":
        plans.revise(pw.session, "claude", "mathx", two_new(), BODY, session_cap_usd=CAP)
        error, words = queue.NotPending, "superseded, not pending"
    elif how == "rejected":
        reject(v1.id, "not this way")
        error, words = queue.NotPending, r"rejected \(not this way\)"
    elif how == "older":
        reject(v1.id, "redo")
        plans.revise(pw.session, "claude", "mathx", two_new(), BODY, session_cap_usd=CAP)
        queue.transition(v1.id, "rejected", "pending")  # a hand-made oddity: an older version still pending
        error, words = PlanError, "isn't the latest version"
    elif how == "applied":
        approve(pw, v1.id)
        error, words = queue.NotPending, "already approved"
    elif how == "not_a_plan":
        target = tools.propose_note(pw.session, "alpha", "inbox/x.md", "# x\n\n[[usgs-fetcher]]\n", "claude", "x").id
        error, words = PlanError, "not a plan"
    else:
        target, error, words = "0000000000zzzz", queue.UnknownProposal, None
    before = pw.snapshot()
    with pytest.raises(error, match=words):
        approve(pw, target)
    assert pw.snapshot() == before


def test_generic_apply_still_refuses_a_plan(pw) -> None:
    """Break 6 target (with commit 11's test): plans go only through approve_plan."""
    p = submit(pw)
    before = pw.snapshot()
    with pytest.raises(PlanApprovalOnly, match="a plan is approved only by plan approval"):
        apply(pw.session, p.id)
    assert pw.snapshot() == before


def test_claude_cant_set_a_decisions_note(pw) -> None:
    fm = good()
    fm["decisions"][1]["note"] = "[[my-own-decision]]"
    with pytest.raises(PlanInvalid) as exc:
        submit(pw, fm)
    assert exc.value.errors == ["decisions[2].note: set by the server when Mohamed approves the plan, never by "
                                "you; leave it out"]


# Names ----------------------------------------------------------------------------------------

def test_stems_are_capped_at_48_characters_at_a_word() -> None:
    stem = plans.decision_stem("data_source", "fetch quakes from the USGS GeoJSON feed every five minutes always")
    assert stem == "data-source-fetch-quakes-from-the-usgs-geojson" and len(stem) <= 48
    assert plans.decision_stem("interface", "x" * 80) == "interface"


def test_a_stem_any_scope_note_uses_gets_a_suffix(pw) -> None:
    """Ambiguous links are refused, so a stem must match no note in the active, pulled, or shared vaults."""
    write_note(pw.shared, "practices/schema-results-are-json-lines.md", "practice", "# x\n", applies_to=["x"])
    write_note(pw.beta, "concepts/interface-python-m-scratch2-mathx-add-2-3.md", "concept", "# x\n")
    write_note(pw.alpha, "decisions/interface-python-m-scratch2-mathx-add-2-3-2.md", "decision", "# x\n")
    fm = two_new()
    fm["decisions"].append({"category": "schema", "choice": "results are JSON lines!", "basis": "new"})
    done = approve(pw, submit(pw, fm).id)
    assert done.decision_refs == ("alpha:decisions/interface-python-m-scratch2-mathx-add-2-3-3.md",
                                  "alpha:decisions/schema-results-are-json-lines-2.md",
                                  "alpha:decisions/schema-results-are-json-lines-3.md")
    for ref in done.decision_refs:
        tools.resolve_ref(pw.session, f"[[{ref.split('/')[-1][:-3]}]]")  # each resolves to exactly one note
