"""Applying and rejecting proposals.

apply() is the only path from the queue into a vault. Its steps, in order:
  1. Load the record; refuse unless it is pending.
  2. Re-authorize against the session passed in now, not the one at propose time.
  3. Stale check: an edit's target must still hash to base_sha256; a create's
     target must still not exist. Otherwise mark the proposal stale and refuse.
  4. Mark the proposal "applying" (recording target, repo, and HEAD; the old
     content is already stored), then write atomically (temp file, fsync,
     os.replace) inside the authorized folder, and confirm realpath(target)
     is still the authorized path. If the process dies from here until step 7,
     the next queue open reconciles it (see daedalus.core.queue).
  5. Commit exactly that one path: git commit --only -- <path>.
  6. If the commit fails, restore the previous content (or remove a create)
     and mark the proposal failed with git's output.
  7. Mark the proposal applied and store the commit sha.

A write that isn't committed shouldn't exist.

apply_changes() does the same for several paths at once, as one commit:
creates, edits, and deletes, all in one repo. Every path's prior and new
content is recorded (queue.begin_changeset) before the first write, so a
failed commit or a crash restores every one of them.
"""

from __future__ import annotations

import difflib
import hashlib
import os
from pathlib import Path
from dataclasses import dataclass
from typing import NamedTuple

from . import queue
from .errors import CommitFailed, NotPending, PlanApprovalOnly, ProposalError, StaleProposal
from .policy import Op, PolicyError, authorize
from .queue import Proposal
from .registry import SHARED_NAME, Registry
from .session import Session
from .vaultio import (  # module-level names, so tests can patch them here
    atomic_write_at, check_still_authorized, commit_path, commit_paths, exists_at, head_or_none, is_tracked,
    open_dir_nofollow, read_at, restore_at, unstage, write_lock,
)


class Rejection(NamedTuple):
    id: str
    rel_path: str
    summary: str
    reason: str
    rejected_at: str


def vault_root(registry: Registry, vault: str) -> Path:
    """The registered root of ``vault``; rule 1 if it is no longer registered."""
    if vault == SHARED_NAME:
        return registry.shared
    if vault in registry.projects:
        return registry.projects[vault]
    raise PolicyError(1, vault, f"vault {vault!r} is not registered")


def repo_for(registry: Registry, vault: str) -> Path:
    """The git repo holding ``vault``: the project repo, or the shared vault's own repo."""
    if vault == SHARED_NAME:
        shared = registry.shared
        repo = next((p for p in (shared, *shared.parents) if (p / ".git").exists()), None)
        if repo is None:
            raise ProposalError(f"the shared vault {shared} is not in a git repo, so nothing can be committed there")
        return repo
    vault_root(registry, vault)
    return registry.repos[vault]


def is_plan_proposal(p: Proposal) -> bool:
    """A proposal into a project's plans/ (rule 5's WRITE_PLAN area)."""
    return p.vault != SHARED_NAME and p.rel_path.startswith("plans/")


def commit_message(p: Proposal) -> list[str]:
    return [f"agent({p.source}): {p.summary}", f"Daedalus-Change: {p.id}"]


# Public API -----------------------------------------------------------------------------

