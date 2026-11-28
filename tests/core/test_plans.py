"""Plans from Claude (commit 11): validation, the queue-only rule, versions and superseding."""

from __future__ import annotations

import copy

import pytest
from conftest import write_note, write_raw

from daedalus.core import plans, queue
from daedalus.core.apply import apply, reject
from daedalus.core.errors import PlanApprovalOnly
from daedalus.core.plans import PlanError, PlanInvalid, validate_plan

CAP = 1.00


def plan_world(w):
    """gworld plus what a plan needs: daedalus.toml, code, a spec, and notes to cite (good and bad)."""
    write_raw(w.alpha_repo, "daedalus.toml", '[sandbox]\nallow = ["pytest"]\n')
    write_raw(w.alpha_repo, "src/mathx.py", "def add(a, b):\n    return a + b\n")
    write_raw(w.alpha_repo, "tests/test_mathx.py", "def test_add():\n    pass\n")
    write_note(w.alpha, "concepts/mathx-cli-spec.md", "concept", "# mathx CLI spec\n", related=["[[usgs-fetcher]]"])
    write_note(w.alpha, "decisions/maybe-postgres.md", "decision", "# Maybe Postgres\n", status="proposed")
    write_note(w.beta, "decisions/beta-cli.md", "decision", "# Beta's CLI\n")
    write_note(w.alpha, "decisions/cache.md", "decision", "# Cache\n")
    write_note(w.alpha, "components/cache.md", "component", "# Cache component\n", code=["src/cache.py"])
    return w


@pytest.fixture
def pw(gworld):
    return plan_world(gworld)


def good() -> dict:
    return {
        "spec": "[[mathx-cli-spec]]",
        "decisions": [
            {"category": "data_source", "choice": "keep results in SQLite", "basis": "[[storage-engine]]"},
            {"category": "interface", "choice": "python -m scratch2.mathx add 2 3", "basis": "new"},
        ],
        "tasks": [
            {"id": "t1", "summary": "add multiply(a, b)", "files": ["src/mathx.py", "tests/test_mathx.py"],
             "red_test": ["tests/test_mathx.py::test_multiply"],
             "done_when": "pytest -q tests/test_mathx.py", "max_iterations": 3, "budget_usd": 0.25},
            {"id": "t2", "summary": "a tiny CLI", "files": ["src/__main__.py", "tests/test_cli.py"],
             "red_test": ["tests/test_cli.py::test_add_prints_5"],
             "done_when": "pytest -q", "max_iterations": 4, "budget_usd": 0.30, "protected": ["tests/test_mathx.py"]},
        ],
    }


BODY = "multiply goes beside add; the CLI is argparse-free on purpose."


def submit_errors(w, fm, slug="mathx") -> list[str]:
    before = queue.statuses()
    with pytest.raises(PlanInvalid) as exc:
        plans.submit(w.session, "claude", slug, fm, BODY, session_cap_usd=CAP)
    assert queue.statuses() == before, "an invalid plan was queued"
    return exc.value.errors


def only_error(w, fm) -> str:
    errors = submit_errors(w, fm)
    assert len(errors) == 1, errors
    return errors[0]


def edit(change):
    fm = good()
    change(fm)
    return fm


# The valid plan -------------------------------------------------------------------------------

def test_a_good_plan_validates_and_is_queued_as_v1(pw) -> None:
    r = plans.submit(pw.session, "claude", "mathx", good(), BODY, session_cap_usd=CAP)
    p = queue.get(r.proposal.id)
    assert (p.vault, p.rel_path, p.kind, p.source, p.status) == ("alpha", "plans/mathx-v1.md", "create", "claude",
                                                                 "pending")
    assert validate_plan(pw.session, p.content, session_cap_usd=CAP) == []
    fm = plans._fm_of(p.content)
    assert (fm["type"], fm["status"], fm["source"]) == ("plan", "proposed", "claude")
    assert fm["related"] == ["[[mathx-cli-spec]]"]  # the spec is linked, so the plan shows in its backlinks
    assert plans.BODY_HEADING in p.content and BODY in p.content


# 1. One test per rule -------------------------------------------------------------------------

def test_bad_category(pw) -> None:
    e = only_error(pw, edit(lambda fm: fm["decisions"][0].update(category="storage")))
    assert e.startswith("decisions[1].category:") and "'storage'" in e


