"""register_project / unregister_project: tomlkit edits, validated, atomic."""

from __future__ import annotations

import json

import pytest
from conftest import make_project

from daedalus.core.registry import RegistryError, load_registry, register_project, unregister_project

HAND_WRITTEN = '''# My vaults. Keep scratch first; it's the one I break things in.
shared = "{shared}"   # the practices

[projects]
scratch = "{alpha}"  # trailing comment
beta = "{beta}"
'''


@pytest.fixture
def reg(world):
    path = world.root / "hand.toml"
    path.write_text(HAND_WRITTEN.format(shared=world.shared, alpha=world.alpha, beta=world.beta))
    return path


def test_register_preserves_comments_and_order_byte_for_byte(reg, world) -> None:
    before = reg.read_text()
    register_project("gamma", world.gamma, reg)
    after = reg.read_text()
    assert after.startswith(before.rstrip("\n"))  # everything before is unchanged, byte for byte
    assert after[len(before):].strip() == f'gamma = "{world.gamma}"'
    assert "gamma" in load_registry(reg).projects
    # And the reverse leaves the original bytes exactly.
    unregister_project("gamma", reg, world.root / "no-state.json")
    assert reg.read_text() == before


def test_invalid_register_leaves_the_file_untouched(reg, world, tmp_path) -> None:
    before = reg.read_bytes()
    not_a_vault = tmp_path / "plain"
    not_a_vault.mkdir()
    with pytest.raises(RegistryError) as exc:
        register_project("plain", not_a_vault, reg)
    assert "not a vault root" in str(exc.value) and "registry left unchanged" in str(exc.value)
    assert reg.read_bytes() == before
    with pytest.raises(RegistryError):  # nested inside another vault
        register_project("nested", world.alpha / "inbox", reg)
    with pytest.raises(RegistryError, match="already registered"):
        register_project("beta", world.beta, reg)
    with pytest.raises(RegistryError, match="reserved"):
        register_project("shared", world.gamma, reg)
    with pytest.raises(RegistryError, match="must be letters"):
        register_project("no spaces", world.gamma, reg)
    assert reg.read_bytes() == before
    assert sorted(p.name for p in reg.parent.iterdir() if p.name.endswith(".tmp")) == []


def test_unregister_refuses_the_active_project(reg, world) -> None:
    state = world.root / "state.json"
    state.write_text(json.dumps({"active": "scratch", "pulled": ["beta"]}))
    before = reg.read_bytes()
    with pytest.raises(RegistryError, match="active project"):
        unregister_project("scratch", reg, state)
    with pytest.raises(RegistryError, match="drop it"):
        unregister_project("beta", reg, state)
    with pytest.raises(RegistryError, match="not registered"):
        unregister_project("nope", reg, state)
    assert reg.read_bytes() == before


def test_paths_under_home_are_written_with_tilde(tmp_path, monkeypatch, world) -> None:
    monkeypatch.setenv("HOME", str(world.root))
    reg = world.root / "r.toml"
    reg.write_text(f'shared = "{world.shared}"\n')  # and no [projects] table yet
    register_project("gamma", world.gamma, reg)
    assert 'gamma = "~/gamma/vault"' in reg.read_text()
    assert load_registry(reg).projects["gamma"] == world.gamma
