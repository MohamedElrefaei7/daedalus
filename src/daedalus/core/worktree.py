"""Agent worktrees: the only place an agent writes code, each on its own agent/<slug> branch.

Each worktree lives at ~/.cache/daedalus/worktrees/<project>/<slug>-<shortid>/,
not beside the repo, so ~/code stays clean. Its branch is agent/<slug>, and
slugs follow the registry's name rules.

  create(session, slug)       git worktree add -b agent/<slug> <path> HEAD, from your checkout
  list_worktrees(project)     slug, branch, path, commits ahead of HEAD, uncommitted changes
  diff(project, slug)         agent/<slug> against its merge base with HEAD
  commit(session, message)    everything in the worktree, onto agent/<slug>; never a vault/ path
  merge(project, slug)        your action only: git merge --no-ff agent/<slug> into your checkout
  drop(project, slug, force)  remove the worktree and the branch; unmerged or uncommitted work needs force

`git add -A` is safe in a worktree, unlike your checkout: only the edit tools
write there (sandboxed runs can't), and vault/ and .git are unwritable. commit
still checks after staging that no staged path is under vault/, and aborts if
one is. Records of what this module created live in
~/.local/state/daedalus/worktrees.json; list reports any worktree git knows
about that isn't recorded. Every change to your checkout's repo (create, merge,
drop) holds the vault write lock, since apply commits into the same repo.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import subprocess
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

from . import queue
from .registry import PROJECT_NAME, Registry
from .session import Session
from .vaultio import write_lock

WORKTREE_ROOT = Path("~/.cache/daedalus/worktrees").expanduser()
BRANCH_PREFIX = "agent/"
TRAILER = "Daedalus-Worktree"
VAULT = "vault"
GIT_TIMEOUT_S = 60.0


class WorktreeError(Exception):
    """A worktree operation was refused or failed; the message says why."""


class MergeConflict(WorktreeError):
    def __init__(self, slug: str, files: list[str]) -> None:
        self.slug, self.files = slug, files
        super().__init__(f"merging {BRANCH_PREFIX}{slug} conflicts in {', '.join(files) or 'unknown files'}; the merge "
                         "was aborted and your checkout is exactly as it was")


@dataclass(frozen=True)
class Record:
    project: str
    slug: str
    branch: str
    path: str
    base: str  # the checkout's HEAD when it was created


@dataclass(frozen=True)
class Created:
    record: Record
    warning: str | None  # set when the checkout had uncommitted changes the worktree won't see


@dataclass(frozen=True)
class Info:
    slug: str | None  # None for a worktree git knows but this module didn't create
    branch: str | None
    path: str
    ahead: int | None  # commits on the branch not in the checkout's HEAD
    dirty: bool | None  # uncommitted changes in the worktree
    state: str  # "ok", "missing" (recorded but gone), or "unrecorded" (git knows it, this module doesn't)


# git ----------------------------------------------------------------------------------------

def _git(repo: Path | str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT_S,
                          env=env)
    if check and proc.returncode != 0:
        detail = "\n".join(s for s in (proc.stderr.strip(), proc.stdout.strip()) if s)
        raise WorktreeError(f"git {args[0]} failed (exit {proc.returncode}): {detail}")
    return proc


def _out(repo: Path | str, *args: str) -> str:
    return _git(repo, *args).stdout


def _branch_exists(repo: Path, branch: str) -> bool:
    return _git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False).returncode == 0


def _dirty(repo: Path | str) -> list[str]:
    return [line for line in _out(repo, "status", "--porcelain", "--untracked-files=all").splitlines() if line]


def _ahead(repo: Path, branch: str) -> int:
    return int(_out(repo, "rev-list", "--count", f"HEAD..{branch}").strip())


def _under_vault(rel: str) -> bool:
    return rel == VAULT or rel.startswith(VAULT + "/")


# Records ------------------------------------------------------------------------------------

def _records_file() -> Path:
    return queue.STATE_ROOT / "worktrees.json"


@contextmanager
def _records() -> Iterator[dict[str, dict[str, dict]]]:
    """Read-modify-write the records under an flock; written back atomically."""
    path = _records_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = json.loads(path.read_text()) if path.exists() else {}
        yield data
        tmp = path.with_suffix(f".{secrets.token_hex(4)}.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, path)


def records(project: str) -> dict[str, Record]:
    path = _records_file()
    data = json.loads(path.read_text()) if path.exists() else {}
    return {slug: Record(**r) for slug, r in sorted(data.get(project, {}).items())}


def get(project: str, slug: str) -> Record:
    rec = records(project).get(slug)
    if rec is None:
        known = ", ".join(records(project)) or "none"
        raise WorktreeError(f"no worktree {slug!r} for {project} (worktrees: {known}); /wt new {slug} makes one")
    return rec


def _repo(registry: Registry, project: str) -> Path:
    if project not in registry.projects:
        raise WorktreeError(f"project {project!r} isn't registered")
    return registry.repos[project]


# create -------------------------------------------------------------------------------------

def check_slug(slug: str) -> str:
    if not isinstance(slug, str) or not PROJECT_NAME.fullmatch(slug):
        raise WorktreeError(f"slug {slug!r} must follow the project name rules: letters, digits, '-' and '_', "
                            "starting with a letter or digit")
    return slug


def create(session: Session, slug: str) -> Created:
    """A new worktree of the active project on agent/<slug>, starting from your checkout's HEAD."""
    check_slug(slug)
    project, repo = session.active, session.active_repo
    branch = BRANCH_PREFIX + slug
    with write_lock():
        if slug in records(project):
            raise WorktreeError(f"{project} already has a worktree {slug!r} ({records(project)[slug].path})")
        if _branch_exists(repo, branch):
            raise WorktreeError(f"branch {branch} already exists in {repo}; pick another slug")
        head = _git(repo, "rev-parse", "--verify", "-q", "HEAD", check=False).stdout.strip()
        if not head:
            raise WorktreeError(f"{repo} has no commits yet, so there's nothing to start a worktree from")
        root = Path(os.path.realpath(WORKTREE_ROOT)) / project
        path = root / f"{slug}-{secrets.token_hex(3)}"
        if os.path.lexists(path):
            raise WorktreeError(f"{path} already exists")
        warning = None
        if _dirty(repo):
            warning = (f"{repo} has uncommitted changes. The worktree starts from HEAD ({head[:7]}) and won't see "
                       "them.")
        root.mkdir(parents=True, exist_ok=True)
        _git(repo, "worktree", "add", "-q", "-b", branch, str(path), "HEAD")
        rec = Record(project, slug, branch, str(path), head)
        try:
            with _records() as data:
                data.setdefault(project, {})[slug] = asdict(rec)
        except BaseException:
            _git(repo, "worktree", "remove", "--force", str(path), check=False)
            _git(repo, "branch", "-D", branch, check=False)
            raise
    return Created(rec, warning)


