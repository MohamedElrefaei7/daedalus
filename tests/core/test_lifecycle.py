"""/new: create a project vault from the template, commit it, register it; roll back on any failure."""

from __future__ import annotations

import os
import shutil

import pytest
from conftest import git, git_init, head, write_raw

from daedalus.core import lifecycle
from daedalus.core import registry as registry_mod
from daedalus.core.lifecycle import INIT_MESSAGE, LifecycleError, new_project, template_files
from daedalus.core.registry import load_registry


@pytest.fixture
def proj(gworld):
    """A fresh project repo with one commit, plus unrelated staged and modified work."""
    repo = gworld.root / "delta"
    repo.mkdir()
    git_init(repo)
    write_raw(repo, "README.md", "# delta\n")
    write_raw(repo, "notes.txt", "v1\n")
    git(repo, "add", "README.md", "notes.txt")
    git(repo, "commit", "-q", "-m", "first")
    write_raw(repo, "staged.txt", "staged, not committed\n")
    git(repo, "add", "staged.txt")
    write_raw(repo, "notes.txt", "v2, modified and unstaged\n")
    gworld.extra["repo"] = repo
    return gworld


def status(repo) -> str:
    return git(repo, "status", "--porcelain=v1", "--untracked-files=all")


def snapshot(w):
    repo = w.extra["repo"]
    return {"head": head(repo), "status": status(repo), "registry": w.registry_path.read_bytes(),
            "vault": os.path.lexists(repo / "vault")}


# Refusals --------------------------------------------------------------------------------

def test_name_collision_refused(proj) -> None:
    before = snapshot(proj)
    with pytest.raises(LifecycleError, match="already registered"):
        new_project("beta", proj.extra["repo"], proj.registry_path)
    assert snapshot(proj) == before


def test_bad_names_refused(proj) -> None:
    for bad in ("shared", "has space", "-dash", ""):
        with pytest.raises(LifecycleError):
            new_project(bad, proj.extra["repo"], proj.registry_path)


