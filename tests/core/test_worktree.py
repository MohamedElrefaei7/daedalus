"""Agent worktrees against real git: lifecycle, drop protection, merge safety, and commit's vault/ guard."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import git, head, vault_digest, write_raw

from daedalus.core import tools, worktree
from daedalus.core.session import Session
from daedalus.core.worktree import MergeConflict, WorktreeError


@pytest.fixture(autouse=True)
def wt_root(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "worktrees"
    monkeypatch.setattr(worktree, "WORKTREE_ROOT", root)
    return root


def coding(w, created: worktree.Created) -> Session:
    return Session(w.registry, "alpha", pulled=("beta",), worktree=Path(created.record.path))


def branches(repo: Path) -> list[str]:
    return git(repo, "branch", "--format=%(refname:short)").split()


def worktrees_git_knows(repo: Path) -> list[str]:
    return [line.split(" ", 1)[1] for line in git(repo, "worktree", "list", "--porcelain").splitlines()
            if line.startswith("worktree ")]


def checkout_state(w) -> tuple[str, str, str]:
    return vault_digest(w.alpha_repo), head(w.alpha_repo), git(w.alpha_repo, "status", "--porcelain")


# 1. Lifecycle ---------------------------------------------------------------------------------

def test_create_list_commit_diff_merge_drop(gworld, wt_root) -> None:
    repo = gworld.alpha_repo
    base = head(repo)
    made = worktree.create(gworld.session, "hello")
    rec = made.record
    assert made.warning is None
    assert rec.branch == "agent/hello" and rec.base == base
    assert Path(rec.path).parent == Path(os.path.realpath(wt_root)) / "alpha"
    assert Path(rec.path).name.startswith("hello-") and len(Path(rec.path).name) == len("hello-") + 6
    assert "agent/hello" in branches(repo) and os.path.realpath(rec.path) in worktrees_git_knows(repo)

    (info,) = worktree.list_worktrees(gworld.registry, "alpha")
    assert (info.slug, info.branch, info.ahead, info.dirty, info.state) == ("hello", "agent/hello", 0, False, "ok")

    s = coding(gworld, made)
    tools.write_code(s, "src/mathx.py", "def add(a, b):\n    return a + b\n")
    assert worktree.list_worktrees(gworld.registry, "alpha")[0].dirty is True
    sha = worktree.commit(s, "add mathx.add")
    assert git(Path(rec.path), "log", "-1", "--format=%B", sha).strip().endswith("Daedalus-Worktree: hello")
    (info,) = worktree.list_worktrees(gworld.registry, "alpha")
    assert (info.ahead, info.dirty) == (1, False)
    assert head(repo) == base, "committing in the worktree moved the checkout"

    d = worktree.diff(gworld.registry, "alpha", "hello")
    assert "+++ b/src/mathx.py" in d and "+def add(a, b):" in d

    merged = worktree.merge(gworld.registry, "alpha", "hello")
    parents = git(repo, "rev-list", "--parents", "-n", "1", merged).split()
    assert parents == [merged, base, sha], "a --no-ff merge commit with the old HEAD and the agent commit as parents"
    assert (repo / "src" / "mathx.py").read_text() == "def add(a, b):\n    return a + b\n"
    assert git(repo, "log", "-1", "--format=%s").strip() == "merge: agent/hello"
    assert worktree.list_worktrees(gworld.registry, "alpha")[0].ahead == 0

    worktree.drop(gworld.registry, "alpha", "hello")
    assert not Path(rec.path).exists()
    assert "agent/hello" not in branches(repo)
    assert worktrees_git_knows(repo) == [str(repo)]
    assert worktree.list_worktrees(gworld.registry, "alpha") == []
    assert git(repo, "status", "--porcelain") == ""


def test_create_refuses_a_taken_slug_branch_or_bad_name(gworld) -> None:
    worktree.create(gworld.session, "hello")
    with pytest.raises(WorktreeError, match="already has a worktree 'hello'"):
        worktree.create(gworld.session, "hello")
    git(gworld.alpha_repo, "branch", "agent/taken")
    with pytest.raises(WorktreeError, match="branch agent/taken already exists"):
        worktree.create(gworld.session, "taken")
    for bad in ("../up", "-dash", "has space", "", "a/b"):
        with pytest.raises(WorktreeError, match="must follow the project name rules"):
            worktree.create(gworld.session, bad)


def test_create_warns_that_uncommitted_checkout_changes_are_not_in_the_worktree(gworld) -> None:
    write_raw(gworld.alpha_repo, "notes.txt", "not committed\n")
    made = worktree.create(gworld.session, "hello")
    assert "uncommitted changes" in made.warning and "starts from HEAD" in made.warning
    assert not (Path(made.record.path) / "notes.txt").exists()


def test_list_reports_a_worktree_git_knows_but_daedalus_did_not_make(gworld, tmp_path) -> None:
    worktree.create(gworld.session, "hello")
    stray = tmp_path / "stray"
    git(gworld.alpha_repo, "worktree", "add", "-q", "-b", "someone/else", str(stray))
    rows = worktree.list_worktrees(gworld.registry, "alpha")
    assert [(r.slug, r.state) for r in rows] == [("hello", "ok"), (None, "unrecorded")]
    assert rows[1].branch == "someone/else" and rows[1].path == os.path.realpath(stray)


# 2. Drop protection ---------------------------------------------------------------------------

def test_drop_refuses_unmerged_commits_and_force_removes_them(gworld) -> None:
    made = worktree.create(gworld.session, "hello")
    s = coding(gworld, made)
    tools.write_code(s, "src/new.py", "x = 1\n")
    sha = worktree.commit(s, "work worth keeping")
    with pytest.raises(WorktreeError, match="1 commit\\(s\\) not merged into HEAD"):
        worktree.drop(gworld.registry, "alpha", "hello")
    assert "agent/hello" in branches(gworld.alpha_repo) and Path(made.record.path).is_dir()
    assert git(gworld.alpha_repo, "rev-parse", "agent/hello").strip() == sha, "nothing was lost"
    assert worktree.records("alpha")["hello"] == made.record

    worktree.drop(gworld.registry, "alpha", "hello", force=True)
    assert "agent/hello" not in branches(gworld.alpha_repo) and not Path(made.record.path).exists()


def test_drop_refuses_uncommitted_changes_without_force(gworld) -> None:
    made = worktree.create(gworld.session, "hello")
    tools.write_code(coding(gworld, made), "src/wip.py", "x = 1\n")
    with pytest.raises(WorktreeError, match="uncommitted changes"):
        worktree.drop(gworld.registry, "alpha", "hello")
    assert (Path(made.record.path) / "src" / "wip.py").is_file()
    worktree.drop(gworld.registry, "alpha", "hello", force=True)
    assert not Path(made.record.path).exists()


# 3. Merge safety ------------------------------------------------------------------------------

def test_merge_with_a_dirty_checkout_is_refused_and_changes_nothing(gworld) -> None:
    made = worktree.create(gworld.session, "hello")
    s = coding(gworld, made)
    tools.write_code(s, "src/new.py", "x = 1\n")
    worktree.commit(s, "new file")
    write_raw(gworld.alpha_repo, "README.md", "my own work in progress\n")
    before = checkout_state(gworld)
    with pytest.raises(WorktreeError, match="has uncommitted changes"):
        worktree.merge(gworld.registry, "alpha", "hello")
    assert checkout_state(gworld) == before


def test_a_conflicting_merge_is_aborted_and_the_checkout_is_byte_identical(gworld) -> None:
    repo = gworld.alpha_repo
    write_raw(repo, "src/shared.py", "value = 0\n")
    git(repo, "add", "src/shared.py")
    git(repo, "commit", "-q", "-m", "shared file")
    made = worktree.create(gworld.session, "hello")
    s = coding(gworld, made)
    tools.edit_code(s, "src/shared.py", "value = 0", "value = 1")
    tools.write_code(s, "src/other.py", "y = 2\n")
    worktree.commit(s, "agent changes value")
    write_raw(repo, "src/shared.py", "value = 2\n")
    git(repo, "commit", "-q", "-am", "I change value too")
    before = checkout_state(gworld)
    with pytest.raises(MergeConflict) as exc:
        worktree.merge(gworld.registry, "alpha", "hello")
    assert exc.value.files == ["src/shared.py"]
    assert checkout_state(gworld) == before
    assert not (repo / ".git" / "MERGE_HEAD").exists() and not (repo / "src" / "other.py").exists()


def test_merge_refuses_a_branch_that_touches_vault(gworld) -> None:
    made = worktree.create(gworld.session, "hello")
    wt = Path(made.record.path)
    write_raw(wt, "vault/inbox/sneaky.md", "behind the tools' back\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "sneaky")
    before = checkout_state(gworld)
    with pytest.raises(WorktreeError, match="changes vault/ paths"):
        worktree.merge(gworld.registry, "alpha", "hello")
    assert checkout_state(gworld) == before


# 6. Commit never includes vault/ --------------------------------------------------------------

def test_commit_aborts_if_a_vault_path_is_staged(gworld) -> None:
    made = worktree.create(gworld.session, "hello")
    wt = Path(made.record.path)
    s = coding(gworld, made)
    tools.write_code(s, "src/ok.py", "x = 1\n")
    write_raw(wt, "vault/inbox/forced.md", "written behind the tools' back\n")
    git(wt, "add", "-f", "vault/inbox/forced.md")
    branch_before = git(wt, "rev-parse", "HEAD").strip()
    with pytest.raises(WorktreeError, match="vault/inbox/forced.md under vault/ were staged"):
        worktree.commit(s, "should not land")
    assert git(wt, "rev-parse", "HEAD").strip() == branch_before, "nothing was committed"
    assert git(wt, "diff", "--cached", "--name-only") == "", "the index was reset"
    assert (wt / "vault" / "inbox" / "forced.md").is_file() and (wt / "src" / "ok.py").is_file()


def test_commit_needs_an_active_recorded_worktree_and_something_to_commit(gworld) -> None:
    with pytest.raises(WorktreeError, match="isn't a worktree this module created"):
        worktree.commit(gworld.session, "x")  # the world's own fake worktree
    made = worktree.create(gworld.session, "hello")
    with pytest.raises(WorktreeError, match="nothing to commit"):
        worktree.commit(coding(gworld, made), "x")
    with pytest.raises(WorktreeError, match="needs a message"):
        worktree.commit(coding(gworld, made), "  ")
