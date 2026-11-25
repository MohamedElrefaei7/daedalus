"""Fixtures for daedalus.core.

Vault fixtures are copies of the real templates/project-vault/ and
templates/shared-vault/, plus notes, so fixtures can never drift from the
templates. `world` uses bare .git/ folders (fast; nothing commits); `gworld`
makes real git repos with a test user, for tests that write and commit.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from daedalus.core import index, queue
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


@pytest.fixture(scope="session", autouse=True)
def _hermetic_git():
    """Ignore the user's global and system git config (hooks paths, signing, ...).

    Child processes inherit this through os.environ.
    """
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("GIT_CONFIG_GLOBAL", os.devnull)
        mp.setenv("GIT_CONFIG_NOSYSTEM", "1")
        yield


@pytest.fixture(autouse=True)
def state_root(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh queue (and write lock) per test, so tests never touch ~/.local/state."""
    root = tmp_path_factory.mktemp("state")
    monkeypatch.setattr(queue, "STATE_ROOT", root)
    return root


def frontmatter(note_type: str, **overrides: Any) -> dict[str, Any]:
    keys = SCHEMA["base"] + SCHEMA["extra"].get(note_type, [])
    fm = {k: (note_type if k == "type" else DEFAULTS[k]) for k in keys}
    for k, v in overrides.items():
        if v is DROP:
            fm.pop(k, None)
        else:
            fm[k] = v
    return fm


def note_text(note_type: str | None = None, body: str = "", **fm: Any) -> str:
    """A note's full text; with ``note_type`` it gets valid frontmatter for that type."""
    if note_type is None:
        return body
    dumped = yaml.safe_dump(frontmatter(note_type, **fm), sort_keys=False, default_flow_style=None)
    return f"---\n{dumped}---\n\n{body}"


def write_note(vault: Path, rel: str, note_type: str | None = None, body: str = "", **fm: Any) -> Path:
    """Write a note; with ``note_type`` it gets valid frontmatter for that type."""
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(note_text(note_type, body, **fm), encoding="utf-8")
    return path


def spawn(target, args_list: list[tuple], results_timeout: float = 120.0) -> list:
    """Run ``target(*args, results)`` in one spawned process per args tuple; return what they put."""
    import multiprocessing as mp

    ctx = mp.get_context("spawn")
    results = ctx.Queue()
    procs = [ctx.Process(target=target, args=(*args, results)) for args in args_list]
    for proc in procs:
        proc.start()
    out = [results.get(timeout=results_timeout) for _ in procs]
    for proc in procs:
        proc.join(timeout=30)
        assert proc.exitcode == 0, f"worker exited with {proc.exitcode}"
    return out


def write_raw(vault: Path, rel: str, text: str) -> Path:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def git_init(repo: Path) -> None:
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Daedalus Test")
    git(repo, "config", "user.email", "test@daedalus.invalid")
    git(repo, "config", "commit.gpgsign", "false")


def git_commit_all(repo: Path, message: str = "fixture") -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def make_project(root: Path, name: str, real_git: bool = False) -> Path:
    """A repo whose vault/ is a copy of the project template; returns the vault."""
    repo = root / name
    repo.mkdir(parents=True)
    if real_git:
        git_init(repo)
    else:
        (repo / ".git").mkdir()
        (repo / ".git" / "config").write_text("[core]\n")
    vault = repo / "vault"
    shutil.copytree(TEMPLATES / "project-vault", vault)
    return vault


def make_shared(root: Path, real_git: bool = False) -> Path:
    shared = root / "shared"
    shutil.copytree(TEMPLATES / "shared-vault", shared)
    if real_git:
        git_init(shared)
    else:
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

    @property
    def vaults(self) -> dict[str, Path]:
        return {"shared": self.shared, "alpha": self.alpha, "beta": self.beta, "gamma": self.gamma}

    @property
    def repos(self) -> dict[str, Path]:
        return {"shared": self.shared, "alpha": self.alpha_repo, "beta": self.beta.parent, "gamma": self.gamma.parent}

    def snapshot(self) -> dict[str, Any]:
        """Every vault's digest, every repo's HEAD, and every queue record's status."""
        return {
            "digests": {name: vault_digest(v) for name, v in self.vaults.items()},
            "heads": {name: head(r) for name, r in self.repos.items()},
            "queue": queue.statuses(),
        }


def vault_digest(vault: Path) -> str:
    """Hash every file in ``vault`` except .git/ and git-ignored files (needs a real repo)."""
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=vault, check=True, capture_output=True,
    ).stdout
    h = hashlib.sha256()
    for rel in sorted({p for p in out.split(b"\0") if p}):
        path = vault / os.fsdecode(rel)
        h.update(rel + b"\0")
        if path.is_symlink():
            h.update(b"L" + os.fsencode(os.readlink(path)))
        elif path.is_file():
            h.update(b"F" + path.read_bytes())
        else:
            h.update(b"MISSING")
    return h.hexdigest()


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def world(tmp_path: Path) -> World:
    return build_world(tmp_path, real_git=False)


@pytest.fixture(scope="session")
def _golden_git_world(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("golden") / "w"
    root.mkdir()
    build_world(root, real_git=True)
    return root


@pytest.fixture
def gworld(tmp_path: Path, _golden_git_world: Path) -> World:
    """Like ``world``, but every repo is a real git repo with one commit holding everything.

    Built once per session and copied per test: a copied repo is a full repo,
    and copying is ~10x faster than four git inits and commits.
    """
    root = tmp_path / "w"
    shutil.copytree(_golden_git_world, root, symlinks=True)
    return open_world(root)


def build_world(tmp_path: Path, real_git: bool) -> World:
    root = Path(os.path.realpath(tmp_path))
    shared = make_shared(root, real_git)
    alpha = make_project(root, "alpha", real_git)
    beta = make_project(root, "beta", real_git)
    gamma = make_project(root, "gamma", real_git)
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
    if real_git:
        for repo in (shared, alpha.parent, beta.parent, gamma.parent):
            git_commit_all(repo)

    return open_world(root)


def open_world(root: Path) -> World:
    """Register the vaults laid out under ``root`` and open the standard session."""
    root = Path(os.path.realpath(root))
    shared, alpha, beta, gamma = root / "shared", root / "alpha" / "vault", root / "beta" / "vault", root / "gamma" / "vault"
    worktree = root / "alpha-wt"
    registry_path = write_registry(root / "vaults.toml", shared, {"alpha": alpha, "beta": beta, "gamma": gamma})
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
