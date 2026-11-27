"""The session: which project is active, which are pulled in, and the worktree.

The session is always passed in explicitly. Core functions never read
state.json, so every one is testable without ~/.config; front ends build a
Session from it with daedalus.core.state.load_session.
"""

from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass
from enum import Enum
from functools import cached_property
from pathlib import Path

from .registry import SHARED_NAME, Registry


class SessionError(ValueError):
    """The session names an unknown project or an unusable worktree."""


class Area(Enum):
    """Where a resolved path lives. Every path falls in exactly one area."""

    ACTIVE_VAULT = "active vault"
    PULLED_VAULT = "pulled vault"
    SHARED_VAULT = "shared vault"
    ACTIVE_REPO = "active repo"  # the repo root minus its vault/
    WORKTREE = "worktree"
    OUTSIDE = "outside"


def normalize_protected(paths) -> tuple[str, ...]:
    """Protected paths as clean worktree-relative POSIX paths; refused if absolute or escaping with '..'."""
    if isinstance(paths, str):
        raise SessionError("protected must be a list of paths, not one string")
    out = []
    for raw in paths or ():
        if not isinstance(raw, str) or not raw.strip():
            raise SessionError(f"protected entry {raw!r} must be a non-empty path")
        if raw.startswith("/") or os.path.isabs(raw):
            raise SessionError(f"protected entry {raw!r} must be relative to the repo root")
        norm = posixpath.normpath(raw.strip())
        if norm == ".." or norm.startswith("../") or norm == ".":
            raise SessionError(f"protected entry {raw!r} must name a path inside the repo")
        out.append(norm + ("/" if raw.rstrip().endswith("/") else ""))
    return tuple(dict.fromkeys(out))


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


@dataclass(frozen=True)
class Session:
    registry: Registry
    active: str
    pulled: tuple[str, ...] = ()
    worktree: Path | None = None
    # Worktree-relative paths a goal run protects (commit 10c): the edit tools refuse them, rule 6. An entry
    # ending in "/" protects everything under it.
    protected: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        projects = self.registry.projects
        known = ", ".join(sorted(projects)) or "none"
        if self.active not in projects:
            raise SessionError(f"active project {self.active!r} is not registered (registered: {known})")
        object.__setattr__(self, "pulled", tuple(self.pulled))
        for name in self.pulled:
            if name not in projects:
                raise SessionError(f"pulled project {name!r} is not registered (registered: {known})")
        if self.active in self.pulled:
            raise SessionError(f"project {self.active!r} is active, so it can't also be pulled")
        if len(set(self.pulled)) != len(self.pulled):
            raise SessionError(f"pulled projects repeat a name: {self.pulled}")
        object.__setattr__(self, "protected", normalize_protected(self.protected))
        if self.protected and self.worktree is None:
            raise SessionError("protected paths apply only to a worktree")
        if self.worktree is not None:
            wt = Path(os.path.realpath(self.worktree))
            if not wt.is_dir():
                raise SessionError(f"worktree {wt} is not a directory")
            if _overlaps(wt, self.active_repo):
                raise SessionError(
                    f"worktree {wt} overlaps your own checkout {self.active_repo}; it must be a separate checkout"
                )
            for vault in [self.registry.shared, *projects.values()]:
                if _overlaps(wt, vault):
                    raise SessionError(f"worktree {wt} overlaps the registered vault {vault}")
            object.__setattr__(self, "worktree", wt)

    @property
    def active_vault(self) -> Path:
        return self.registry.projects[self.active]

    @property
    def active_repo(self) -> Path:
        return self.registry.repos[self.active]

    @property
    def scope(self) -> tuple[tuple[str, Path], ...]:
        """The vaults this session reads: active, then pulled, then shared."""
        projects = self.registry.projects
        return (
            (self.active, projects[self.active]),
            *((name, projects[name]) for name in self.pulled),
            (SHARED_NAME, self.registry.shared),
        )

    def vault_root(self, name: str) -> Path:
        for vault, root in self.scope:
            if vault == name:
                return root
        raise KeyError(f"vault {name!r} is not in this session's scope")

    @cached_property
    def areas(self) -> tuple[tuple[str, Area, str | None], ...]:
        """(root, area, vault name), most specific first.

        Vaults come before the worktree and the active repo, so a path in the
        active vault is classified as the vault, not as the repo around it.
        """
        projects = self.registry.projects
        roots: list[tuple[str, Area, str | None]] = [(str(projects[self.active]), Area.ACTIVE_VAULT, self.active)]
        roots += [(str(projects[name]), Area.PULLED_VAULT, name) for name in self.pulled]
        roots.append((str(self.registry.shared), Area.SHARED_VAULT, SHARED_NAME))
        if self.worktree is not None:
            roots.append((str(self.worktree), Area.WORKTREE, None))
        roots.append((str(self.active_repo), Area.ACTIVE_REPO, None))
        return tuple(roots)