def apply(session: Session, change_id: str) -> Proposal:
    """Apply one pending proposal: write it and make exactly one commit."""
    with write_lock():
        # 1. Only a pending proposal can be applied, so applying twice is refused.
        rec = queue.get(change_id)
        if rec.status != "pending":
            raise NotPending(f"proposal {change_id} is {rec.status}, not pending")
        # A plan never goes through the generic path: approving one must also write its new decisions.
        if is_plan_proposal(rec):
            raise PlanApprovalOnly(f"proposal {change_id} is a plan ({rec.vault}:{rec.rel_path}); a plan is approved "
                                   "only by plan approval (/approve in the terminal), which writes it and its new "
                                   "decisions in one commit. Nothing was written.")

        # 2. Re-authorize against the session passed in now.
        auth = authorize(vault_root(session.registry, rec.vault) / rec.rel_path, Op.PROPOSE, session)
        repo = repo_for(session.registry, rec.vault)
        rel_to_repo = os.path.relpath(auth.path, repo)
        name = auth.path.name

        dirfd = open_dir_nofollow(auth.path.parent)
        try:
            # 3. Stale check.
            previous = read_at(dirfd, name)
            if rec.kind == "edit":
                current = None if previous is None else hashlib.sha256(previous[0]).hexdigest()
                if current != rec.base_sha256:
                    reason = ("the target was deleted after this edit was proposed" if current is None else
                              f"the target changed after this edit was proposed (sha256 {current[:12]}, "
                              f"expected {rec.base_sha256[:12]})")
                    queue.transition(rec.id, "pending", "stale", reason=reason)
                    raise StaleProposal(f"proposal {rec.id}: {reason}")
            elif previous is not None or exists_at(dirfd, name):
                reason = "the target was created after this create was proposed"
                queue.transition(rec.id, "pending", "stale", reason=reason)
                raise StaleProposal(f"proposal {rec.id}: {reason}")

            # 4. Mark applying, so a crash from here on is reconciled at the next
            #    queue open. Then the atomic write, and confirm the target still
            #    resolves to the authorized path.
            if not queue.transition(rec.id, "pending", "applying", target_path=str(auth.path),
                                    repo_path=str(repo), base_head=head_or_none(repo)):
                raise NotPending(f"proposal {change_id} is no longer pending")

            # A create, or an edit of a file git doesn't track yet (a hand-written
            # draft), must be staged before `git commit --only` will take it.
            stage = rec.kind == "create" or not is_tracked(repo, rel_to_repo)

            def roll_back(to: str, reason: str | None) -> None:
                restore_at(dirfd, name, previous)
                if stage:
                    unstage(repo, rel_to_repo)
                queue.transition(rec.id, "applying", to, reason=reason)

            try:
                atomic_write_at(dirfd, name, rec.content.encode("utf-8"), previous[1] if previous else None)
                check_still_authorized(auth.path)
            except PolicyError:
                roll_back("pending", None)  # a policy refusal depends on the session, so it stays pending
                raise
            except BaseException as e:
                roll_back("failed", f"write failed: {e!r}")
                raise

            # 5 and 6. Commit exactly that path, or roll the write back.
            try:
                sha = commit_path(repo, rel_to_repo, commit_message(rec), add=stage)
            except CommitFailed as e:
                roll_back("failed", e.output or str(e))
                raise
            except BaseException as e:
                roll_back("failed", f"commit interrupted: {e!r}")
                raise
        finally:
            os.close(dirfd)

        # 7. Applied.
        queue.transition(rec.id, "applying", "applied", commit_sha=sha)
        return queue.get(rec.id)


# Several paths, one commit -------------------------------------------------------------

@dataclass(frozen=True)
class Change:
    """One path in a change set. The caller has already authorized ``path`` (a resolved real path)."""

    path: Path
    content: str | None  # the new content; None deletes the file
    base_sha256: str | None  # what the caller read: the file must still hash to this; None means it must not exist


