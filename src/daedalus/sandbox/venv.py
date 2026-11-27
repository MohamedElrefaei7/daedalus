"""The project venv: built from the frozen uv.lock, outside the worktree, keyed by the lock's hash.

    ~/.cache/daedalus/venvs/<project>-<sha12 of uv.lock>/

It's built with `UV_PROJECT_ENVIRONMENT=<that path> uv sync --frozen
--no-install-project`, run from your own checkout of the project repo:
  * outside the sandbox, because it needs the network to download wheels;
  * only when the lock's hash changes (a new path), so it's never rebuilt in place;
  * from the lockfile alone. A dependency change is a design decision in the
    `dependency` category, so the gate puts it in front of the human before it
    can reach uv.lock. The install is trusted only because the lock is approved
    and frozen.

`--no-install-project` keeps the project itself out of the venv: an editable
install would point at your checkout, and tests must import the worktree's code.
A venv counts as built only once `.daedalus-ready` exists, so a sync that died
halfway is removed and redone.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
from pathlib import Path

from .errors import VenvError
from .exec import Executor

VENV_ROOT = Path("~/.cache/daedalus/venvs").expanduser()
READY = ".daedalus-ready"
SYNC_TIMEOUT_S = 900


def venv_root() -> Path:
    return Path(os.path.realpath(VENV_ROOT))


def lock_hash(repo: str | os.PathLike[str]) -> str:
    lock = Path(repo) / "uv.lock"
    try:
        return hashlib.sha256(lock.read_bytes()).hexdigest()[:12]
    except FileNotFoundError:
        raise VenvError(f"{lock} doesn't exist; commit a uv.lock (`uv lock`) so the project venv can be built") \
            from None


def venv_path(project: str, repo: str | os.PathLike[str]) -> Path:
    return venv_root() / f"{project}-{lock_hash(repo)}"


def ensure_venv(project: str, repo: str | os.PathLike[str], executor: Executor) -> Path:
    """The project's venv for its current uv.lock, built first if it doesn't exist yet."""
    path = venv_path(project, repo)
    if (path / READY).is_file():
        return path
    root = venv_root()
    root.mkdir(parents=True, exist_ok=True)
    with open(root / f".{project}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # another terminal may be building the same venv
        if (path / READY).is_file():
            return path
        if os.path.lexists(path):
            shutil.rmtree(path)  # a half-built venv from a sync that died
        env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT")}
        env["UV_PROJECT_ENVIRONMENT"] = str(path)
        done = executor(["uv", "sync", "--frozen", "--no-install-project"], cwd=Path(repo), env=env,
                        timeout_s=SYNC_TIMEOUT_S, limits=None)
        if done.exit_code != 0 or done.timed_out:
            shutil.rmtree(path, ignore_errors=True)
            why = "timed out" if done.timed_out else f"exited {done.exit_code}"
            raise VenvError(f"building the {project} venv from uv.lock {why}:\n{done.stderr_tail[-4000:]}")
        (path / READY).write_text(lock_hash(repo) + "\n")
    return path


def base_python_prefix(venv: Path) -> Path | None:
    """The real install folder of the Python the venv links to (pyvenv.cfg's `home` is its bin/)."""
    try:
        for line in (venv / "pyvenv.cfg").read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "home" and value.strip():
                return Path(os.path.realpath(value.strip())).parent
    except OSError:
        pass
    return None