def test_basis_citing_a_proposed_note(pw) -> None:
    """Break 1 target: accepting a proposed note as basis must fail here."""
    e = only_error(pw, edit(lambda fm: fm["decisions"][0].update(basis="[[maybe-postgres]]")))
    assert e.startswith("decisions[1].basis:") and "'proposed', not accepted" in e


def test_basis_citing_a_pulled_projects_note(pw) -> None:
    e = only_error(pw, edit(lambda fm: fm["decisions"][0].update(basis="[[beta-cli]]")))
    assert e.startswith("decisions[1].basis:") and "pulled project 'beta'" in e


def test_ambiguous_basis(pw) -> None:
    e = only_error(pw, edit(lambda fm: fm["decisions"][1].update(basis="[[cache]]")))
    assert e.startswith("decisions[2].basis:") and "ambiguous" in e and "[[decisions/cache]]" in e


def test_superseded_and_missing_bases(pw) -> None:
    write_note(pw.alpha, "decisions/old-db.md", "decision", "# Old\n", status="superseded")
    errors = submit_errors(pw, edit(lambda fm: (fm["decisions"][0].update(basis="[[old-db]]"),
                                                fm["decisions"][1].update(basis="[[no-such-note]]"))))
    assert len(errors) == 2, errors
    assert errors[0].startswith("decisions[1].basis:") and "'superseded'" in errors[0]
    assert errors[1].startswith("decisions[2].basis:") and "matches no note" in errors[1]


def test_duplicate_task_ids(pw) -> None:
    e = only_error(pw, edit(lambda fm: fm["tasks"][1].update(id="t1")))
    assert e.startswith("tasks[t1].id:") and "duplicate" in e


def test_a_file_under_vault(pw) -> None:
    e = only_error(pw, edit(lambda fm: fm["tasks"][0]["files"].append("vault/plans/x.md")))
    assert e.startswith("tasks[t1].files:") and "under vault/" in e


def test_a_dotdot_escape(pw) -> None:
    e = only_error(pw, edit(lambda fm: fm["tasks"][1]["files"].append("src/../../elsewhere.py")))
    assert e.startswith("tasks[t2].files:") and "escapes the repo" in e


def test_git_and_symlink_escapes(pw) -> None:
    (pw.alpha_repo / "outside").symlink_to(pw.root)
    errors = submit_errors(pw, edit(lambda fm: fm["tasks"][0]["files"].extend([".git/hooks/pre-commit",
                                                                                 "outside/x.py", "/abs/x.py"])))
    assert len(errors) == 3, errors
    assert "under .git" in errors[0] and "escapes the repo (it resolves to" in errors[1]
    assert "relative to the repo root" in errors[2]


def test_a_done_when_program_not_in_allow(pw) -> None:
    e = only_error(pw, edit(lambda fm: fm["tasks"][0].update(done_when="ruff check .")))
    assert e.startswith("tasks[t1].done_when:") and "rule 7" in e and "allow list" in e


def test_done_when_is_read_from_the_checkouts_daedalus_toml(pw) -> None:
    (pw.alpha_repo / "daedalus.toml").unlink()
    errors = submit_errors(pw, good())
    assert len(errors) == 2 and all(".done_when:" in e and "daedalus.toml" in e for e in errors), errors


def test_max_iterations_of_11(pw) -> None:
    e = only_error(pw, edit(lambda fm: fm["tasks"][1].update(max_iterations=11)))
    assert e.startswith("tasks[t2].max_iterations:") and "1 to 10" in e


def test_a_budget_above_the_session_cap(pw) -> None:
    e = only_error(pw, edit(lambda fm: fm["tasks"][0].update(budget_usd=1.5)))
    assert e.startswith("tasks[t1].budget_usd:") and "session cap ($1.00)" in e


def test_a_missing_spec_note(pw) -> None:
    errors = submit_errors(pw, edit(lambda fm: fm.update(spec="[[no-such-spec]]")))
    assert errors[0].startswith("spec:") and "doesn't name a note" in errors[0]
    # With no spec to link, the plan also links nothing: said too, rather than hidden.
    assert errors[1:] == [e for e in errors[1:] if e.startswith("links:")]


def test_a_spec_in_another_vault(pw) -> None:
    e = submit_errors(pw, edit(lambda fm: fm.update(spec="[[quake-magnitude]]")))[0]
    assert e.startswith("spec:") and "beta:concepts/quake-magnitude.md" in e and "active project's vault" in e


def test_a_protected_path_that_doesnt_exist(pw) -> None:
    e = only_error(pw, edit(lambda fm: fm["tasks"][1].update(protected=["tests/test_cli.py"])))
    assert e.startswith("tasks[t2].protected:") and "doesn't exist" in e


