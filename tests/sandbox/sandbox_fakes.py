"""A fake executor for the sandbox unit tests: it records every spawn and runs nothing."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from daedalus.sandbox import runner, venv
from daedalus.sandbox.config import ProjectConfig
from daedalus.sandbox.exec import Completed, Limits

CONFIG = ProjectConfig(allow=("pytest", "ruff"), timeout_s=300)
LOCK = 'version = 1\nrequires-python = ">=3.13"\n'


@dataclass
class Call:
    argv: list[str]
    cwd: Path
    env: dict[str, str]
    timeout_s: float | None
    limits: Limits | None
    profile: str | None  # the -f file's text, read while it still existed

    def param(self, key: str) -> str:
        for a, b in zip(self.argv, self.argv[1:]):
            if a == "-D" and b.startswith(f"{key}="):
                return b.split("=", 1)[1]
        raise KeyError(key)


@dataclass
class FakeExecutor:
    preflight_exit: int = 0
    sync_exit: int = 0
    installed: tuple[str, ...] = ("pytest", "python")
    uv_python_dir: str = "/nonexistent/uv/python"
    result: Completed = field(default_factory=lambda: Completed(0, "1 passed\n", "", 0.25, False))
    calls: list[Call] = field(default_factory=list)

    def __call__(self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout_s: float | None,
                 limits: Limits | None) -> Completed:
        argv = list(argv)
        prof = Path(argv[2]).read_text() if argv[:2] == [runner.SANDBOX_EXEC, "-f"] else None
        self.calls.append(Call(argv, Path(cwd), dict(env), timeout_s, limits, prof))
        if argv == runner.PREFLIGHT:
            return Completed(self.preflight_exit, "", "" if self.preflight_exit == 0 else "sandbox-exec: nope", 0.01,
                             False)
        if argv[:3] == ["uv", "python", "dir"]:
            return Completed(0, self.uv_python_dir + "\n", "", 0.01, False)
        if argv[:2] == ["uv", "sync"]:
            if self.sync_exit == 0:
                make_venv(Path(env["UV_PROJECT_ENVIRONMENT"]), self.installed, ready=False)
            return Completed(self.sync_exit, "", "" if self.sync_exit == 0 else "error: no network", 0.5, False)
        return self.result

    @property
    def sandboxed(self) -> list[Call]:
        return [c for c in self.calls if c.argv[:2] == [runner.SANDBOX_EXEC, "-f"]]

    @property
    def syncs(self) -> list[Call]:
        return [c for c in self.calls if c.argv[:2] == ["uv", "sync"]]


def make_venv(path: Path, installed: tuple[str, ...] = ("pytest", "python"), ready: bool = True,
              home: str = "/opt/fakepython/bin") -> Path:
    (path / "bin").mkdir(parents=True, exist_ok=True)
    for name in installed:
        exe = path / "bin" / name
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
    (path / "pyvenv.cfg").write_text(f"home = {home}\nversion_info = 3.13.5\n")
    if ready:
        (path / venv.READY).write_text("x\n")
    return path


def project(repo: Path, lock: str = LOCK) -> Path:
    """Give ``repo`` the files a project needs to run commands: daedalus.toml and uv.lock."""
    (repo / "daedalus.toml").write_text('[sandbox]\nallow = ["pytest", "ruff"]\ntimeout_s = 300\n')
    (repo / "uv.lock").write_text(lock)
    return repo


def is_real(path: str) -> bool:
    return os.path.realpath(path) == path
