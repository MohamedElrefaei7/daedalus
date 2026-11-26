"""Part C: apply and its guarantees (tests 2-9)."""

from __future__ import annotations

import os
import stat

import pytest
from conftest import git, head, note_text, vault_digest, write_raw, write_registry

from daedalus.core import apply as apply_mod
from daedalus.core import queue
from daedalus.core.apply import apply, reject
from daedalus.core.policy import PolicyError
from daedalus.core.queue import CommitFailed, NotPending, StaleProposal
from daedalus.core.registry import load_registry
from daedalus.core.session import Session
from daedalus.core.tools import propose_edit, propose_lesson, propose_note

HELLO = "# Hello\n\nA draft.\n"
FAILING_HOOK = "#!/bin/sh\necho 'pre-commit: hook says no' >&2\nexit 1\n"


def commit_files(repo, rev="HEAD") -> list[str]:
    return git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", rev).split()


def commits_since(repo, base) -> int:
    return int(git(repo, "rev-list", "--count", f"{base}..HEAD"))


def install_hook(repo, body: str = FAILING_HOOK) -> None:
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(body)
    hook.chmod(0o755)


def test_one_apply_one_commit(gworld) -> None:
    """Test 2."""
    repo = gworld.alpha_repo
    base = head(repo)
    p = propose_note(gworld.session, "alpha", "inbox/hello.md", HELLO, "claude", "say hello")
    done = apply(gworld.session, p.id)

    assert (gworld.alpha / "inbox" / "hello.md").read_text() == HELLO
    assert commits_since(repo, base) == 1
    assert commit_files(repo) == ["vault/inbox/hello.md"]
    assert git(repo, "log", "-1", "--format=%B") == f"agent(claude): say hello\n\nDaedalus-Change: {p.id}\n\n"
    assert git(repo, "log", "-1", "--format=%(trailers:key=Daedalus-Change,valueonly)").strip() == p.id
    assert done.status == "applied" and done.commit_sha == head(repo)
    assert git(repo, "status", "--porcelain") == ""


def test_apply_edit(gworld) -> None:
    repo = gworld.alpha_repo
    base = head(repo)
    new = (gworld.alpha / "CONTEXT.md").read_text() + "\nNext: ship.\n"
    p = propose_edit(gworld.session, "alpha", "CONTEXT.md", new, "deepseek", "add next step")
    apply(gworld.session, p.id)
    assert (gworld.alpha / "CONTEXT.md").read_text() == new
    assert commits_since(repo, base) == 1 and commit_files(repo) == ["vault/CONTEXT.md"]
    assert git(repo, "log", "-1", "--format=%s") == "agent(deepseek): add next step\n"


def test_apply_edit_keeps_file_mode(gworld) -> None:
    target = gworld.alpha / "CONTEXT.md"
    target.chmod(0o600)
    p = propose_edit(gworld.session, "alpha", "CONTEXT.md", "# CONTEXT\n\nnew\n", "claude", "s")
    apply(gworld.session, p.id)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_apply_into_shared_commits_in_shared_repo(gworld) -> None:
    shared_base, alpha_base = head(gworld.shared), head(gworld.alpha_repo)
    p = propose_lesson(gworld.session, "git-hooks.md", note_text("lesson", "# Hooks\n", applies_to=["git"], related=["[[red-test-first]]"], source="claude"),
                       "claude", "lesson about hooks")
    apply(gworld.session, p.id)
    assert commits_since(gworld.shared, shared_base) == 1
    assert commit_files(gworld.shared) == ["inbox/git-hooks.md"]
    assert head(gworld.alpha_repo) == alpha_base


def test_staged_work_stays_put(gworld) -> None:
    """Test 3: unrelated staged work is still staged, and not committed, after an apply."""
    repo = gworld.alpha_repo
    write_raw(gworld.alpha, "inbox/mine.md", "my own work in progress\n")
    git(repo, "add", "vault/inbox/mine.md")
    (gworld.alpha / "components" / "usgs-fetcher.md").write_text("unstaged edit\n")

    p = propose_note(gworld.session, "alpha", "inbox/hello.md", HELLO, "claude", "say hello")
    apply(gworld.session, p.id)

    assert commit_files(repo) == ["vault/inbox/hello.md"]
    assert git(repo, "diff", "--cached", "--name-only").split() == ["vault/inbox/mine.md"]
    assert git(repo, "diff", "--name-only").split() == ["vault/components/usgs-fetcher.md"]