def test_a_later_task_may_protect_a_file_an_earlier_task_lists(pw) -> None:
    """t1 creates tests/test_multiply.py, so t2 may protect it though it isn't on main yet (A2)."""
    def change(fm):
        fm["tasks"][0]["files"].append("tests/test_multiply.py")
        fm["tasks"][1].update(protected=["tests/test_mathx.py", "tests/test_multiply.py"])
    plans.submit(pw.session, "claude", "mathx", edit(change), BODY, session_cap_usd=CAP)


def test_an_earlier_task_may_not_protect_a_file_a_later_task_lists(pw) -> None:
    def change(fm):
        fm["tasks"][1]["files"].append("tests/test_cli.py")
        fm["tasks"][0].update(protected=["tests/test_cli.py"])
    e = only_error(pw, edit(change))
    assert e.startswith("tasks[t1].protected:") and "doesn't exist" in e and "no earlier task lists it in files" in e


def test_a_protected_file_no_task_lists_is_refused(pw) -> None:
    e = only_error(pw, edit(lambda fm: fm["tasks"][1].update(protected=["tests/test_nobody.py"])))
    assert e.startswith("tasks[t2].protected:") and "no earlier task lists it in files" in e


def test_protected_typed_into_one_item_is_refused(pw) -> None:
    e = only_error(pw, edit(lambda fm: fm["tasks"][1].update(protected=['["tests/test_mathx.py"]'])))
    assert e.startswith("tasks[t2].protected:")


def test_tasks_written_as_a_string(pw) -> None:
    e = only_error(pw, edit(lambda fm: fm.update(tasks="t1, t2")))
    assert "'tasks' must be a YAML list" in e


def test_empty_tasks_unknown_keys_and_server_keys(pw) -> None:
    errors = submit_errors(pw, edit(lambda fm: fm.update(tasks=[], source="human", owner="me")))
    assert "frontmatter.source: set by the server" in errors[0]
    assert errors[1].startswith("frontmatter.owner: unknown key")
    assert "tasks: must list at least one task" in errors


def test_task_shape_errors_name_the_task(pw) -> None:
    def change(fm):
        t = fm["tasks"][0]
        del t["red_test"]
        t.update(summary="two\nlines", files=[], colour="red")
        fm["tasks"][1].update(id="task-2", budget_usd=0)

    errors = submit_errors(pw, edit(change))
    assert "tasks[t1].colour: unknown key; a task has id, summary, files, red_test, done_when, max_iterations, " \
           "budget_usd, and optionally protected and red_test_note" in errors
    assert "tasks[t1].red_test: missing" in errors
    assert "tasks[t1].summary: must be one line" in errors
    assert any(e.startswith("tasks[t1].files: must be a non-empty list") for e in errors)
    assert any(e.startswith("tasks[#2].id:") for e in errors)
    assert any(e.startswith("tasks[#2].budget_usd:") and "greater than 0" in e for e in errors)


def test_the_body_needs_context(pw) -> None:
    with pytest.raises(PlanInvalid) as exc:
        plans.submit(pw.session, "claude", "mathx", good(), "   ", session_cap_usd=CAP)
    assert exc.value.errors == [f"body: '{plans.BODY_HEADING}' is empty; say what Mohamed needs to know to approve "
                                "the plan"]


def test_bad_slugs(pw) -> None:
    for slug, said in (("mathx-v2", "mustn't end in -v<N>"), ("../x", "must be letters"), ("", "must be letters")):
        errors = submit_errors(pw, good(), slug=slug)
        assert errors[0].startswith("slug:") and said in errors[0], errors


def test_one_call_returns_every_error(pw) -> None:
    """Break 2 target: stopping at the first error must fail here."""
    def change(fm):
        fm["decisions"][0].update(category="storage", basis="[[maybe-postgres]]")
        fm["decisions"][1]["basis"] = "[[beta-cli]]"
        fm["tasks"][0].update(files=["vault/x.md", "../x.py"], done_when="ruff check .", budget_usd=9)
        fm["tasks"][1].update(id="t1", max_iterations=11, protected=["tests/nope.py"])

    errors = submit_errors(pw, edit(change))
    wanted = ["decisions[1].category:", "decisions[1].basis:", "decisions[2].basis:", "tasks[t1].files: 'vault/x.md'",
              "tasks[t1].files: '../x.py'", "tasks[t1].done_when:", "tasks[t1].budget_usd:", "tasks[t1].id: duplicate",
              "tasks[t1].max_iterations:", "tasks[t1].protected:"]
    for prefix in wanted:
        assert any(e.startswith(prefix) for e in errors), f"missing {prefix!r} in {errors}"
    assert len(errors) == len(wanted), errors


