"""daedalus.toml: the project's committed sandbox settings, read from your own checkout.

    [sandbox]
    allow = ["pytest", "ruff"]   # programs agents may run
    timeout_s = 300
    allow_localhost = false      # true only if tests need to bind local sockets

It's read from the project repo root (your checkout), never from a worktree,
so code an agent edits can't widen what it may run. No file means the project
can't run commands. Unknown keys are errors, like config.toml's.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .errors import SandboxConfigError

FILENAME = "daedalus.toml"
MAX_TIMEOUT_S = 3600
EXAMPLE = '[sandbox]\nallow = ["pytest", "ruff"]   # programs agents may run\ntimeout_s = 300\nallow_localhost = false\n'


@dataclass(frozen=True)
class ProjectConfig:
    allow: tuple[str, ...]
    timeout_s: int = 300
    allow_localhost: bool = False


def load_config(repo: str | os.PathLike[str]) -> ProjectConfig:
    path = Path(repo) / FILENAME
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise SandboxConfigError(
            f"{path} doesn't exist, so this project can't run commands. To allow some, commit a "
            f"{FILENAME} at the repo root:\n\n{EXAMPLE}") from None
    except tomllib.TOMLDecodeError as e:
        raise SandboxConfigError(f"{path}: not valid TOML: {e}") from None
    return parse(data, path)


def parse(data: dict, where: object = FILENAME) -> ProjectConfig:
    extra = sorted(set(data) - {"sandbox"})
    if extra:
        raise SandboxConfigError(f"{where}: unknown table or key {', '.join(extra)}; only [sandbox] is allowed")
    sb = data.get("sandbox")
    if not isinstance(sb, dict):
        raise SandboxConfigError(f"{where}: needs a [sandbox] table")
    extra = sorted(set(sb) - {"allow", "timeout_s", "allow_localhost"})
    if extra:
        raise SandboxConfigError(f"{where}: unknown key {', '.join(extra)} in [sandbox] "
                                 "(the keys are allow, timeout_s, allow_localhost)")
    allow = sb.get("allow")
    if not isinstance(allow, list) or not all(isinstance(a, str) for a in allow):
        raise SandboxConfigError(f"{where}: sandbox.allow must be a list of program names")
    for name in allow:
        if not name or "/" in name or "\0" in name or name in (".", ".."):
            raise SandboxConfigError(f"{where}: sandbox.allow entry {name!r} must be a bare program name, "
                                     "with no '/'; it's looked up only in the project venv's bin/")
    timeout = sb.get("timeout_s", 300)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= MAX_TIMEOUT_S:
        raise SandboxConfigError(f"{where}: sandbox.timeout_s must be a whole number of seconds, 1 to {MAX_TIMEOUT_S}")
    localhost = sb.get("allow_localhost", False)
    if not isinstance(localhost, bool):
        raise SandboxConfigError(f"{where}: sandbox.allow_localhost must be true or false")
    return ProjectConfig(tuple(dict.fromkeys(allow)), timeout, localhost)