def test_stale_edit(gworld) -> None:
    """Test 4: you edit the target after the proposal; apply refuses and keeps your edit."""
    target = gworld.alpha / "CONTEXT.md"
    p = propose_edit(gworld.session, "alpha", "CONTEXT.md", "# CONTEXT\n\nagent version\n", "deepseek", "rewrite")
    target.write_text("# CONTEXT\n\nmy edit in Obsidian\n")
    base = head(gworld.alpha_repo)
    with pytest.raises(StaleProposal, match="changed after this edit was proposed"):
        apply(gworld.session, p.id)
    assert target.read_text() == "# CONTEXT\n\nmy edit in Obsidian\n"
    rec = queue.get(p.id)
    assert rec.status == "stale" and "changed after" in rec.status_reason
    assert head(gworld.alpha_repo) == base


def test_stale_edit_target_deleted(gworld) -> None:
    write_raw(gworld.alpha, "inbox/draft.md", "v1\n")
    p = propose_edit(gworld.session, "alpha", "inbox/draft.md", "v2\n", "claude", "s")
    (gworld.alpha / "inbox" / "draft.md").unlink()
    with pytest.raises(StaleProposal, match="deleted"):
        apply(gworld.session, p.id)
    assert not (gworld.alpha / "inbox" / "draft.md").exists()


def test_create_collision(gworld) -> None:
    """Test 5."""
    p = propose_note(gworld.session, "alpha", "inbox/hello.md", HELLO, "claude", "say hello")
    write_raw(gworld.alpha, "inbox/hello.md", "I wrote this first\n")
    with pytest.raises(StaleProposal, match="created after"):
        apply(gworld.session, p.id)
    assert (gworld.alpha / "inbox" / "hello.md").read_text() == "I wrote this first\n"
    assert queue.get(p.id).status == "stale"


@pytest.mark.parametrize("kind", ["create", "edit"])
def test_commit_failure_rolls_back(gworld, kind) -> None:
    """Test 6: a hook that rejects the commit leaves no write behind."""
    repo = gworld.alpha_repo
    target = gworld.alpha / ("inbox/hello.md" if kind == "create" else "CONTEXT.md")
    before = target.read_bytes() if kind == "edit" else None
    if kind == "create":
        p = propose_note(gworld.session, "alpha", "inbox/hello.md", HELLO, "claude", "s")
    else:
        p = propose_edit(gworld.session, "alpha", "CONTEXT.md", "# CONTEXT\n\nnew\n", "claude", "s")
    install_hook(repo)
    base = head(repo)

    with pytest.raises(CommitFailed, match="hook says no"):
        apply(gworld.session, p.id)

    if kind == "create":
        assert not target.exists()
    else:
        assert target.read_bytes() == before
    assert head(repo) == base
    assert git(repo, "status", "--porcelain") == ""  # nothing left staged or modified
    rec = queue.get(p.id)
    assert rec.status == "failed" and "pre-commit: hook says no" in rec.status_reason


def test_reauthorization_pulled(gworld) -> None:
    """Test 7: propose into alpha, apply with a session where alpha is only pulled."""
    p = propose_note(gworld.session, "alpha", "inbox/hello.md", HELLO, "claude", "s")
    later = Session(gworld.registry, active="beta", pulled=("alpha",))
    base = head(gworld.alpha_repo)
    with pytest.raises(PolicyError) as exc:
        apply(later, p.id)
    assert exc.value.rule == 3
    assert not (gworld.alpha / "inbox" / "hello.md").exists()
    assert head(gworld.alpha_repo) == base
    assert queue.get(p.id).status == "pending"


def test_reauthorization_unregistered(gworld) -> None:
    p = propose_note(gworld.session, "alpha", "inbox/hello.md", HELLO, "claude", "s")
    registry = load_registry(write_registry(gworld.root / "later.toml", gworld.shared, {"beta": gworld.beta}))
    with pytest.raises(PolicyError) as exc:
        apply(Session(registry, active="beta"), p.id)
    assert exc.value.rule == 1
    assert not (gworld.alpha / "inbox" / "hello.md").exists()


