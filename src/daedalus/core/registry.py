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

import tomlkit

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


def obsidian_mistake(folder: Path, vault: Path) -> str:
    """The commit-1 message for a folder that Obsidian opened as a vault by mistake."""
    return (f"{folder / '.obsidian'} exists, so {folder} was opened as an Obsidian vault by mistake. Remove it "
            f"from Obsidian's vault list, delete that .obsidian/ folder, and open {vault} instead.")


def _repo_root(src: Path, name: str, vault: Path) -> Path:
    repo = next((p for p in vault.parents if (p / ".git").exists()), None)
    if repo is None:
        raise RegistryError(
            f"{src}: projects.{name}: no parent of {vault} contains .git, so the project has no repo root"
        )
    # Every folder from the vault's parent up to and including the repo root.
    for parent in vault.parents:
        if (parent / ".obsidian").exists():
            raise RegistryError(f"{src}: projects.{name}: {obsidian_mistake(parent, vault)}")
        if parent == repo:
            break
    return repo


# Writing the registry ---------------------------------------------------------------------
#
# Only register_project and unregister_project write vaults.toml, and only
# /new (daedalus.core.lifecycle) calls them for now. Both edit the file with
# tomlkit, so comments, key order, and every other entry survive byte for
# byte, and both validate the new text with load_registry before an atomic
# replace: a write that would make the registry invalid leaves the old file
# untouched.


def _display_path(path: Path) -> str:
    """``~/...`` for paths under the home folder, as the hand-written entries are; else absolute."""
    home = Path(os.path.expanduser("~"))
    try:
        return "~/" + Path(path).relative_to(home).as_posix()
    except ValueError:
        return str(path)


def _write_validated(src: Path, text: str) -> Registry:
    """Write ``text`` to ``src`` atomically, but only if it loads as a valid registry."""
    import secrets as _secrets

    tmp = src.parent / f".{src.name}.{_secrets.token_hex(6)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        try:
            os.write(fd, text.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            load_registry(tmp)  # the same checks every reader runs
        except RegistryError as e:
            raise RegistryError(str(e).replace(str(tmp), str(src)) + " (registry left unchanged)") from None
        os.chmod(tmp, os.stat(src).st_mode & 0o7777)
        os.replace(tmp, src)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    dirfd = os.open(src.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dirfd)
    finally:
        os.close(dirfd)
    return load_registry(src)


def _registry_file(path: str | os.PathLike[str] | None) -> tuple[Path, "tomlkit.TOMLDocument"]:
    src = Path(os.path.expanduser(os.fspath(path if path is not None else DEFAULT_REGISTRY)))
    try:
        text = src.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise RegistryError(f"{src}: registry file not found") from None
    try:
        return src, tomlkit.parse(text)
    except Exception as e:
        raise RegistryError(f"{src}: not valid TOML: {e}") from None


def register_project(name: str, vault_path: str | os.PathLike[str],
                     registry_path: str | os.PathLike[str] | None = None) -> Registry:
    """Add ``name = "<vault_path>"`` under [projects]; returns the new Registry."""
    src, doc = _registry_file(registry_path)
    if name == SHARED_NAME:
        raise RegistryError(f"{src}: project name '{SHARED_NAME}' is reserved for the shared vault")
    if not PROJECT_NAME.fullmatch(name):
        raise RegistryError(
            f"{src}: project name {name!r} must be letters, digits, '-' or '_', starting with a letter or digit"
        )
    projects = doc.get("projects")
    if projects is None:
        doc["projects"] = projects = tomlkit.table()
    if name in projects:
        raise RegistryError(f"{src}: project {name!r} is already registered ({projects[name]})")
    projects[name] = _display_path(Path(os.path.realpath(vault_path)))
    return _write_validated(src, tomlkit.dumps(doc))


def unregister_project(name: str, registry_path: str | os.PathLike[str] | None = None,
                       state_path: str | os.PathLike[str] | None = None) -> Registry:
    """Remove a project's entry. Refused for the active project, and for a pulled one (drop it first)."""
    from .state import StateError, read_state, state_file

    src, doc = _registry_file(registry_path)
    projects = doc.get("projects")
    if projects is None or name not in projects:
        raise RegistryError(f"{src}: project {name!r} is not registered")
    try:
        state = read_state(state_path)
    except StateError:
        state = None  # no usable state: nothing is active
    if state is not None:
        if state["active"] == name:
            raise RegistryError(f"{name!r} is the active project in {state_file(state_path)}; switch to another "
                                "project before unregistering it")
        if name in state["pulled"]:
            raise RegistryError(f"{name!r} is pulled in {state_file(state_path)}; drop it before unregistering it")
    del projects[name]
    return _write_validated(src, tomlkit.dumps(doc))
