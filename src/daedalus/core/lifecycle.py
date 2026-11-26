"""Project lifecycle: ``/new <name> --repo <path>`` creates a project vault and registers it.

Steps, in order; any failure rolls back every step before it, in reverse:

  1. the name is valid under the registry's rules and not registered yet;
  2. --repo is an existing git repo (its top level), with no .obsidian/ at its root;
  3. <repo>/vault/ doesn't exist, or is empty;
  4. templates/project-vault/ is copied into <repo>/vault/;
  5. exactly the new vault/ paths are committed in the project repo, as
     "init: daedalus vault from template", with a path-limited commit:
     anything already staged or modified there stays exactly as it was;
  6. the project is registered in vaults.toml;
  7. the one manual step (Obsidian can't register a vault by path) is returned
     for the REPL to print.

Rollback: unregister; undo the init commit only if it's still HEAD and touches
only vault/ (a soft reset, then vault/ alone is dropped from the index, so
your own staged work stays staged); remove the copied folder.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import registry as registry_mod
from .registry import PROJECT_NAME, SHARED_NAME, RegistryError, load_registry, obsidian_mistake
from .vaultio import CommitFailed, commit_path, git, head_or_none, write_lock

TEMPLATES = Path(__file__).resolve().parents[3] / "templates"  # the daedalus repo (editable install)
INIT_MESSAGE = "init: daedalus vault from template"
VAULT_DIR = "vault"
_SKIP = shutil.ignore_patterns(".DS_Store")


class LifecycleError(Exception):
    """/new was refused or failed; everything it did has been rolled back."""


@dataclass(frozen=True)
class NewProject:
    name: str
    repo: Path
    vault: Path
    commit_sha: str
    files: tuple[str, ...]  # repo-relative paths in the init commit

    def instructions(self) -> str:
        return (
            f"Registered {self.name!r} at {self.vault} (commit {self.commit_sha[:7]}).\n"
            "\n"
            "One manual step: Obsidian can't register a vault by path. In Obsidian, choose "
            "Open another vault → Open folder as vault, and pick exactly this folder:\n"
            f"    {self.vault}\n"
            f"(the vault/ folder, not {self.repo}). Then, in that vault:\n"
            "  • Settings → Core plugins → turn on Templates; set its template folder location to `templates`.\n"
            "  • Settings → Files and links → Deleted files → Move to system trash.\n"
            "  • Make `created` a Date property: insert a template once, then in Properties click the icon "
            "next to `created` → Property type → Date.\n"
            f"Then /vault {self.name} to switch to it."
        )


def template_files(templates: Path = TEMPLATES) -> list[str]:
    """The project template's files, relative to the template root, sorted."""
    root = templates / "project-vault"
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in filenames:
            if name != ".DS_Store":
                out.append(Path(dirpath, name).relative_to(root).as_posix())
    return sorted(out)


def _check_name(name: str, registry_path) -> None:
    if name == SHARED_NAME:
        raise LifecycleError(f"project name '{SHARED_NAME}' is reserved for the shared vault")
    if not PROJECT_NAME.fullmatch(name or ""):
        raise LifecycleError(f"project name {name!r} must be letters, digits, '-' or '_', starting with a letter or digit")
    try:
        reg = load_registry(registry_path)
    except RegistryError as e:
        raise LifecycleError(str(e)) from None
    if name in reg.projects:
        raise LifecycleError(f"project {name!r} is already registered ({reg.projects[name]})")


def _check_repo(repo_arg: str | os.PathLike[str]) -> Path:
    repo = Path(os.path.realpath(os.path.expanduser(os.fspath(repo_arg))))
    if not repo.is_dir():
        raise LifecycleError(f"--repo {repo} is not a folder")
    try:
        top = git(repo, "rev-parse", "--show-toplevel").strip()
    except CommitFailed:
        raise LifecycleError(f"--repo {repo} is not a git repo; run `git init` there first") from None
    if Path(os.path.realpath(top)) != repo:
        raise LifecycleError(f"--repo {repo} is inside the git repo {top}; pass the repo's top-level folder")
    if (repo / ".obsidian").exists():
        raise LifecycleError(obsidian_mistake(repo, repo / VAULT_DIR))
    return repo