def test_symlink_swap_before_apply(gworld) -> None:
    """Test 8: after proposing, the target's folder becomes a symlink to the shared vault."""
    p = propose_note(gworld.session, "alpha", "inbox/hello.md", HELLO, "claude", "s")
    inbox = gworld.alpha / "inbox"
    inbox.rename(gworld.alpha / "inbox-real")
    inbox.symlink_to(gworld.shared)
    shared_digest, shared_head = vault_digest(gworld.shared), head(gworld.shared)

    with pytest.raises(PolicyError) as exc:
        apply(gworld.session, p.id)
    assert exc.value.rule == 2
    assert vault_digest(gworld.shared) == shared_digest and head(gworld.shared) == shared_head
    assert not (gworld.shared / "hello.md").exists()
    assert not (gworld.alpha / "inbox-real" / "hello.md").exists()


def test_symlink_swap_after_authorization(gworld, monkeypatch) -> None:
    """The swap lands between authorize() and the write: the write still can't follow it."""
    p = propose_note(gworld.session, "alpha", "inbox/hello.md", HELLO, "claude", "s")
    inbox = gworld.alpha / "inbox"
    real_authorize = apply_mod.authorize

    def authorize_then_swap(*args, **kwargs):
        auth = real_authorize(*args, **kwargs)
        inbox.rename(gworld.alpha / "inbox-real")
        inbox.symlink_to(gworld.shared / "inbox")
        return auth

    monkeypatch.setattr(apply_mod, "authorize", authorize_then_swap)
    shared_digest = vault_digest(gworld.shared)
    with pytest.raises(PolicyError, match="became a symlink") as exc:
        apply(gworld.session, p.id)
    assert exc.value.rule == 1
    assert vault_digest(gworld.shared) == shared_digest
    assert not (gworld.alpha / "inbox-real" / "hello.md").exists()
    assert queue.get(p.id).status == "pending"


def test_symlink_swap_after_write_is_rolled_back(gworld, monkeypatch) -> None:
    """The swap lands after the write: the post-write realpath check restores and refuses."""
    p = propose_note(gworld.session, "alpha", "inbox/hello.md", HELLO, "claude", "s")
    inbox = gworld.alpha / "inbox"
    real_write = apply_mod.atomic_write_at

    def write_then_swap(*args, **kwargs):
        real_write(*args, **kwargs)
        inbox.rename(gworld.alpha / "inbox-real")
        inbox.symlink_to(gworld.shared / "inbox")

    monkeypatch.setattr(apply_mod, "atomic_write_at", write_then_swap)
    shared_digest, base = vault_digest(gworld.shared), head(gworld.alpha_repo)
    with pytest.raises(PolicyError, match="now resolves to") as exc:
        apply(gworld.session, p.id)
    assert exc.value.rule == 1
    assert not (gworld.alpha / "inbox-real" / "hello.md").exists()  # the write was undone
    assert vault_digest(gworld.shared) == shared_digest
    assert head(gworld.alpha_repo) == base


def test_double_apply_refused(gworld) -> None:
    """Test 9."""
    p = propose_note(gworld.session, "alpha", "inbox/hello.md", HELLO, "claude", "s")
    apply(gworld.session, p.id)
    after_first = head(gworld.alpha_repo)
    with pytest.raises(NotPending, match="is applied, not pending"):
        apply(gworld.session, p.id)
    assert head(gworld.alpha_repo) == after_first


def test_rejected_proposal_cannot_be_applied(gworld) -> None:
    p = propose_note(gworld.session, "alpha", "inbox/hello.md", HELLO, "claude", "s")
    reject(p.id, "not needed")
    with pytest.raises(NotPending, match="is rejected"):
        apply(gworld.session, p.id)
    assert not (gworld.alpha / "inbox" / "hello.md").exists()


def test_no_temp_files_left_behind(gworld) -> None:
    p = propose_note(gworld.session, "alpha", "inbox/hello.md", HELLO, "claude", "s")
    apply(gworld.session, p.id)
    assert [n for n in os.listdir(gworld.alpha / "inbox") if n.startswith(".daedalus-")] == []


def test_edit_of_untracked_draft_commit_failure_unstages(gworld) -> None:
    repo = gworld.alpha_repo
    write_raw(gworld.alpha, "inbox/draft.md", "v1\n")  # hand-written, never committed
    p = propose_edit(gworld.session, "alpha", "inbox/draft.md", "v2\n", "claude", "s")
    install_hook(repo)
    with pytest.raises(CommitFailed):
        apply(gworld.session, p.id)
    assert (gworld.alpha / "inbox" / "draft.md").read_text() == "v1\n"
    assert git(repo, "status", "--porcelain").strip() == "?? vault/inbox/draft.md"  # untracked again, not staged
