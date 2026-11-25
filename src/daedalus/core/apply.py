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
"""

from __future__ import annotations

import difflib
import hashlib
import os
from pathlib import Path
from typing import NamedTuple

from . import queue
from .errors import CommitFailed, NotPending, ProposalError, StaleProposal
from .policy import Op, PolicyError, authorize
from .queue import Proposal
from .registry import SHARED_NAME, Registry
from .session import Session
from .vaultio import (  # module-level names, so tests can patch them here
    atomic_write_at, check_still_authorized, commit_path, exists_at, head_or_none, is_tracked, open_dir_nofollow,
    read_at, restore_at, unstage, write_lock,
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