# list ---------------------------------------------------------------------------------------

def _git_worktrees(repo: Path) -> list[dict[str, str]]:
    """`git worktree list --porcelain`, parsed: one dict per worktree (worktree, HEAD, branch, ...)."""
    out, cur = [], {}
    for line in _out(repo, "worktree", "list", "--porcelain").splitlines():
        if not line:
            if cur:
                out.append(cur)
            cur = {}
            continue
        key, _, value = line.partition(" ")
        cur[key] = value
    if cur:
        out.append(cur)
    return out


def list_worktrees(registry: Registry, project: str) -> list[Info]:
    repo = _repo(registry, project)
    recs = records(project)
    main = os.path.realpath(repo)
    known = {os.path.realpath(w["worktree"]): w for w in _git_worktrees(repo)}
    rows: list[Info] = []
    for slug, rec in recs.items():
        here = os.path.realpath(rec.path) in known and Path(rec.path).is_dir()
        if not here:
            rows.append(Info(slug, rec.branch, rec.path, None, None, "missing"))
            continue
        ahead = _ahead(repo, rec.branch) if _branch_exists(repo, rec.branch) else None
        rows.append(Info(slug, rec.branch, rec.path, ahead, bool(_dirty(rec.path)), "ok"))
    recorded = {os.path.realpath(r.path) for r in recs.values()}
    for path, w in sorted(known.items()):
        if path == main or path in recorded:
            continue
        branch = w.get("branch", "").removeprefix("refs/heads/") or None
        rows.append(Info(None, branch, path, None, None, "unrecorded"))
    return rows


# diff, commit -------------------------------------------------------------------------------

def diff(registry: Registry, project: str, slug: str) -> str:
    """The unified diff of agent/<slug> against its merge base with your checkout's HEAD."""
    rec = get(project, slug)
    return _out(_repo(registry, project), "diff", "--no-color", "--no-ext-diff", f"HEAD...{rec.branch}")


def active_record(session: Session) -> Record:
    if session.worktree is None:
        raise WorktreeError("no worktree is active; /code <slug> enters one")
    for rec in records(session.active).values():
        if os.path.realpath(rec.path) == str(session.worktree):
            return rec
    raise WorktreeError(f"{session.worktree} isn't a worktree this module created for {session.active}")


def uncommitted(session: Session) -> list[str]:
    return _dirty(active_record(session).path)


