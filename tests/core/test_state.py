"""daedalus.core.state: state.json -> Session, atomic saves, and the stopgap CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from daedalus.core import state as state_mod
from daedalus.core.registry import RegistryError
from daedalus.core.state import StateError, load_session, read_state, save_state


@pytest.fixture
def paths(world):
    return world.registry_path, world.root / "config" / "state.json"


def write(path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data))


def test_load_session(paths) -> None:
    reg, st = paths
    write(st, {"active": "alpha", "pulled": ["beta"]})
    s = load_session(reg, st)
    assert (s.active, s.pulled) == ("alpha", ("beta",))


def test_pulled_defaults_to_empty(paths) -> None:
    reg, st = paths
    write(st, {"active": "alpha"})
    assert load_session(reg, st).pulled == ()


@pytest.mark.parametrize("data, says", [
    (None, "set-active"),
    ("{not json", "not valid JSON"),
    ([], "JSON object"),
    ({}, "no active project"),
    ({"active": ""}, "no active project"),
    ({"active": 3}, "no active project"),
    ({"active": "alpha", "pulled": "beta"}, "'pulled' must be a list"),
    ({"active": "nope"}, "'nope' is not registered"),
    ({"active": "alpha", "pulled": ["nope"]}, "'nope' is not registered"),
    ({"active": "alpha", "pulled": ["alpha"]}, "can't also be pulled"),
], ids=["missing", "bad_json", "not_object", "no_active", "empty_active", "active_not_str", "pulled_not_list",
        "unregistered_active", "unregistered_pulled", "active_also_pulled"])
def test_bad_state_raises_state_error_with_fix(paths, data, says) -> None:
    reg, st = paths
    if data is not None:
        write(st, data)
    with pytest.raises(StateError) as exc:
        load_session(reg, st)
    assert says in str(exc.value)
    assert "python -m daedalus.state" in str(exc.value)


def test_missing_registry_is_registry_error(paths, tmp_path) -> None:
    _, st = paths
    write(st, {"active": "alpha"})
    with pytest.raises(RegistryError):
        load_session(tmp_path / "none.toml", st)


def test_save_state_validates_before_writing(paths) -> None:
    reg, st = paths
    save_state("alpha", ["beta"], reg, st)
    before = st.read_bytes()
    with pytest.raises(StateError):
        save_state("nope", [], reg, st)
    with pytest.raises(StateError):
        save_state("alpha", ["alpha"], reg, st)
    assert st.read_bytes() == before
    assert json.loads(before) == {"active": "alpha", "pulled": ["beta"]}


def test_save_state_is_atomic(paths, monkeypatch) -> None:
    """A crash between the temp write and the replace leaves the old file and no temp file."""
    reg, st = paths
    save_state("alpha", [], reg, st)
    before = st.read_bytes()

    def crash(*a, **k):
        raise OSError("disk gone")

    monkeypatch.setattr(state_mod.os, "replace", crash)
    with pytest.raises(OSError):
        save_state("beta", [], reg, st)
    assert st.read_bytes() == before
    assert sorted(os.listdir(st.parent)) == ["state.json"]


def test_read_state_round_trips(paths) -> None:
    reg, st = paths
    save_state("gamma", ["alpha", "beta"], reg, st)
    assert read_state(st) == {"active": "gamma", "pulled": ["alpha", "beta"]}


def cli(paths, *args):
    reg, st = paths
    return subprocess.run(
        [sys.executable, "-m", "daedalus.state", "--registry", str(reg), "--state", str(st), *args],
        capture_output=True, text=True,
    )


def test_cli(paths) -> None:
    _, st = paths
    r = cli(paths, "show")
    assert r.returncode == 1 and "set-active" in r.stderr
    assert cli(paths, "set-active", "alpha").returncode == 0
    assert cli(paths, "pull", "beta").returncode == 0
    assert cli(paths, "pull", "beta").returncode == 0  # idempotent
    assert json.loads(st.read_text()) == {"active": "alpha", "pulled": ["beta"]}
    r = cli(paths, "pull", "alpha")
    assert r.returncode == 1 and "active project" in r.stderr
    r = cli(paths, "pull", "nope")
    assert r.returncode == 1 and "not registered" in r.stderr
    # set-active on a pulled project moves it from pulled to active
    assert cli(paths, "set-active", "beta").returncode == 0
    assert json.loads(st.read_text()) == {"active": "beta", "pulled": []}
    r = cli(paths, "drop", "gamma")
    assert r.returncode == 1 and "not pulled" in r.stderr
    cli(paths, "pull", "gamma")
    assert cli(paths, "drop", "gamma").returncode == 0
    r = cli(paths, "show")
    assert r.returncode == 0 and "active:  beta" in r.stdout and "pulled:  (none)" in r.stdout