def apply_changes(repo: Path, changes: list[Change], summary: str, message: list[str] | None = None,
                  change_id: str | None = None) -> str:
    """Write every change and make exactly one commit, or leave every path as it was. Returns the sha.

    Under the write lock, in order: stale-check every path against its
    base_sha256; record the change set as applying, with each path's prior
    and new content; write each (atomic replace, or unlink); confirm each
    still resolves to its authorized path; commit exactly these paths with
    ``git commit --only``. Any failure, including a failing hook, restores
    every path and marks the change set failed. A crash anywhere after the
    record is reconciled on the next queue open.

    ``change_id`` names the change set (and the commit's Daedalus-Change
    trailer); plan approval passes the plan proposal's id, so the proposal and
    its change set reconcile against the same commit.
    """
    if not changes:
        raise ValueError("apply_changes needs at least one change")
    rels = [os.path.relpath(c.path, repo) for c in changes]
    if any(r == ".." or r.startswith("../") for r in rels):
        raise ProposalError(f"every path in a change set must be inside {repo}")
    if len(set(rels)) != len(rels):
        raise ProposalError("a change set names the same path twice")
    change_id = change_id or queue.new_id()
    message = message or [summary]
    with write_lock():
        dirfds = [open_dir_nofollow(c.path.parent) for c in changes]
        try:
            priors = []
            for c, fd in zip(changes, dirfds):
                prior = read_at(fd, c.path.name)
                current = None if prior is None else hashlib.sha256(prior[0]).hexdigest()
                if c.base_sha256 is None and (prior is not None or exists_at(fd, c.path.name)):
                    raise StaleProposal(f"{c.path} already exists")
                if c.base_sha256 is not None and current != c.base_sha256:
                    what = "was deleted" if current is None else "changed"
                    raise StaleProposal(f"{c.path} {what} since it was read; nothing was written")
                priors.append(prior)

            # New or untracked paths must be staged for `git commit --only`; a deleted untracked path has
            # nothing for git to commit, so it stays out of the pathspec.
            tracked = [prior is not None and is_tracked(repo, rel) for prior, rel in zip(priors, rels)]
            staged = [c.content is not None and not t for c, t in zip(changes, tracked)]
            in_commit = [rel for c, rel, t in zip(changes, rels, tracked) if c.content is not None or t]
            queue.begin_changeset(change_id, repo, head_or_none(repo), summary, [
                queue.ChangePath(str(c.path), None if prior is None else prior[0], None if prior is None else prior[1],
                                 None if c.content is None else c.content.encode("utf-8"), st)
                for c, prior, st in zip(changes, priors, staged)])

            def roll_back(reason: str) -> None:
                for c, fd, prior, rel, st in zip(changes, dirfds, priors, rels, staged):
                    restore_at(fd, c.path.name, prior)
                    if st:
                        unstage(repo, rel)
                queue.finish_changeset(change_id, "failed", reason=reason)

            try:
                for c, fd, prior in zip(changes, dirfds, priors):
                    if c.content is None:
                        os.unlink(c.path.name, dir_fd=fd)
                        os.fsync(fd)
                    else:
                        atomic_write_at(fd, c.path.name, c.content.encode("utf-8"), prior[1] if prior else None)
                        check_still_authorized(c.path)
            except BaseException as e:
                roll_back(f"write failed: {e!r}")
                raise

            try:
                sha = commit_paths(repo, in_commit, [*message, f"Daedalus-Change: {change_id}"],
                                   add=[rel for rel, st in zip(rels, staged) if st])
            except CommitFailed as e:
                roll_back(e.output or str(e))
                raise
            except BaseException as e:
                roll_back(f"commit interrupted: {e!r}")
                raise
        finally:
            for fd in dirfds:
                os.close(fd)
        queue.finish_changeset(change_id, "applied", commit_sha=sha)
        return sha


def reject(change_id: str, reason: str) -> Proposal:
    if not isinstance(reason, str) or not reason.strip():
        raise ProposalError("a rejection needs a non-empty reason; agents read it on their next turn")
    with write_lock():
        rec = queue.get(change_id)
        if not queue.transition(change_id, "pending", "rejected", reason=reason.strip()):
            raise NotPending(f"proposal {change_id} is {rec.status}, not pending")
        return queue.get(change_id)


def pending(vault: str | None = None) -> list[Proposal]:
    return queue.select(status="pending", vault=vault)


def recent_rejections(vault: str, n: int = 10) -> list[Rejection]:
    return [
        Rejection(p.id, p.rel_path, p.summary, p.status_reason or "", p.updated_at)
        for p in queue.select(status="rejected", vault=vault, newest_first=True, limit=n)
    ]


def diff(change_id: str) -> str:
    """A unified diff for an edit (against the content it was proposed on); the full content for a create."""
    rec = queue.get(change_id)
    if rec.kind == "create":
        return rec.content
    label = f"{rec.vault}:{rec.rel_path}"
    return "".join(
        difflib.unified_diff(
            (rec.base_content or "").splitlines(keepends=True),
            rec.content.splitlines(keepends=True),
            fromfile=f"a/{label}",
            tofile=f"b/{label}",
        )
    )