def test_non_repo_refused(proj, tmp_path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(LifecycleError, match="not a git repo"):
        new_project("plain", plain, proj.registry_path)
    with pytest.raises(LifecycleError, match="not a folder"):
        new_project("plain", tmp_path / "missing", proj.registry_path)
    sub = proj.extra["repo"] / "sub"
    sub.mkdir()
    with pytest.raises(LifecycleError, match="top-level"):
        new_project("sub", sub, proj.registry_path)
    assert not (plain / "vault").exists()


def test_repo_with_obsidian_at_root_refused_with_commit_1_message(proj) -> None:
    repo = proj.extra["repo"]
    (repo / ".obsidian").mkdir()
    with pytest.raises(LifecycleError) as exc:
        new_project("delta", repo, proj.registry_path)
    assert "was opened as an Obsidian vault by mistake" in str(exc.value)
    assert f"open {repo / 'vault'} instead" in str(exc.value)
    assert not (repo / "vault").exists()


def test_non_empty_vault_refused(proj) -> None:
    repo = proj.extra["repo"]
    write_raw(repo, "vault/mine.md", "my notes\n")
    before = snapshot(proj)
    with pytest.raises(LifecycleError, match="isn't empty"):
        new_project("delta", repo, proj.registry_path)
    assert snapshot(proj) == before
    assert (repo / "vault" / "mine.md").read_text() == "my notes\n"


# Success ----------------------------------------------------------------------------------

def test_success_commits_exactly_the_template_and_leaves_your_work_alone(proj) -> None:
    repo = proj.extra["repo"]
    base = head(repo)
    made = new_project("delta", repo, proj.registry_path)

    want = sorted([*(f"vault/{f}" for f in template_files()), "vault/.obsidian/types.json"])
    assert list(made.files) == want
    assert git(repo, "show", "--name-only", "--format=", "HEAD").split() == want
    assert git(repo, "log", "-1", "--format=%s").strip() == INIT_MESSAGE
    assert git(repo, "rev-parse", "HEAD~1").strip() == base
    # Your staged file is still staged and not in the commit; your modified file is still modified.
    assert "A  staged.txt" in status(repo).splitlines()
    assert " M notes.txt" in status(repo).splitlines()
    assert (repo / "notes.txt").read_text() == "v2, modified and unstaged\n"
    assert load_registry(proj.registry_path).projects["delta"] == repo / "vault"
    assert "Open folder as vault" in made.instructions() and str(repo / "vault") in made.instructions()


def test_the_vault_gets_obsidian_property_types_in_the_init_commit(proj) -> None:
    import json

    repo = proj.extra["repo"]
    made = new_project("delta", repo, proj.registry_path)
    written = json.loads((repo / "vault" / ".obsidian" / "types.json").read_text())
    assert written == {"types": {"created": "date", "related": "multitext", "shared_refs": "multitext",
                                 "project_refs": "multitext", "protected": "multitext", "code": "multitext",
                                 "options": "multitext", "applies_to": "multitext"}}
    committed = json.loads(git(repo, "show", "HEAD:vault/.obsidian/types.json"))
    assert committed == written
    steps = made.instructions()
    assert "Property type → Date" not in steps and "Make `created` a Date" not in steps
    assert "Turn on Templates".lower() in steps.lower() and "types.json" in steps


def test_empty_vault_folder_is_fine(proj) -> None:
    (proj.extra["repo"] / "vault").mkdir()
    new_project("delta", proj.extra["repo"], proj.registry_path)


def test_first_commit_in_an_empty_repo(proj, tmp_path) -> None:
    repo = tmp_path / "fresh"
    repo.mkdir()
    git_init(repo)
    made = new_project("fresh", repo, proj.registry_path)
    assert git(repo, "rev-list", "--count", "HEAD").strip() == "1"
    assert made.files


# Rollback: a failure after each of steps 4, 5, and 6 --------------------------------------

def _then_fail(real):
    def wrapper(*args, **kwargs):
        real(*args, **kwargs)  # the step really happens...
        raise OSError("injected failure")  # ...and then something breaks

    return wrapper


@pytest.mark.parametrize("after", ["copy_template", "commit_vault", "register_project"],
                         ids=["after_step_4", "after_step_5", "after_step_6"])
def test_failure_after_each_step_rolls_back_fully(proj, monkeypatch, after) -> None:
    before = snapshot(proj)
    target = registry_mod if after == "register_project" else lifecycle
    monkeypatch.setattr(target, after, _then_fail(getattr(target, after)))
    with pytest.raises(LifecycleError) as exc:
        new_project("delta", proj.extra["repo"], proj.registry_path)
    assert "injected failure" in str(exc.value) and "Everything was rolled back" in str(exc.value)
    after_state = snapshot(proj)
    assert after_state["vault"] is False, "vault/ was left behind"
    assert after_state["head"] == before["head"], "HEAD moved"
    assert after_state["registry"] == before["registry"], "the registry changed"
    assert after_state["status"] == before["status"], "your staged or modified work changed"


def test_rollback_of_a_first_commit(proj, tmp_path, monkeypatch) -> None:
    repo = tmp_path / "fresh"
    repo.mkdir()
    git_init(repo)
    write_raw(repo, "staged.txt", "x\n")
    git(repo, "add", "staged.txt")
    monkeypatch.setattr(registry_mod, "register_project", _then_fail(registry_mod.register_project))
    with pytest.raises(LifecycleError):
        new_project("fresh", repo, proj.registry_path)
    assert subprocess_ok(repo) is False  # an unborn branch again: no HEAD
    assert status(repo).splitlines() == ["A  staged.txt"]
    assert not (repo / "vault").exists()


def subprocess_ok(repo) -> bool:
    import subprocess

    return subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "-q", "HEAD"],
                          capture_output=True).returncode == 0


def test_rollback_keeps_a_commit_that_is_no_longer_head(proj, monkeypatch) -> None:
    repo = proj.extra["repo"]

    def commit_then_someone_commits(r):
        sha = lifecycle.commit_path(r, "vault", [INIT_MESSAGE], add=True)
        write_raw(repo, "other.txt", "x\n")
        git(repo, "add", "other.txt")
        git(repo, "commit", "-q", "-m", "someone else", "--", "other.txt")
        return sha

    monkeypatch.setattr(lifecycle, "commit_vault", commit_then_someone_commits)
    monkeypatch.setattr(registry_mod, "register_project", _then_fail(registry_mod.register_project))
    with pytest.raises(LifecycleError) as exc:
        new_project("delta", repo, proj.registry_path)
    assert "no longer the init commit" in str(exc.value)
    assert git(repo, "log", "-1", "--format=%s").strip() == "someone else"
