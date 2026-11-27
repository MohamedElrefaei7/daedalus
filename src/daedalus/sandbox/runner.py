"""run(): every command an agent triggers runs here, sandboxed, or not at all.

run(session, argv, worktree), in order. Every refusal comes before any spawn
that it can stop:
  1. Load daedalus.toml from your checkout (missing: refused, with how to add it).
  2. Rule 7's shape and name checks (no venv needed).
  3. The worktree must be the session's worktree or the active repo, and not
     the home folder or above it.
  4. Make sure the venv for the current uv.lock exists (`uv sync --frozen`,
     unsandboxed, only when the lock's hash is new).
  5. Rule 7's pin: the program is <venv>/bin/<name>, or it's refused.
  6. Preflight, once per process: `sandbox-exec -p "(version 1)(allow default)"
     /usr/bin/true`. If it fails, this and every later run refuse: Daedalus
     never runs a command unsandboxed.
  7. A fresh run folder (mkdtemp, realpath) holding the profile and tmp/,
     the only writable place, which is also HOME and TMPDIR.
  8. Execute `/usr/bin/sandbox-exec -f <profile> -D ... -- <pinned> <args>` in
     exec form, cwd=worktree, in its own session, with an environment built from
     scratch, rlimits (CPU = timeout_s, open files, 512 MB file size), and the
     timeout. A profile that fails to load (exit 65 or 71 with a
     "sandbox-exec:" message) is SandboxUnavailable, never a result.
  9. Keep the last 200 KB of stdout and stderr, and delete the run folder.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ..core.policy import authorize_command, check_argv
from ..core.session import Session
from . import profile, venv as venvs
from .config import ProjectConfig, load_config
from .errors import SandboxError, SandboxUnavailable
from .exec import Completed, Executor, Limits, spawn

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
PREFLIGHT = [SANDBOX_EXEC, "-p", "(version 1)(allow default)", "/usr/bin/true"]
UNAVAILABLE = "sandbox unavailable — Daedalus won't run commands unsandboxed"
ENV_KEYS = ("PATH", "HOME", "TMPDIR", "LANG", "PYTHONDONTWRITEBYTECODE", "PYTEST_ADDOPTS")

_preflight: str | None = None  # None: not run yet; "" passed; otherwise why it failed (sticky)
_uv_python_dir: str | None = None


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    stdout_tail: str
    stderr_tail: str
    duration_s: float
    timed_out: bool


def child_env(venv: Path, run_tmp: Path) -> dict[str, str]:
    """The child's whole environment. Nothing is inherited from the host."""
    return {
        "PATH": f"{venv}/bin:/usr/bin:/bin",
        "HOME": str(run_tmp),
        "TMPDIR": str(run_tmp),
        "LANG": "en_US.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_ADDOPTS": "-p no:cacheprovider",
    }


def preflight(executor: Executor) -> None:
    global _preflight
    if _preflight is None:
        if not os.access(SANDBOX_EXEC, os.X_OK):
            _preflight = f"{SANDBOX_EXEC} is missing"
        else:
            try:
                done = executor(PREFLIGHT, cwd=Path("/"), env={"PATH": "/usr/bin:/bin"}, timeout_s=10, limits=None)
                _preflight = "" if done.exit_code == 0 and not done.timed_out else (
                    f"the preflight exited {done.exit_code}: {done.stderr_tail.strip()[:300]}")
            except OSError as e:
                _preflight = f"the preflight couldn't start: {e}"
    if _preflight:
        raise SandboxUnavailable(f"{UNAVAILABLE} ({_preflight})")


def uv_python_dir(executor: Executor) -> str:
    """`uv python dir`, realpath'd (it may not exist: then uv has no Pythons of its own)."""
    global _uv_python_dir
    if _uv_python_dir is None:
        done = executor(["uv", "python", "dir"], cwd=Path("/"), env=dict(os.environ), timeout_s=30, limits=None)
        out = done.stdout_tail.strip().splitlines()
        if done.exit_code != 0 or not out:
            raise SandboxError(f"`uv python dir` failed ({done.exit_code}): {done.stderr_tail.strip()[:300]}")
        _uv_python_dir = profile.real(out[-1])
    return _uv_python_dir


def check_worktree(session: Session, worktree: str | os.PathLike[str]) -> Path:
    wt = Path(profile.real(worktree))
    allowed = [p for p in (session.worktree, session.active_repo) if p is not None]
    if not any(wt == Path(profile.real(p)) for p in allowed):
        raise SandboxError(f"{wt} isn't this session's worktree or the active repo "
                           f"({', '.join(str(p) for p in allowed)}); commands run only there")
    home = profile.home()
    if wt == home or wt in home.parents:
        raise SandboxError(f"{wt} contains the home folder, so the sandbox couldn't keep home unreadable")
    if not wt.is_dir():
        raise SandboxError(f"{wt} isn't a directory")
    return wt


def _force_rmtree(path: Path) -> None:
    """Delete the run folder even if the run made parts of it read-only or immutable."""
    def fix(func, p, _exc):
        for target in {p, os.path.dirname(p)}:
            try:
                if hasattr(os, "lchflags"):
                    os.lchflags(target, 0)
                os.chmod(target, stat.S_IRWXU, follow_symlinks=False)
            except (OSError, NotImplementedError):
                pass
        func(p)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=fix)
    else:  # Python 3.11 is supported; onerror is deprecated from 3.12
        shutil.rmtree(path, onerror=fix)


def _profile_failed(done: Completed) -> bool:
    return done.exit_code in (65, 71) and done.stderr_tail.lstrip().startswith("sandbox-exec:")


def run(session: Session, argv: Sequence[str], worktree: str | os.PathLike[str], *,
        config: ProjectConfig | None = None, executor: Executor | None = None) -> RunResult:
    """Run ``argv`` sandboxed in ``worktree``, for the session's active project. Front ends never pass ``config``."""
    executor = executor or spawn
    config = config if config is not None else load_config(session.active_repo)  # 1
    check_argv(argv, config)  # 2
    wt = check_worktree(session, worktree)  # 3
    venv = venvs.ensure_venv(session.active, session.active_repo, executor)  # 4
    program = authorize_command(argv, config, venv)  # 5
    preflight(executor)  # 6

    home = profile.home()
    pythons = [uv_python_dir(executor)]
    base = venvs.base_python_prefix(venv)
    if base is not None and (base == home or home in base.parents):
        pythons.append(str(base))

    run_dir = Path(profile.real(tempfile.mkdtemp(prefix="daedalus-run-")))  # 7
    try:
        run_tmp = run_dir / "tmp"
        run_tmp.mkdir(mode=0o700)
        inputs = profile.ProfileInputs(str(home), str(wt), str(venv), str(run_tmp), tuple(pythons),
                                       config.allow_localhost)
        profile_file = run_dir / "profile.sb"
        profile_file.write_text(profile.render(inputs))
        done = executor(profile.sandbox_argv(profile_file, inputs, program, list(argv[1:])), cwd=wt,  # 8
                        env=child_env(venv, run_tmp), timeout_s=config.timeout_s, limits=Limits(config.timeout_s))
    finally:
        _force_rmtree(run_dir)  # 9
    if _profile_failed(done):
        raise SandboxUnavailable(f"{UNAVAILABLE} (the profile didn't load: {done.stderr_tail.strip()[:300]})")
    return RunResult(done.exit_code, done.stdout_tail, done.stderr_tail, round(done.duration_s, 3), done.timed_out)

