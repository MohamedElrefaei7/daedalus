"""The runner with an injected executor: nothing here is sandboxed for real (see test_live.py)."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from sandbox_fakes import CONFIG, FakeExecutor, is_real, make_venv, project

from daedalus.core.policy import PolicyError
from daedalus.core.session import Session
from daedalus.sandbox import profile, runner, venv
from daedalus.sandbox.config import ProjectConfig, SandboxConfigError, load_config, parse
from daedalus.sandbox.errors import SandboxError, SandboxUnavailable, VenvError
from daedalus.sandbox.exec import Completed


@pytest.fixture
def proj(world, tmp_path, monkeypatch):
    """alpha as a project that may run commands, with its venv root redirected and its venv built."""
    monkeypatch.setattr(venv, "VENV_ROOT", tmp_path / "venvs")
    project(world.alpha_repo)
    make_venv(venv.venv_path("alpha", world.alpha_repo))
    return world


# 1. Profile contents --------------------------------------------------------------------------

def test_profile_denies_network_dns_writes_home_reads_and_securityd(proj) -> None:
    fake = FakeExecutor()
    runner.run(proj.session, ["pytest", "-q"], proj.worktree, executor=fake)
    (call,) = fake.sandboxed
    text = call.profile
    assert text.startswith("(version 1)\n(allow default)\n")
    for rule in ("(deny network*)",
                 '(deny mach-lookup (global-name "com.apple.dnssd.service") (global-name "com.apple.mDNSResponder"))',
                 "(deny file-write*)",
                 '(deny file-read* (subpath (param "HOME")))',
                 '(deny mach-lookup (global-name "com.apple.SecurityServer") (global-name "com.apple.securityd"))',
                 '(deny file-read* (subpath (param "WORKTREE_VAULT")) (subpath (param "WORKTREE_GIT")))',
                 '(allow file-read-metadata (subpath (param "HOME")))'):
        assert rule in text, rule
    assert "localhost" not in text
    # Later rules win: each deny comes before its exceptions, and the worktree is never writable.
    assert text.index("(deny file-write*)") < text.index("(allow file-write*")
    assert text.index('(deny file-read* (subpath (param "HOME")))') < text.index('(allow file-read* (subpath (param "WORKTREE"))')
    writable = text[text.index("(allow file-write*"):].split("\n", 1)[0]
    assert writable == ('(allow file-write* (subpath (param "RUN_TMP")) (literal "/dev/null") (literal "/dev/zero") '
                        '(regex #"^/dev/tty") (subpath "/dev/fd"))')


def test_profile_text_holds_no_paths_and_every_param_is_a_realpath(proj) -> None:
    fake = FakeExecutor()
    runner.run(proj.session, ["pytest"], proj.worktree, executor=fake)
    (call,) = fake.sandboxed
    values = [b.split("=", 1)[1] for a, b in zip(call.argv, call.argv[1:]) if a == "-D"]
    assert {b.split("=", 1)[0] for a, b in zip(call.argv, call.argv[1:]) if a == "-D"} >= {
        "HOME", "WORKTREE", "WORKTREE_VAULT", "WORKTREE_GIT", "VENV", "RUN_TMP", "PYTHON_0"}
    assert all(is_real(v) for v in values), values
    for v in values:
        assert v not in call.profile, f"{v} was pasted into the profile"
    assert "/Users/" not in call.profile and "/private/" not in call.profile


def test_a_worktree_under_tmp_appears_as_private_tmp(world, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(venv, "VENV_ROOT", tmp_path / "venvs")
    project(world.alpha_repo)
    make_venv(venv.venv_path("alpha", world.alpha_repo))
    wt = Path(tempfile.mkdtemp(prefix="daedalus-wt-", dir="/tmp"))
    try:
        assert str(wt).startswith("/tmp/")
        session = Session(world.registry, "alpha", worktree=wt)
        fake = FakeExecutor()
        runner.run(session, ["pytest"], wt, executor=fake)
        (call,) = fake.sandboxed
        assert call.param("WORKTREE") == "/private" + str(wt)
        assert call.param("WORKTREE_VAULT") == f"/private{wt}/vault"
        assert call.cwd == Path("/private" + str(wt))
        assert call.param("RUN_TMP").startswith(("/private/var/", "/private/tmp/"))
    finally:
        shutil.rmtree(wt)


def test_profile_params_resolve_symlinks() -> None:
    inputs = profile.ProfileInputs(home="/tmp", worktree="/tmp/x", venv="/var/v", run_tmp="/tmp/r",
                                   python_dirs=("/tmp/py", "/tmp/py"))
    p = profile.params(inputs)
    assert p["HOME"] == "/private/tmp" and p["VENV"] == "/private/var/v" and p["WORKTREE"] == "/private/tmp/x"
    assert [k for k in p if k.startswith("PYTHON_")] == ["PYTHON_0"]  # duplicates collapse


def test_allow_localhost_reallows_loopback_only() -> None:
    inputs = profile.ProfileInputs("/tmp", "/tmp/x", "/tmp/v", "/tmp/r", (), allow_localhost=True)
    text = profile.render(inputs)
    assert "(deny network*)" in text
    allows = [line for line in text.splitlines() if line.startswith("(allow network")]
    assert allows == ['(allow network-bind (local ip "localhost:*"))',
                      '(allow network-inbound (local ip "localhost:*"))',
                      '(allow network-outbound (remote ip "localhost:*"))']
    assert text.index("(deny network*)") < text.index(allows[0])


def test_the_venvs_base_python_under_home_is_readable(proj, monkeypatch, tmp_path) -> None:
    fake_home = tmp_path / "home"
    (fake_home / ".pyenv" / "versions" / "3.13" / "bin").mkdir(parents=True)
    monkeypatch.setattr(profile, "home", lambda: Path(os.path.realpath(fake_home)))
    vpath = venv.venv_path("alpha", proj.alpha_repo)
    make_venv(vpath, home=str(fake_home / ".pyenv" / "versions" / "3.13" / "bin"))
    fake = FakeExecutor()
    runner.run(proj.session, ["pytest"], proj.worktree, executor=fake)
    (call,) = fake.sandboxed
    assert call.param("PYTHON_0") == "/nonexistent/uv/python"
    assert call.param("PYTHON_1") == os.path.realpath(fake_home / ".pyenv" / "versions" / "3.13")


# 2. No inherited environment --------------------------------------------------------------------

def test_the_child_environment_is_exactly_the_allowlist(proj, monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-TESTKEY")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ssh-agent.sock")
    fake = FakeExecutor()
    runner.run(proj.session, ["pytest"], proj.worktree, executor=fake)
    (call,) = fake.sandboxed
    run_tmp = call.param("RUN_TMP")
    vpath = call.param("VENV")
    assert call.env == {"PATH": f"{vpath}/bin:/usr/bin:/bin", "HOME": run_tmp, "TMPDIR": run_tmp,
                        "LANG": "en_US.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
                        "PYTEST_ADDOPTS": "-p no:cacheprovider"}
    assert tuple(call.env) == runner.ENV_KEYS
    assert not any("sk-TESTKEY" in v or "ssh-agent" in v for v in call.env.values())


def test_exec_form_pinned_program_limits_and_cleanup(proj) -> None:
    fake = FakeExecutor()
    result = runner.run(proj.session, ["pytest", "-q", "tests/test_x.py"], proj.worktree, executor=fake)
    (call,) = fake.sandboxed
    pinned = f"{call.param('VENV')}/bin/pytest"
    assert call.argv[0] == "/usr/bin/sandbox-exec"
    assert call.argv[call.argv.index("--") + 1:] == [pinned, "-q", "tests/test_x.py"]
    assert call.cwd == proj.worktree
    assert call.timeout_s == 300 and call.limits.cpu_s == 300 and call.limits.fsize_bytes == 512 * 1024 * 1024
    assert not Path(call.argv[2]).exists() and not Path(call.param("RUN_TMP")).exists(), "the run folder was kept"
    assert result == runner.RunResult(0, "1 passed\n", "", 0.25, False)


# 3. Rule 7 through the runner: refused, and nothing spawned ---------------------------------------

@pytest.mark.parametrize("argv", [["/tmp/evil/pytest"], ["./pytest"], ["rm", "-rf", "/"], ["ruff", "check"], [],
                                  ["pytest", "-k", "a\0b"], ["pytest", 3], "pytest -q"],
                         ids=["slash", "dot-slash", "not-allowed", "missing-from-venv", "empty", "nul",
                              "non-string", "a-string"])
def test_rule7_refusals_never_call_the_executor(proj, argv) -> None:
    fake = FakeExecutor()
    with pytest.raises(PolicyError) as exc:
        runner.run(proj.session, argv, proj.worktree, executor=fake)
    assert exc.value.rule == 7
    assert fake.calls == []


def test_the_hosts_path_never_matters(proj, tmp_path, monkeypatch) -> None:
    evil = tmp_path / "evil"
    evil.mkdir()
    (evil / "pytest").write_text("#!/bin/sh\necho pwned\n")
    (evil / "pytest").chmod(0o755)
    monkeypatch.setenv("PATH", f"{evil}:{os.environ['PATH']}")
    fake = FakeExecutor()
    runner.run(proj.session, ["pytest"], proj.worktree, executor=fake)
    (call,) = fake.sandboxed
    assert call.argv[call.argv.index("--") + 1] == f"{call.param('VENV')}/bin/pytest"
    assert str(evil) not in " ".join(call.argv) + call.env["PATH"]


def test_commands_run_only_in_the_worktree_or_the_active_repo(proj, tmp_path) -> None:
    other = tmp_path / "elsewhere"
    other.mkdir()
    fake = FakeExecutor()
    with pytest.raises(SandboxError, match="isn't this session's worktree"):
        runner.run(proj.session, ["pytest"], other, executor=fake)
    assert fake.calls == []


def test_a_worktree_containing_home_is_refused(proj) -> None:
    """Allowing reads in the worktree would undo the home denial."""
    session = Session(proj.registry, "alpha", worktree=Path.home())
    fake = FakeExecutor()
    with pytest.raises(SandboxError, match="contains the home folder"):
        runner.run(session, ["pytest"], Path.home(), executor=fake)
    assert fake.calls == []


def test_the_active_repo_is_a_worktree_too(proj) -> None:
    fake = FakeExecutor()
    runner.run(proj.session, ["pytest"], proj.alpha_repo, executor=fake)
    (call,) = fake.sandboxed
    assert call.param("WORKTREE") == str(proj.alpha_repo) and call.param("WORKTREE_VAULT") == str(proj.alpha)


# 4. Fail closed ---------------------------------------------------------------------------------

def test_a_failed_preflight_refuses_every_run_and_never_runs_the_command(proj) -> None:
    fake = FakeExecutor(preflight_exit=1)
    for _ in range(2):
        with pytest.raises(SandboxUnavailable, match="sandbox unavailable — Daedalus won't run commands unsandboxed"):
            runner.run(proj.session, ["pytest"], proj.worktree, executor=fake)
    assert [c.argv for c in fake.calls] == [runner.PREFLIGHT], "the preflight is run once and its failure sticks"
    assert fake.sandboxed == []


def test_missing_sandbox_exec_refuses_without_spawning(proj, monkeypatch) -> None:
    monkeypatch.setattr(runner, "SANDBOX_EXEC", "/nonexistent/sandbox-exec")
    fake = FakeExecutor()
    with pytest.raises(SandboxUnavailable, match="/nonexistent/sandbox-exec is missing"):
        runner.run(proj.session, ["pytest"], proj.worktree, executor=fake)
    assert fake.calls == []


def test_a_profile_that_fails_to_load_is_unavailable_not_a_result(proj) -> None:
    fake = FakeExecutor(result=Completed(65, "", "sandbox-exec: unbound variable: bogus\n", 0.01, False))
    with pytest.raises(SandboxUnavailable, match="the profile didn't load"):
        runner.run(proj.session, ["pytest"], proj.worktree, executor=fake)


def test_a_command_that_itself_exits_65_is_a_result(proj) -> None:
    fake = FakeExecutor(result=Completed(65, "", "error: bad data\n", 0.01, False))
    assert runner.run(proj.session, ["pytest"], proj.worktree, executor=fake).exit_code == 65


def test_preflight_runs_exactly_the_minimal_profile(proj) -> None:
    fake = FakeExecutor()
    runner.run(proj.session, ["pytest"], proj.worktree, executor=fake)
    assert fake.calls[0].argv == ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)", "/usr/bin/true"]


# 5. Venv reuse ----------------------------------------------------------------------------------

def test_the_venv_path_follows_the_lockfile_and_only_the_lockfile(world, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(venv, "VENV_ROOT", tmp_path / "venvs")
    repo = project(world.alpha_repo, lock="version = 1\n# a\n")
    first = venv.venv_path("alpha", repo)
    (repo / "pyproject.toml").write_text("[project]\nname = 'changed'\n")
    (repo / "README.md").write_text("changed\n")
    assert venv.venv_path("alpha", repo) == first, "only uv.lock is an input"
    (repo / "uv.lock").write_text("version = 1\n# b\n")
    second = venv.venv_path("alpha", repo)
    assert second != first and second.parent == first.parent == Path(os.path.realpath(tmp_path / "venvs"))
    assert first.name.startswith("alpha-") and len(first.name) == len("alpha-") + 12


def test_the_venv_is_built_once_per_lock_unsandboxed_from_the_checkout(world, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(venv, "VENV_ROOT", tmp_path / "venvs")
    repo = project(world.alpha_repo)
    fake = FakeExecutor()
    for _ in range(2):
        runner.run(world.session, ["pytest"], world.worktree, executor=fake)
    (sync,) = fake.syncs
    assert sync.argv == ["uv", "sync", "--frozen", "--no-install-project"]
    assert sync.cwd == repo and sync.limits is None
    assert sync.env["UV_PROJECT_ENVIRONMENT"] == str(venv.venv_path("alpha", repo))
    new_lock = "version = 1\n# new deps\n"
    (repo / "uv.lock").write_text(new_lock)
    runner.run(world.session, ["pytest"], world.worktree, executor=fake)
    assert len(fake.syncs) == 2
    assert fake.syncs[1].env["UV_PROJECT_ENVIRONMENT"].endswith(
        "alpha-" + hashlib.sha256(new_lock.encode()).hexdigest()[:12])


def test_a_half_built_venv_is_rebuilt_and_a_failed_sync_leaves_nothing(world, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(venv, "VENV_ROOT", tmp_path / "venvs")
    repo = project(world.alpha_repo)
    path = make_venv(venv.venv_path("alpha", repo), ready=False)
    (path / "junk").write_text("from a sync that died\n")
    venv.ensure_venv("alpha", repo, FakeExecutor())
    assert not (path / "junk").exists() and (path / venv.READY).is_file()

    (repo / "uv.lock").write_text("version = 1\n# other\n")
    with pytest.raises(VenvError, match="exited 1"):
        venv.ensure_venv("alpha", repo, FakeExecutor(sync_exit=1))
    assert not venv.venv_path("alpha", repo).exists()


def test_no_uv_lock_is_refused_before_anything_runs(world, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(venv, "VENV_ROOT", tmp_path / "venvs")
    project(world.alpha_repo)
    (world.alpha_repo / "uv.lock").unlink()
    fake = FakeExecutor()
    with pytest.raises(VenvError, match="uv.lock doesn't exist"):
        runner.run(world.session, ["pytest"], world.worktree, executor=fake)
    assert fake.calls == []


# daedalus.toml ----------------------------------------------------------------------------------

def test_a_missing_daedalus_toml_refuses_and_says_how_to_add_it(world) -> None:
    fake = FakeExecutor()
    with pytest.raises(SandboxConfigError) as exc:
        runner.run(world.session, ["pytest"], world.worktree, executor=fake)
    msg = str(exc.value)
    assert "can't run commands" in msg and "[sandbox]" in msg and 'allow = ["pytest", "ruff"]' in msg
    assert fake.calls == []


def test_the_defaults_parse(world) -> None:
    (world.alpha_repo / "daedalus.toml").write_text(
        '[sandbox]\nallow = ["pytest", "ruff"]   # programs agents may run\ntimeout_s = 300\n'
        "allow_localhost = false      # true only if tests need to bind local sockets\n")
    assert load_config(world.alpha_repo) == ProjectConfig(("pytest", "ruff"), 300, False)
    assert parse({"sandbox": {"allow": []}}) == ProjectConfig((), 300, False)


@pytest.mark.parametrize("data, match", [
    ({"sandbox": {"allow": [], "timeout": 3}}, "unknown key timeout"),
    ({"sandbox": {"allow": []}, "extra": {}}, "unknown table or key extra"),
    ({}, "needs a \\[sandbox\\] table"),
    ({"sandbox": {"allow": "pytest"}}, "must be a list"),
    ({"sandbox": {"allow": [".venv/bin/pytest"]}}, "bare program name"),
    ({"sandbox": {"allow": [""]}}, "bare program name"),
    ({"sandbox": {"allow": [], "timeout_s": 0}}, "1 to 3600"),
    ({"sandbox": {"allow": [], "timeout_s": True}}, "whole number"),
    ({"sandbox": {"allow": [], "allow_localhost": "no"}}, "true or false"),
])
def test_bad_daedalus_toml_is_an_error(data, match) -> None:
    with pytest.raises(SandboxConfigError, match=match):
        parse(data)
