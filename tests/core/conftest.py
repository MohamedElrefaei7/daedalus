"""Fixtures for daedalus.core.

Vault fixtures are copies of the real templates/project-vault/ and
templates/shared-vault/, plus notes, so fixtures can never drift from the
templates.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from daedalus.core import index
from daedalus.core.registry import Registry, load_registry
from daedalus.core.schema import SCHEMA
from daedalus.core.session import Session

REPO = Path(__file__).resolve().parents[2]
TEMPLATES = REPO / "templates"

DROP = object()  # pass as a frontmatter override to remove that key

DEFAULTS: dict[str, Any] = {
    "status": "accepted",
    "created": "2026-11-25",
    "related": [],
    "shared_refs": [],
    "project_refs": [],
    "source": "human",
    "code": [],
    "done_when": "the thing works",
    "max_iterations": 5,
    "budget_usd": 0.5,
    "checkin_every": 2,
    "options": [],
    "answered": False,
    "spec": "",
    "decisions": [],
    "tasks": [],
    "applies_to": [],
}

BENCH_RESULTS: list[str] = []


@pytest.fixture(scope="session", autouse=True)
def _index_cache(tmp_path_factory: pytest.TempPathFactory):
    """Point the search-index cache at a temp dir, so tests never write ~/.cache."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(index, "CACHE_ROOT", tmp_path_factory.mktemp("cache"))
        yield


def frontmatter(note_type: str, **overrides: Any) -> dict[str, Any]:
    keys = SCHEMA["base"] + SCHEMA["extra"].get(note_type, [])
    fm = {k: (note_type if k == "type" else DEFAULTS[k]) for k in keys}
    for k, v in overrides.items():
        if v is DROP:
            fm.pop(k, None)
        else:
            fm[k] = v
    return fm


def write_note(vault: Path, rel: str, note_type: str | None = None, body: str = "", **fm: Any) -> Path:
    """Write a note; with ``note_type`` it gets valid frontmatter for that type."""
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    text = body
    if note_type is not None:
        dumped = yaml.safe_dump(frontmatter(note_type, **fm), sort_keys=False, default_flow_style=None)
        text = f"---\n{dumped}---\n\n{body}"
    path.write_text(text, encoding="utf-8")
    return path


def write_raw(vault: Path, rel: str, text: str) -> Path:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_project(root: Path, name: str) -> Path:
    """A repo (with .git) whose vault/ is a copy of the project template; returns the vault."""
    repo = root / name
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text("[core]\n")
    vault = repo / "vault"
    shutil.copytree(TEMPLATES / "project-vault", vault)
    return vault


def make_shared(root: Path) -> Path:
    shared = root / "shared"
    shutil.copytree(TEMPLATES / "shared-vault", shared)
    (shared / ".git").mkdir()
    return shared


def write_registry(path: Path, shared: Path | str, projects: dict[str, Path | str]) -> Path:
    lines = [f'shared = "{shared}"', "", "[projects]"]
    lines += [f'{name} = "{p}"' for name, p in projects.items()]
    path.write_text("\n".join(lines) + "\n")
    return path


def tree_hash(root: Path) -> str:
    """Hash every path, file type, and file content under ``root``."""
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(dirnames + filenames):
            p = Path(dirpath, name)
            h.update(str(p.relative_to(root)).encode())
            if p.is_symlink():
                h.update(b"L" + os.readlink(p).encode())
            elif p.is_file():
                h.update(b"F" + p.read_bytes())
    return h.hexdigest()


@dataclass
class World:
    root: Path
    shared: Path
    alpha: Path  # active project's vault
    alpha_repo: Path
    beta: Path  # pulled project's vault
    gamma: Path  # registered but not pulled
    worktree: Path
    registry_path: Path
    registry: Registry
    session: Session
    extra: dict[str, Any] = field(default_factory=dict)


@pytest.fixture
def world(tmp_path: Path) -> World:
    root = Path(os.path.realpath(tmp_path))
    shared = make_shared(root)
    alpha = make_project(root, "alpha")
    beta = make_project(root, "beta")
    gamma = make_project(root, "gamma")
    # A vault that Obsidian has opened, as every live vault will be.
    write_raw(alpha, ".obsidian/app.json", "{}\n")

    worktree = root / "alpha-wt"
    write_raw(worktree, "src/main.py", "print('hi')\n")
    write_raw(worktree, "vault/inbox/copy.md", "a vault copy in the worktree\n")
    (worktree / ".git").write_text("gitdir: ../alpha/.git/worktrees/alpha-wt\n")

    write_note(shared, "practices/red-test-first.md", "practice", "# Red test first\n", applies_to=["testing"])
    write_note(
        alpha, "decisions/storage-engine.md", "decision", "# Storage engine\n\nSee [[usgs-fetcher]].\n",
        related=["[[usgs-fetcher]]"], shared_refs=["[[red-test-first]]"],
    )
    write_note(alpha, "components/usgs-fetcher.md", "component", "# USGS fetcher\n", code=["src/fetch.py"])
    write_raw(alpha, "logs/2026-11-25-run-1.md", "# Run 1\n")
    write_note(beta, "concepts/quake-magnitude.md", "concept", "# Quake magnitude\n")
    write_note(gamma, "concepts/secret.md", "concept", "# Not pulled\n")

    registry_path = write_registry(
        root / "vaults.toml", shared, {"alpha": alpha, "beta": beta, "gamma": gamma}
    )
    registry = load_registry(registry_path)
    session = Session(registry, active="alpha", pulled=("beta",), worktree=worktree)
    return World(root, shared, alpha, alpha.parent, beta, gamma, worktree, registry_path, registry, session)


def pytest_terminal_summary(terminalreporter) -> None:
    if BENCH_RESULTS:
        terminalreporter.write_sep("-", "daedalus.core benchmark")
        for line in BENCH_RESULTS:
            terminalreporter.write_line(line)


@pytest.fixture(scope="session")
def bench_results() -> list[str]:
    return BENCH_RESULTS