def test_validate_plan_takes_any_plan_text(pw) -> None:
    text = plans.build(pw.session, "mathx", 1, good(), BODY, "human")
    assert validate_plan(pw.session, text, session_cap_usd=CAP) == []
    assert validate_plan(pw.session, "no frontmatter\n") == [
        "frontmatter: no '---'-delimited frontmatter block at the top of the plan"]


# 3. Revisions supersede -------------------------------------------------------------------------

def test_a_revision_supersedes_a_pending_v1_and_keeps_it(pw) -> None:
    v1 = plans.submit(pw.session, "claude", "mathx", good(), BODY, session_cap_usd=CAP).proposal
    fm = edit(lambda fm: fm["tasks"][0].update(max_iterations=5))
    v2 = plans.revise(pw.session, "claude", "mathx", fm, "Now with five iterations.", session_cap_usd=CAP)
    assert v2.superseded == v1.id and v2.version == 2
    assert v2.proposal.rel_path == "plans/mathx-v2.md"
    assert plans._fm_of(v2.proposal.content)["supersedes"] == "[[mathx-v1]]"
    old = queue.get(v1.id)
    assert old.status == "superseded" and old.status_reason == f"superseded by {v2.proposal.id}"
    assert old.content == v1.content  # still readable, unchanged
    st = plans.status(pw.session, "mathx")
    assert (st["version"], st["status"]) == (2, "pending")
    assert [(c["version"], c["status"], c["supersedes"]) for c in st["chain"]] == [
        (1, "superseded", None), (2, "pending", "[[mathx-v1]]")]
    assert plans.status(pw.session, v1.id)["status"] == "superseded"
    assert plans.status(pw.session, "mathx-v1")["id"] == v1.id


def test_revising_a_rejected_plan_leaves_it_rejected(pw) -> None:
    v1 = plans.submit(pw.session, "claude", "mathx", good(), BODY, session_cap_usd=CAP).proposal
    reject(v1.id, "protect the CLI test")
    v2 = plans.revise(pw.session, "claude", v1.id, good(), BODY, session_cap_usd=CAP)
    assert v2.superseded is None and queue.get(v1.id).status == "rejected"
    assert [c["status"] for c in plans.status(pw.session, "mathx")["chain"]] == ["rejected", "pending"]


def test_only_the_latest_version_can_be_revised(pw) -> None:
    plans.submit(pw.session, "claude", "mathx", good(), BODY, session_cap_usd=CAP)
    plans.revise(pw.session, "claude", "mathx", good(), BODY, session_cap_usd=CAP)
    with pytest.raises(PlanError, match="isn't the latest version; revise v2"):
        plans.revise(pw.session, "claude", "mathx-v1", good(), BODY, session_cap_usd=CAP)
    with pytest.raises(PlanError, match="no plan 'nope'"):
        plans.revise(pw.session, "claude", "nope", good(), BODY, session_cap_usd=CAP)


def test_an_invalid_revision_leaves_v1_pending(pw) -> None:
    v1 = plans.submit(pw.session, "claude", "mathx", good(), BODY, session_cap_usd=CAP).proposal
    with pytest.raises(PlanInvalid):
        plans.revise(pw.session, "claude", "mathx", edit(lambda fm: fm["tasks"][0].update(max_iterations=0)), BODY,
                     session_cap_usd=CAP)
    assert queue.statuses() == {v1.id: "pending"}


def test_a_second_submit_of_the_same_slug_is_refused(pw) -> None:
    plans.submit(pw.session, "claude", "mathx", good(), BODY, session_cap_usd=CAP)
    e = submit_errors(pw, good())
    assert e == ["slug: plan 'mathx' already exists (v1 is pending); change it with revise_plan, or pick another slug"]


# 6. No generic apply ------------------------------------------------------------------------------

def test_apply_refuses_a_plan_and_writes_nothing(pw) -> None:
    v1 = plans.submit(pw.session, "claude", "mathx", good(), BODY, session_cap_usd=CAP).proposal
    before = pw.snapshot()
    with pytest.raises(PlanApprovalOnly, match="a plan is approved only by plan approval"):
        apply(pw.session, v1.id)
    assert pw.snapshot() == before
    assert not (pw.alpha / "plans" / "mathx-v1.md").exists()


