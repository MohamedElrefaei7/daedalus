"""Part D: write_log is create-only and commits at once (test 10)."""

from __future__ import annotations

import multiprocessing

import pytest
import workers
from conftest import git, head, spawn
from test_apply import install_hook

from daedalus.core import index
from daedalus.core.policy import PolicyError
from daedalus.core.queue import CommitFailed, ProposalError
from daedalus.core.tools import write_log


def test_write_log_creates_and_commits(gworld) -> None:
    repo = gworld.alpha_repo
    base = head(repo)
    sha = write_log(gworld.session, "run-7", "# Run 7\n\nall green\n", "deepseek")
    assert (gworld.alpha / "logs" / "run-7.md").read_text() == "# Run 7\n\nall green\n"
    assert sha == head(repo) and git(repo, "rev-list", "--count", f"{base}..HEAD").strip() == "1"
    assert git(repo, "log", "-1", "--format=%s").strip() == "log(deepseek): run-7"
    assert git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").split() == ["vault/logs/run-7.md"]
    assert git(repo, "status", "--porcelain") == ""


def test_second_write_log_refused_and_first_untouched(gworld) -> None:
    """Test 10, sequential: same run_id twice."""
    write_log(gworld.session, "run-7", "first\n", "deepseek")
    path = gworld.alpha / "logs" / "run-7.md"
    first, base = path.read_bytes(), head(gworld.alpha_repo)
    with pytest.raises(PolicyError) as exc:
        write_log(gworld.session, "run-7", "second\n", "claude")
    assert exc.value.rule == 5
    assert path.read_bytes() == first
    assert head(gworld.alpha_repo) == base


def test_concurrent_write_log_exactly_one_success(gworld, state_root) -> None:
    """Test 10, concurrent: both processes pass the policy check, then race to create."""
    barrier = multiprocessing.get_context("spawn").Barrier(2)
    base = head(gworld.alpha_repo)
    common = (str(gworld.registry_path), str(state_root), str(index.CACHE_ROOT), "run-race")
    results = spawn(workers.write_log_race, [(*common, f"body from racer {n}\n", barrier) for n in (1, 2)])

    outcomes = sorted(r[0] for r in results)
    assert outcomes == ["ok", "refused"], results
    winner = next(r for r in results if r[0] == "ok")
    loser = next(r for r in results if r[0] == "refused")
    assert loser[1] == 5
    assert (gworld.alpha / "logs" / "run-race.md").read_text() == winner[1]
    assert git(gworld.alpha_repo, "rev-list", "--count", f"{base}..HEAD").strip() == "1"


def test_existing_log_from_human_refused(gworld) -> None:
    path = gworld.alpha / "logs" / "2026-11-25-run-1.md"
    before = path.read_bytes()
    with pytest.raises(PolicyError) as exc:
        write_log(gworld.session, "2026-11-25-run-1", "overwrite?\n", "deepseek")
    assert exc.value.rule == 5 and path.read_bytes() == before


def test_write_log_commit_failure_rolls_back(gworld) -> None:
    install_hook(gworld.alpha_repo)
    base = head(gworld.alpha_repo)
    with pytest.raises(CommitFailed, match="hook says no"):
        write_log(gworld.session, "run-8", "body\n", "deepseek")
    assert not (gworld.alpha / "logs" / "run-8.md").exists()
    assert head(gworld.alpha_repo) == base
    assert git(gworld.alpha_repo, "status", "--porcelain") == ""


@pytest.mark.parametrize("run_id", ["", "../escape", "a/b", ".hidden", "a..b", "x" * 200])
def test_bad_run_id(gworld, run_id) -> None:
    with pytest.raises(ProposalError, match="run_id"):
        write_log(gworld.session, run_id, "body\n", "deepseek")


def test_bad_source(gworld) -> None:
    with pytest.raises(ProposalError, match="source"):
        write_log(gworld.session, "run-9", "body\n", "robot")
