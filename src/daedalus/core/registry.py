"""The vault registry: ``~/.config/daedalus/vaults.toml``.

The registry names the shared vault and every project vault. Loading it
validates every path, because the path policy trusts these roots: a wrong root
here would make every later check wrong.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

DEFAULT_REGISTRY = Path("~/.config/daedalus/vaults.toml")

# The shared vault is addressed as "shared" in refs like "shared:practices/x.md",
# so no project may take that name.
SHARED_NAME = "shared"

# Project names appear in refs ("name:rel/path.md"), so keep them simple.
PROJECT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


class RegistryError(Exception):
    """The registry file is missing, malformed, or names a bad vault."""


@dataclass(frozen=True)
class Registry:
    shared: Path
    projects: Mapping[str, Path]  # project name -> vault root
    repos: Mapping[str, Path]  # project name -> repo root (nearest parent with .git)
    source: Path  # the vaults.toml this was loaded from


def load_registry(path: str | os.PathLike[str] | None = None) -> Registry:
    """Load and validate the registry; ``path`` overrides the default location."""
    src = Path(os.path.expanduser(os.fspath(path if path is not None else DEFAULT_REGISTRY)))
    try:
        with open(src, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise RegistryError(f"{src}: registry file not found") from None
    except tomllib.TOMLDecodeError as e:
        raise RegistryError(f"{src}: not valid TOML: {e}") from None

    shared_raw = data.get("shared")
    if not isinstance(shared_raw, str):
        raise RegistryError(f"{src}: key 'shared' must be a path string")
    projects_raw = data.get("projects", {})
    if not isinstance(projects_raw, dict):
        raise RegistryError(f"{src}: key 'projects' must be a table")
    for name, value in projects_raw.items():
        if name == SHARED_NAME:
            raise RegistryError(f"{src}: project name '{SHARED_NAME}' is reserved for the shared vault")
        if not PROJECT_NAME.fullmatch(name):
            raise RegistryError(
                f"{src}: project name {name!r} must be letters, digits, '-' or '_', starting with a letter or digit"
            )
        if not isinstance(value, str):
            raise RegistryError(f"{src}: projects.{name} must be a path string")

    shared = _vault_root(src, "shared", shared_raw)
    projects = {name: _vault_root(src, f"projects.{name}", value) for name, value in projects_raw.items()}
    _check_distinct(src, shared, projects)
    repos = {name: _repo_root(src, name, vault) for name, vault in projects.items()}
    return Registry(
        shared=shared,
        projects=MappingProxyType(projects),
        repos=MappingProxyType(repos),
        source=src,
    )


def _vault_root(src: Path, key: str, raw: str) -> Path:
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        raise RegistryError(f"{src}: {key} = {raw!r} must be an absolute path or start with '~'")
    real = Path(os.path.realpath(expanded))
    if not real.exists():
        raise RegistryError(f"{src}: {key} = {raw!r}: {real} does not exist")
    if not real.is_dir():
        raise RegistryError(f"{src}: {key} = {raw!r}: {real} is not a directory")
    if not (real / "AGENTS.md").is_file():
        raise RegistryError(
            f"{src}: {key} = {raw!r}: {real} is not a vault root (no AGENTS.md). "
            "Register the vault folder itself, not the repo that contains it."
        )
    return real


def _check_distinct(src: Path, shared: Path, projects: dict[str, Path]) -> None:
    named = [("shared", shared)] + [(f"projects.{n}", p) for n, p in projects.items()]
    # Compare by inode as well as by path: on a case-insensitive file system,
    # two differently-cased paths can name the same folder.
    seen: dict[tuple[int, int], str] = {}
    for key, path in named:
        st = path.stat()
        other = seen.get((st.st_dev, st.st_ino))
        if other is not None:
            if other == "shared":
                raise RegistryError(f"{src}: {key} is the shared vault ({path}); the shared vault can't also be a project")
            raise RegistryError(f"{src}: {other} and {key} both resolve to {path}")
        seen[(st.st_dev, st.st_ino)] = key
    # A vault inside another vault would make "which vault is this path in?"
    # ambiguous for the path policy.
    for key_a, a in named:
        for key_b, b in named:
            if key_a != key_b and a in b.parents:
                raise RegistryError(f"{src}: {key_a} ({a}) is nested inside {key_b} ({b})")


def _repo_root(src: Path, name: str, vault: Path) -> Path:
    repo = next((p for p in vault.parents if (p / ".git").exists()), None)
    if repo is None:
        raise RegistryError(
            f"{src}: projects.{name}: no parent of {vault} contains .git, so the project has no repo root"
        )
    # Every folder from the vault's parent up to and including the repo root.
    for parent in vault.parents:
        if (parent / ".obsidian").exists():
            raise RegistryError(
                f"{src}: projects.{name}: {parent / '.obsidian'} exists, so {parent} was opened as an "
                f"Obsidian vault by mistake. Remove it from Obsidian's vault list, delete that .obsidian/ "
                f"folder, and open {vault} instead."
            )
        if parent == repo:
            break
    return repo
