"""Part B: registry loading and validation, and the Session."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import make_project, make_shared, write_raw, write_registry

from daedalus.core.registry import RegistryError, load_registry
from daedalus.core.session import Session, SessionError


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path.resolve()


def load(root: Path, shared, projects):
    return load_registry(write_registry(root / "vaults.toml", shared, projects))


# Happy path ----------------------------------------------------------------

def test_loads_resolved_paths_and_repo_roots(root: Path) -> None:
    shared = make_shared(root)
    alpha = make_project(root, "alpha")
    reg = load(root, shared, {"alpha": alpha})
    assert reg.shared == shared
    assert dict(reg.projects) == {"alpha": alpha}
    assert dict(reg.repos) == {"alpha": alpha.parent}


def test_expands_tilde(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(root))
    shared = make_shared(root)
    alpha = make_project(root, "alpha")
    reg = load(root, "~/shared", {"alpha": "~/alpha/vault"})
    assert reg.shared == shared and reg.projects["alpha"] == alpha


def test_resolves_symlinked_vault_path(root: Path) -> None:
    shared = make_shared(root)
    alpha = make_project(root, "alpha")
    (root / "link").symlink_to(alpha.parent)
    reg = load(root, shared, {"alpha": root / "link" / "vault"})
    assert reg.projects["alpha"] == alpha


# Every RegistryError condition -------------------------------------------------

def test_missing_registry_file(root: Path) -> None:
    with pytest.raises(RegistryError, match="registry file not found"):
        load_registry(root / "nope.toml")


def test_malformed_toml(root: Path) -> None:
    (root / "vaults.toml").write_text("shared = \n")
    with pytest.raises(RegistryError, match="not valid TOML"):
        load_registry(root / "vaults.toml")


def test_registered_path_does_not_exist(root: Path) -> None:
    shared = make_shared(root)
    with pytest.raises(RegistryError, match=r"projects\.ghost.*does not exist"):
        load(root, shared, {"ghost": root / "ghost" / "vault"})


def test_registered_shared_path_does_not_exist(root: Path) -> None:
    alpha = make_project(root, "alpha")
    with pytest.raises(RegistryError, match=r"shared.*does not exist"):
        load(root, root / "nowhere", {"alpha": alpha})


def test_registered_path_is_not_a_vault_root(root: Path) -> None:
    # The commit-1 mistake: registering the repo instead of its vault/.
    shared = make_shared(root)
    alpha = make_project(root, "alpha")
    with pytest.raises(RegistryError, match=r"projects\.alpha.*is not a vault root \(no AGENTS\.md\)"):
        load(root, shared, {"alpha": alpha.parent})


def test_obsidian_at_repo_root_fails(root: Path) -> None:
    shared = make_shared(root)
    alpha = make_project(root, "alpha")
    (alpha.parent / ".obsidian").mkdir()
    with pytest.raises(RegistryError, match=r"\.obsidian exists.*opened as an Obsidian vault by mistake"):
        load(root, shared, {"alpha": alpha})


def test_obsidian_in_intermediate_parent_fails(root: Path) -> None:
    shared = make_shared(root)
    alpha = make_project(root, "alpha")
    nested = alpha.parent / "docs" / "vault"
    nested.parent.mkdir()
    alpha.rename(nested)
    (nested.parent / ".obsidian").mkdir()
    with pytest.raises(RegistryError, match=r"docs/\.obsidian exists"):
        load(root, shared, {"alpha": nested})


def test_obsidian_above_repo_root_is_not_checked(root: Path) -> None:
    # Only parents up to and including the repo root count.
    (root / ".obsidian").mkdir()
    shared = make_shared(root)
    alpha = make_project(root, "alpha")
    assert load(root, shared, {"alpha": alpha}).projects["alpha"] == alpha


def test_obsidian_inside_the_vault_is_fine(root: Path) -> None:
    shared = make_shared(root)
    alpha = make_project(root, "alpha")
    write_raw(alpha, ".obsidian/app.json", "{}")
    assert load(root, shared, {"alpha": alpha}).projects["alpha"] == alpha


def test_two_names_same_path(root: Path) -> None:
    shared = make_shared(root)
    alpha = make_project(root, "alpha")
    with pytest.raises(RegistryError, match=r"projects\.a and projects\.b both resolve to"):
        load(root, shared, {"a": alpha, "b": alpha.parent / "." / "vault"})


def test_two_names_same_path_through_symlink(root: Path) -> None:
    shared = make_shared(root)
    alpha = make_project(root, "alpha")
    (root / "alias").symlink_to(alpha.parent)
    with pytest.raises(RegistryError, match="both resolve to"):
        load(root, shared, {"a": alpha, "b": root / "alias" / "vault"})


def test_shared_also_registered_as_project(root: Path) -> None:
    shared = make_shared(root)
    with pytest.raises(RegistryError, match=r"projects\.s is the shared vault"):
        load(root, shared, {"s": shared})


def test_project_without_git_repo(root: Path) -> None:
    shared = make_shared(root)
    alpha = make_project(root, "alpha")
    (alpha.parent / ".git" / "config").unlink()
    (alpha.parent / ".git").rmdir()
    with pytest.raises(RegistryError, match=r"no parent of .* contains \.git"):
        load(root, shared, {"alpha": alpha})


def test_reserved_project_name(root: Path) -> None:
    shared = make_shared(root)
    alpha = make_project(root, "alpha")
    with pytest.raises(RegistryError, match="reserved for the shared vault"):
        load(root, shared, {"shared": alpha})


def test_nested_vaults(root: Path) -> None:
    shared = make_shared(root)
    alpha = make_project(root, "alpha")
    inner = alpha / "inbox" / "inner"
    write_raw(inner, "AGENTS.md", "# inner\n")
    with pytest.raises(RegistryError, match="is nested inside"):
        load(root, shared, {"alpha": alpha, "inner": inner})


def test_relative_path_refused(root: Path) -> None:
    alpha = make_project(root, "alpha")
    with pytest.raises(RegistryError, match="must be an absolute path"):
        load(root, "shared", {"alpha": alpha})


# Session ----------------------------------------------------------------------

def test_session_valid(world) -> None:
    assert world.session.active_vault == world.alpha
    assert [name for name, _ in world.session.scope] == ["alpha", "beta", "shared"]


def test_session_active_must_be_registered(world) -> None:
    with pytest.raises(SessionError, match="active project 'nope' is not registered"):
        Session(world.registry, active="nope")


def test_session_pulled_must_be_registered(world) -> None:
    with pytest.raises(SessionError, match="pulled project 'nope' is not registered"):
        Session(world.registry, active="alpha", pulled=("nope",))


def test_session_active_not_also_pulled(world) -> None:
    with pytest.raises(SessionError, match="can't also be pulled"):
        Session(world.registry, active="alpha", pulled=("alpha",))


def test_session_worktree_not_inside_own_checkout(world) -> None:
    inside = world.alpha_repo / "wt"
    inside.mkdir()
    with pytest.raises(SessionError, match="overlaps your own checkout"):
        Session(world.registry, active="alpha", worktree=inside)


def test_session_worktree_must_exist(world) -> None:
    with pytest.raises(SessionError, match="is not a directory"):
        Session(world.registry, active="alpha", worktree=world.root / "missing")


def test_session_is_frozen(world) -> None:
    with pytest.raises(AttributeError):
        world.session.active = "beta"