def _check_vault_dir(vault: Path) -> bool:
    """True if vault/ already exists (empty)."""
    if os.path.lexists(vault):
        if vault.is_symlink() or not vault.is_dir():
            raise LifecycleError(f"{vault} exists and isn't a folder")
        if any(vault.iterdir()):
            raise LifecycleError(f"{vault} exists and isn't empty; /new only creates a vault from the template")
        return True
    return False


def copy_template(vault: Path, templates: Path = TEMPLATES) -> None:
    shutil.copytree(templates / "project-vault", vault, ignore=_SKIP, dirs_exist_ok=True)


def commit_vault(repo: Path) -> str:
    """Commit exactly vault/ in ``repo``: git add -- vault, then git commit --only -- vault."""
    return commit_path(repo, VAULT_DIR, [INIT_MESSAGE], add=True)


def _commit_files(repo: Path, sha: str) -> list[str]:
    out = git(repo, "show", "--name-only", "--format=", "-z", sha)
    return sorted(p for p in out.split("\0") if p)


def _undo_commit(repo: Path, parent: str | None) -> str | None:
    """Undo the init commit if it's still HEAD and touches only vault/. Returns why not, or None.

    Tolerates a step 5 that never committed (HEAD is still ``parent``).
    """
    sha = head_or_none(repo)
    if sha is None or sha == parent:
        git(repo, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", VAULT_DIR)  # maybe staged, never committed
        return None
    if git(repo, "log", "-1", "--format=%s", sha).strip() != INIT_MESSAGE:
        return f"HEAD ({sha[:7]}) is no longer the init commit, so it was left in place"
    if any(not p.startswith(VAULT_DIR + "/") for p in _commit_files(repo, sha)):
        return f"the init commit {sha[:7]} touches files outside {VAULT_DIR}/, so it was left in place"
    if parent is None:
        git(repo, "update-ref", "-d", "HEAD")  # it was the repo's first commit: back to an unborn branch
    else:
        git(repo, "reset", "-q", "--soft", parent)  # moves HEAD only; the index keeps your staged work
    git(repo, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", VAULT_DIR)  # drop only vault/ from the index
    return None


def _unregister_if_present(name: str, registry_path, state_path) -> None:
    if name in load_registry(registry_path).projects:
        registry_mod.unregister_project(name, registry_path, state_path)


def new_project(name: str, repo_arg: str | os.PathLike[str], registry_path=None, state_path=None,
                templates: Path = TEMPLATES) -> NewProject:
    """Create <repo>/vault/ from the template, commit it, register it; roll back on any failure.

    Each step's undo is armed before the step runs and tolerates a step that
    only half happened, so a failure anywhere (even mid-step) rolls back fully.
    """
    _check_name(name, registry_path)  # 1
    repo = _check_repo(repo_arg)  # 2
    vault = repo / VAULT_DIR
    existed = _check_vault_dir(vault)  # 3

    def remove_copy() -> None:
        if existed:
            for child in vault.iterdir():
                shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
        else:
            shutil.rmtree(vault, ignore_errors=True)

    undo: list[tuple[str, Callable[[], str | None]]] = []
    with write_lock():
        try:
            undo.append(("remove the copied vault/", remove_copy))
            copy_template(vault, templates)  # 4

            parent = head_or_none(repo)
            undo.append(("undo the init commit", lambda: _undo_commit(repo, parent)))
            sha = commit_vault(repo)  # 5

            undo.append(("unregister", lambda: _unregister_if_present(name, registry_path, state_path)))
            registry_mod.register_project(name, vault, registry_path)  # 6

            made = NewProject(name, repo, vault, sha, tuple(_commit_files(repo, sha)))
        except BaseException as e:
            problems = []
            for what, step in reversed(undo):
                try:
                    note = step()
                    if note:
                        problems.append(note)
                except Exception as undo_error:  # keep undoing the rest
                    problems.append(f"couldn't {what}: {undo_error}")
            detail = f" Rollback: {'; '.join(problems)}." if problems else " Everything was rolled back."
            if isinstance(e, Exception):
                raise LifecycleError(f"/new {name} failed: {e}.{detail}") from e
            raise
    return made  # 7: the REPL prints made.instructions()
