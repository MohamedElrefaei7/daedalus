"""Low-level vault writes and git: the write lock, fd-relative file ops, path-limited commits.

Shared by apply (proposals), write_log, and queue reconciliation. Nothing here
decides whether a write is allowed; callers authorize first.
"""

from __future__ import annotations

import fcntl
import os
import secrets
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .errors import CommitFailed, LockBusy
from .policy import PolicyError

GIT_TIMEOUT_S = 120.0
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

_held_here = 0  # how many write_lock() blocks this process is inside


# The write lock -----------------------------------------------------------------------

def lock_path() -> Path:
    from . import queue  # STATE_ROOT is redirected in tests, so read it at call time

    return queue.STATE_ROOT / "write.lock"


@contextmanager
def write_lock(timeout: float = 60.0) -> Iterator[None]:
    """Serialize every vault write across processes.

    An OS lock: fcntl.flock on a lock file. The kernel holds the lock, not the
    file, and releases it when the holder exits, even with os._exit or a crash.
    The file's existence means nothing. timeout=0 tries once.
    """
    global _held_here
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockBusy(f"another daedalus write holds {path}") from None
                time.sleep(0.01)
        _held_here += 1
        try:
            yield
        finally:
            _held_here -= 1
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def lock_held_here() -> bool:
    return _held_here > 0


# File operations relative to a verified directory fd -----------------------------------

def open_dir_nofollow(path: Path) -> int:
    """Open ``path`` (already resolved) one component at a time with O_NOFOLLOW.

    If any folder on the path was swapped for a symlink after authorization,
    this fails instead of following it. Everything after is done relative to
    the returned fd, so a later swap can't redirect the write either.
    """
    fd = os.open("/", _DIR_FLAGS)
    try:
        for part in path.parts[1:]:
            nfd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = nfd
    except OSError as e:
        os.close(fd)
        raise PolicyError(1, path, f"a folder on the path is missing or became a symlink after authorization ({e.strerror})") from None
    return fd


def read_at(dirfd: int, name: str) -> tuple[bytes, int] | None:
    """(content, mode) of a regular file, or None if nothing is there."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dirfd)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise PolicyError(1, name, f"the target is not a regular file ({e.strerror})") from None
    try:
        mode = os.fstat(fd).st_mode
        chunks = []
        while chunk := os.read(fd, 1 << 16):
            chunks.append(chunk)
        return b"".join(chunks), mode
    finally:
        os.close(fd)


def exists_at(dirfd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=dirfd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def atomic_write_at(dirfd: int, name: str, data: bytes, mode: int | None = None) -> None:
    """Write a temp file in the folder, fsync it, then os.replace it onto ``name``."""
    tmp = f".daedalus-{secrets.token_hex(6)}.tmp"  # dot-prefixed: Obsidian and the index ignore it
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666, dir_fd=dirfd)
    try:
        try:
            if mode is not None:
                os.fchmod(fd, mode & 0o7777)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, name, src_dir_fd=dirfd, dst_dir_fd=dirfd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dirfd)
        except FileNotFoundError:
            pass
        raise
    os.fsync(dirfd)


def restore_at(dirfd: int, name: str, previous: tuple[bytes, int] | None) -> None:
    """Put back the previous content, or remove the file if there was none."""
    if previous is None:
        try:
            os.unlink(name, dir_fd=dirfd)
        except FileNotFoundError:
            pass
    else:
        atomic_write_at(dirfd, name, previous[0], previous[1])
    os.fsync(dirfd)


def check_still_authorized(path: Path) -> None:
    if os.path.realpath(path) != str(path):
        raise PolicyError(1, path, f"the target now resolves to {os.path.realpath(path)}, not the authorized path")


# Git ------------------------------------------------------------------------------------

def git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT_S, env=env,
        )
    except subprocess.TimeoutExpired as e:
        raise CommitFailed(f"git {args[0]} timed out after {GIT_TIMEOUT_S:.0f}s", str(e)) from None
    if proc.returncode != 0:
        output = "\n".join(s for s in (proc.stderr.strip(), proc.stdout.strip()) if s)
        raise CommitFailed(f"git {args[0]} failed (exit {proc.returncode})", output)
    return proc.stdout


def head_or_none(repo: Path) -> str | None:
    try:
        return git(repo, "rev-parse", "--verify", "-q", "HEAD").strip() or None
    except CommitFailed:
        return None  # a repo with no commits yet


def commit_path(repo: Path, rel: str, message: list[str], add: bool) -> str:
    """Commit exactly ``rel`` and nothing else; returns the new commit's sha.

    ``git commit --only -- <path>`` commits that path's working-tree content
    and leaves whatever else is staged staged. Never -a, never `git add .`.
    """
    if add:
        git(repo, "add", "--", rel)
    args = ["commit", "-q", "--only"]
    for paragraph in message:
        args += ["-m", paragraph]
    git(repo, *args, "--", rel)
    return git(repo, "rev-parse", "HEAD").strip()


def is_tracked(repo: Path, rel: str) -> bool:
    try:
        git(repo, "ls-files", "--error-unmatch", "--", rel)
        return True
    except CommitFailed:
        return False


def in_head(repo: Path, rel: str) -> bool:
    if head_or_none(repo) is None:
        return False
    return bool(git(repo, "ls-tree", "--name-only", "HEAD", "--", rel).strip())


def unstage(repo: Path, rel: str) -> None:
    """Drop a path we staged from the index (used when a create or untracked edit is rolled back).

    -f is needed once the working file has been restored: git otherwise refuses
    an index entry that differs from both the file and HEAD. With --cached it
    only ever touches the index, never the file.
    """
    try:
        git(repo, "rm", "--cached", "-f", "-q", "--ignore-unmatch", "--", rel)
    except CommitFailed:
        pass


TRAILER = "Daedalus-Change"


def find_commit_with_trailer(repo: Path, change_id: str, since: str | None) -> str | None:
    """The commit on HEAD (after ``since``) whose message carries ``Daedalus-Change: <id>``."""
    if head_or_none(repo) is None:
        return None
    rng = [f"{since}..HEAD"] if since else ["HEAD"]
    out = git(repo, "log", "--format=%H%x00%(trailers:key=" + TRAILER + ",valueonly,separator=%x01)", *rng)
    for line in out.splitlines():
        sha, _, values = line.partition("\0")
        if change_id in values.split("\x01"):
            return sha
    return None