# Listing -----------------------------------------------------------------------------------------

def test_list_plans_marks_decisions_and_shows_chains(pw) -> None:
    plans.submit(pw.session, "claude", "mathx", good(), BODY, session_cap_usd=CAP)
    v2 = plans.revise(pw.session, "claude", "mathx", good(), BODY, session_cap_usd=CAP).proposal
    other = plans.submit(pw.session, "claude", "other", good(), BODY, session_cap_usd=CAP).proposal
    reject(other.id, "not now")
    write_note(pw.alpha, "plans/done-v1.md", "plan", "# Done\n", spec="[[mathx-cli-spec]]",
               related=["[[mathx-cli-spec]]"])
    listed = plans.list_plans(pw.session)
    [pending] = listed["pending"]
    assert (pending["id"], pending["slug"], pending["version"], pending["tasks"]) == (v2.id, "mathx", 2, 2)
    assert [d["marked"] for d in pending["decisions"]] == ["cited", "NEW"]
    assert [c["status"] for c in pending["chain"]] == ["superseded", "pending"]
    assert [(r["slug"], r["rejection_reason"]) for r in listed["rejected"]] == [("other", "not now")]
    assert [(a["slug"], a["version"]) for a in listed["approved"]] == [("done", 1)]


# Queue schema v4 ------------------------------------------------------------------------------------

def test_migrates_a_version_3_queue_keeping_rows(state_root) -> None:
    import sqlite3

    state_root.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(queue.queue_path())
    con.execute(queue._TABLE.format(name="proposals").replace(",\n                                    'superseded'", ""))
    con.execute("INSERT INTO proposals (id, vault, rel_path, kind, content, source, summary, created_at, updated_at, "
                "status, target_path) VALUES ('old3', 'alpha', 'plans/p-v1.md', 'create', 'x', 'claude', 's', 't', "
                "'t', 'pending', '/t')")
    for ddl in queue._CHANGESETS:
        con.execute(ddl)
    con.execute("PRAGMA user_version = 3")
    con.commit()
    con.close()
    with pytest.raises(sqlite3.IntegrityError):  # the v3 table really refuses the new status
        c = sqlite3.connect(queue.queue_path())
        try:
            c.execute("UPDATE proposals SET status = 'superseded'")
        finally:
            c.close()

    rec = queue.get("old3")
    assert (rec.status, rec.target_path) == ("pending", "/t")
    assert queue.transition("old3", "pending", "superseded")
    with queue.connect() as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == queue.SCHEMA_VERSION
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name = 'proposals_by_status'").fetchone()


# Structured red tests (commit 12c) ------------------------------------------------------------------

def test_a_text_red_test_gets_its_own_error(pw) -> None:
    e = only_error(pw, edit(lambda fm: fm["tasks"][0].update(red_test="tests/test_mathx.py::test_multiply fails")))
    assert e.startswith("tasks[t1].red_test: is text, and it must now be a list of the red tests")
    assert "red_test_note" in e


@pytest.mark.parametrize("entry, words", [
    ("tests/test_mathx.py::", "no test name after it"),
    ("/abs/tests/test_x.py", "relative to the repo root"),
    ("tests/test_mathx.txt", "must name a .py test file"),
    ("tests/../tests/test_mathx.py", "no '..' or './'"),
    (".git/hooks/test_x.py", "under .git"),
    ("tests/test_nobody.py::test_x", "neither this task nor an earlier one lists it in files"),
])
def test_red_test_entries_are_checked(pw, entry, words) -> None:
    e = only_error(pw, edit(lambda fm: fm["tasks"][0].update(red_test=[entry])))
    assert e.startswith("tasks[t1].red_test:") and words in e, e


def test_a_red_test_an_earlier_task_creates_is_allowed_and_a_note_is_prose(pw) -> None:
    def change(fm):
        fm["tasks"][0]["files"].append("tests/test_multiply.py")
        fm["tasks"][1].update(red_test=["tests/test_multiply.py::test_big"],
                              red_test_note="reads [tool.pytest.ini_options] and nothing.py")
    e = only_error(pw, edit(lambda fm: fm["tasks"][0].update(red_test_note="  ")))
    assert e == "tasks[t1].red_test_note: must be non-empty text, or left out"
    plans.submit(pw.session, "claude", "mathx", edit(change), BODY, session_cap_usd=CAP)
