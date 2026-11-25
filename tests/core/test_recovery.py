"""Crash recovery and lock survival for the write path."""

from __future__ import annotations

import multiprocessing
import time

import pytest
import workers
from conftest import git, head

from daedalus.core import index, queue
from daedalus.core.apply import apply
from daedalus.core.tools import propose_edit, propose_note

HELLO = "# Hello\n\nA draft.\n"


def run_process(target, args: tuple) -> int:
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=target, args=args)
    proc.start()
    proc.join(timeout=60)
    assert proc.exitcode is not None, "worker hung"
    return proc.exitcode


def propose(w, kind: str):
    if kind == "create":
        return propose_note(w.session, "alpha", "inbox/hello.md", HELLO, "claude", "say hello"), w.alpha / "inbox" / "hello.md"
    return (propose_edit(w.session, "alpha", "CONTEXT.md", "# CONTEXT\n\nagent version\n", "claude", "rewrite"),
            w.alpha / "CONTEXT.md")


@pytest.mark.parametrize("kind", ["create", "edit"])
def test_crash_after_replace_is_rolled_back_on_next_open(gworld, state_root, kind) -> None:
    p, target = propose(gworld, kind)
    before = target.read_bytes() if target.exists() else None
    base = head(gworld.alpha_repo)

    args = (str(gworld.registry_path), str(state_root), str(index.CACHE_ROOT), p.id, "after_replace")
    assert run_process(workers.apply_and_crash, args) == 1
    assert target.read_text() == p.content, "the crash should land after the replace"

    queue.connect().close()  # a fresh queue open reconciles

    if before is None:
        assert not target.exists()
    else:
        assert target.read_bytes() == before
    assert head(gworld.alpha_repo) == base
    assert git(gworld.alpha_repo, "status", "--porcelain") == ""
    rec = queue.get(p.id)
    assert (rec.status, rec.status_reason) == ("failed", "interrupted")


@pytest.mark.parametrize("kind", ["create", "edit"])
def test_crash_after_commit_reconciles_to_applied(gworld, state_root, kind) -> None:
    p, target = propose(gworld, kind)
    base = head(gworld.alpha_repo)
    args = (str(gworld.registry_path), str(state_root), str(index.CACHE_ROOT), p.id, "after_commit")
    assert run_process(workers.apply_and_crash, args) == 1

    queue.connect().close()

    rec = queue.get(p.id)
    assert rec.status == "applied" and rec.commit_sha == head(gworld.alpha_repo) != base
    assert target.read_text() == p.content


def test_reconcile_leaves_a_file_changed_after_the_crash(gworld, state_root) -> None:
    p, target = propose(gworld, "create")
    args = (str(gworld.registry_path), str(state_root), str(index.CACHE_ROOT), p.id, "after_replace")
    assert run_process(workers.apply_and_crash, args) == 1
    target.write_text("I edited it before anyone noticed the crash\n")

    queue.connect().close()

    assert target.read_text() == "I edited it before anyone noticed the crash\n"
    rec = queue.get(p.id)
    assert rec.status == "failed" and rec.status_reason.startswith("interrupted; the target has changed since")


def test_reconcile_skips_an_apply_that_is_still_running(gworld, state_root) -> None:
    """A live apply holds the write lock; its 'applying' record must not be touched."""
    p, target = propose(gworld, "create")
    target.write_text(p.content)  # as if mid-apply: written, not yet committed
    assert queue.transition(p.id, "pending", "applying", target_path=str(target),
                            repo_path=str(gworld.alpha_repo), base_head=head(gworld.alpha_repo))
    ctx = multiprocessing.get_context("spawn")
    held, release = ctx.Event(), ctx.Event()
    holder = ctx.Process(target=workers.hold_lock_until, args=(str(state_root), held, release))
    holder.start()
    try:
        assert held.wait(timeout=30)
        queue.connect().close()
        assert queue.get(p.id).status == "applying" and target.exists()
    finally:
        release.set()
        holder.join(timeout=30)
    queue.connect().close()  # the lock is free now: reconcile
    assert queue.get(p.id).status == "failed" and not target.exists()


def test_migrates_a_version_1_queue(state_root) -> None:
    import sqlite3

    v1 = """CREATE TABLE proposals (id TEXT PRIMARY KEY, vault TEXT NOT NULL, rel_path TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('create', 'edit')), content TEXT NOT NULL, base_sha256 TEXT,
        base_content TEXT, source TEXT NOT NULL CHECK (source IN ('deepseek', 'claude', 'human')),
        summary TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending', 'applied', 'rejected', 'stale', 'failed')),
        status_reason TEXT, commit_sha TEXT)"""
    state_root.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(queue.queue_path())
    con.execute(v1)
    con.execute("INSERT INTO proposals VALUES ('old1', 'alpha', 'inbox/a.md', 'create', 'x', NULL, NULL, 'human', "
                "'s', 't', 't', 'applied', NULL, 'abc')")
    con.commit()
    con.close()

    rec = queue.get("old1")
    assert (rec.status, rec.commit_sha, rec.target_path) == ("applied", "abc", None)
    with queue.connect() as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == queue.SCHEMA_VERSION
    assert queue.transition("old1", "applied", "applying")  # the new status is accepted


# Lock survival ----------------------------------------------------------------------------

def test_lock_released_when_holder_crashes(gworld, state_root) -> None:
    """The write lock is flock(2): the kernel drops it when the holder dies, even via os._exit."""
    assert run_process(workers.hold_lock_and_crash, (str(state_root),)) == 1
    assert (state_root / "write.lock").exists()  # the file stays; it means nothing
    p = propose_note(gworld.session, "alpha", "inbox/hello.md", HELLO, "claude", "s")
    t0 = time.monotonic()
    assert apply(gworld.session, p.id).status == "applied"
    assert time.monotonic() - t0 < 1.0


def test_lock_excludes_while_held(state_root) -> None:
    from daedalus.core.errors import LockBusy
    from daedalus.core.vaultio import write_lock

    ctx = multiprocessing.get_context("spawn")
    held, release = ctx.Event(), ctx.Event()
    holder = ctx.Process(target=workers.hold_lock_until, args=(str(state_root), held, release))
    holder.start()
    try:
        assert held.wait(timeout=30)
        with pytest.raises(LockBusy):
            with write_lock(timeout=0.2):
                pass
    finally:
        release.set()
        holder.join(timeout=30)
    with write_lock(timeout=1):
        pass