def changes(project: str, slug: str) -> list[str]:
    """The worktree's uncommitted changes, as `git status --porcelain` lines; empty if clean or missing."""
    rec = get(project, slug)
    return _dirty(rec.path) if Path(rec.path).is_dir() else []


def commit(session: Session, message: str) -> str:
    """Commit everything in the active worktree onto agent/<slug>; returns the commit's sha."""
    rec = active_record(session)
    wt = rec.path
    message = (message or "").strip()
    if not message:
        raise WorktreeError("a commit needs a message")
    _git(wt, "add", "-A")
    staged = [p for p in _out(wt, "diff", "--cached", "--name-only", "-z", "--no-renames").split("\0") if p]
    bad = [p for p in staged if _under_vault(p)]
    if bad:
        _git(wt, "reset", "-q")  # unstage everything; the working tree is left as it was
        raise WorktreeError(f"refusing to commit: {', '.join(bad)} under {VAULT}/ were staged. The worktree's "
                            f"{VAULT}/ is off limits (rule 4); nothing was committed")
    if not staged:
        raise WorktreeError(f"nothing to commit in {rec.branch}")
    _git(wt, "commit", "-q", "-m", message, "-m", f"{TRAILER}: {rec.slug}")
    return _out(wt, "rev-parse", "HEAD").strip()


# merge, drop --------------------------------------------------------------------------------

def merge(registry: Registry, project: str, slug: str) -> str:
    """Your action: merge agent/<slug> into your checkout with --no-ff; returns the merge commit's sha.

    Refused if the checkout has uncommitted changes, if the branch has nothing
    to merge, or if it touches vault/. A conflict aborts the merge, leaving the
    checkout exactly as it was, and names the conflicting files.
    """
    rec = get(project, slug)
    repo = _repo(registry, project)
    with write_lock():
        dirty = _dirty(repo)
        if dirty:
            raise WorktreeError(f"{repo} has uncommitted changes ({len(dirty)} paths, e.g. {dirty[0][3:]}); commit "
                                "or stash them first, so a merge can't mix with them")
        if _ahead(repo, rec.branch) == 0:
            raise WorktreeError(f"{rec.branch} has no commits that aren't already in HEAD")
        touched = [p for p in _out(repo, "diff", "--name-only", "-z", "--no-renames", f"HEAD...{rec.branch}").split("\0")
                   if p]
        if any(_under_vault(p) for p in touched):
            raise WorktreeError(f"{rec.branch} changes {VAULT}/ paths, which agents never write; not merged")
        before = _out(repo, "rev-parse", "HEAD").strip()
        proc = _git(repo, "merge", "--no-ff", "--no-edit", "-m", f"merge: {rec.branch}", "-m", f"{TRAILER}: {slug}",
                    rec.branch, check=False)
        if proc.returncode != 0:
            files = [p for p in _out(repo, "diff", "--name-only", "--diff-filter=U").splitlines() if p]
            _git(repo, "merge", "--abort", check=False)
            if _out(repo, "rev-parse", "HEAD").strip() != before or _dirty(repo):
                raise WorktreeError(f"merging {rec.branch} failed and the abort didn't restore {repo}; check "
                                    f"`git status` there.\n{proc.stderr.strip()}")
            if files:
                raise MergeConflict(slug, files)
            raise WorktreeError(f"merging {rec.branch} failed; your checkout is as it was.\n{proc.stderr.strip()}")
        return _out(repo, "rev-parse", "HEAD").strip()


def drop(registry: Registry, project: str, slug: str, force: bool = False) -> Record:
    """Remove the worktree and delete agent/<slug>. Unmerged commits or uncommitted changes need ``force``."""
    rec = get(project, slug)
    repo = _repo(registry, project)
    with write_lock():
        exists = Path(rec.path).is_dir()
        if not force:
            if _branch_exists(repo, rec.branch) and _ahead(repo, rec.branch):
                n = _ahead(repo, rec.branch)
                raise WorktreeError(f"{rec.branch} has {n} commit(s) not merged into HEAD; /wt merge {slug} first, "
                                    f"or /wt drop {slug} --force to throw them away")
            if exists and _dirty(rec.path):
                raise WorktreeError(f"the {slug} worktree has uncommitted changes; commit them (/code {slug}, then "
                                    f"/code off) or /wt drop {slug} --force to throw them away")
        if exists:
            _git(repo, "worktree", "remove", *(["--force"] if force else []), rec.path)
        _git(repo, "worktree", "prune")
        if _branch_exists(repo, rec.branch):
            _git(repo, "branch", "-D", rec.branch)
        with _records() as data:
            data.get(project, {}).pop(slug, None)
            if not data.get(project):
                data.pop(project, None)
    return rec
