"""Live sandbox tests: the real sandbox-exec, the real scratch2 venv. Run with `uv run pytest -m sandbox`.

They never skip: if sandbox-exec is missing or a profile won't load, run()
raises SandboxUnavailable and the test fails. Each test runs a small Python
snippet (or pytest) through the real runner, in a throwaway worktree holding a
copy of scratch2's project files, with scratch2's venv (built from its uv.lock
on first use). Snippets print one JSON line of findings, and never print a
secret: the Keychain probe reports only whether a value came back.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import statistics
import subprocess
import textwrap
import time
import uuid
from pathlib import Path

import pytest
from core_conftest import tree_hash

from daedalus.core.registry import load_registry
from daedalus.core.session import Session
from daedalus.sandbox import runner, venv
from daedalus.sandbox.config import ProjectConfig
from daedalus.sandbox.exec import spawn

pytestmark = pytest.mark.sandbox

PROJECT = "scratch2"
HOME = Path(os.path.realpath(Path.home()))
CONFIG = ProjectConfig(allow=("python", "pytest"), timeout_s=60)


class Live:
    def __init__(self, session: Session, worktree: Path) -> None:
        self.session, self.worktree = session, worktree

    def run(self, argv, config: ProjectConfig = CONFIG) -> runner.RunResult:
        return runner.run(self.session, argv, self.worktree, config=config)

    def py(self, snippet: str, config: ProjectConfig = CONFIG) -> tuple[dict, runner.RunResult]:
        name = f"probe_{uuid.uuid4().hex[:8]}.py"
        (self.worktree / name).write_text(textwrap.dedent(snippet))
        result = self.run(["python", name], config)
        lines = [line for line in result.stdout_tail.splitlines() if line.startswith("{")]
        assert lines, f"the snippet printed no findings:\n{result.stdout_tail}\n{result.stderr_tail}"
        return json.loads(lines[-1]), result


@pytest.fixture
def live(tmp_path) -> Live:
    registry = load_registry()
    repo = registry.repos[PROJECT]
    wt = Path(os.path.realpath(tmp_path)) / "wt"
    (wt / "tests").mkdir(parents=True)
    for rel in ("pyproject.toml", "uv.lock", ".python-version", "tests/test_smoke.py"):
        shutil.copy2(repo / rel, wt / rel)
    (wt / "vault").mkdir()
    (wt / "vault" / "note.md").write_text("# a vault copy in the worktree\n")
    (wt / ".git").write_text("gitdir: /nonexistent\n")
    return Live(Session(registry, PROJECT, worktree=wt), wt)


def test_the_sandbox_is_available(live) -> None:
    runner.preflight(spawn)  # raises SandboxUnavailable, failing the test, if it isn't


# 6. No network ----------------------------------------------------------------------------------

NETWORK = """
import json, socket
out = {}
def probe(name, fn):
    try:
        fn(); out[name] = "ALLOWED"
    except Exception as e:
        out[name] = type(e).__name__
probe("connect", lambda: socket.create_connection(("1.1.1.1", 443), timeout=3))
probe("dns", lambda: socket.getaddrinfo("api.deepseek.com", 443))
def loopback():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen()
    socket.create_connection(s.getsockname(), timeout=2)
probe("loopback", loopback)
print(json.dumps(out))
"""


def test_no_network_and_no_dns(live) -> None:
    found, _ = live.py(NETWORK)
    assert found["connect"] != "ALLOWED", "a sandboxed run reached 1.1.1.1"
    assert found["dns"] != "ALLOWED", "a sandboxed run resolved api.deepseek.com"
    assert found["loopback"] != "ALLOWED", "loopback is reachable without allow_localhost"


def test_allow_localhost_allows_loopback_only(live) -> None:
    found, _ = live.py(NETWORK, ProjectConfig(allow=("python",), timeout_s=60, allow_localhost=True))
    assert found["loopback"] == "ALLOWED"
    assert found["connect"] != "ALLOWED" and found["dns"] != "ALLOWED"


# 7. No writes outside the run folder ------------------------------------------------------------

def test_no_writes_outside_the_run_folder(live) -> None:
    outside = HOME / "daedalus-sandbox-probe"
    assert not outside.exists()
    vpath = venv.venv_path(PROJECT, live.session.active_repo)
    before = tree_hash(live.worktree)
    found, _ = live.py(f"""
        import json, os
        out = {{}}
        for name, path in [("worktree", "probe.txt"), ("home", {str(outside)!r}),
                           ("venv", {str(vpath / "lib" / "planted.py")!r}), ("tmp", os.environ["TMPDIR"] + "/ok.txt")]:
            try:
                with open(path, "w") as f: f.write("x")
                out[name] = "WROTE"
            except Exception as e:
                out[name] = type(e).__name__
        print(json.dumps(out))
    """)
    probe_file = next(live.worktree.glob("probe_*.py"))
    try:
        assert found["worktree"] != "WROTE", "a run wrote into the worktree"
        assert found["home"] != "WROTE", "a run wrote into the home folder"
        assert found["venv"] != "WROTE", "a run wrote into the project venv"
        assert found["tmp"] == "WROTE", f"a run couldn't write its own TMPDIR: {found}"
        assert not outside.exists() and not (vpath / "lib" / "planted.py").exists()
        probe_file.unlink()
        assert tree_hash(live.worktree) == before, "the worktree changed"
    finally:
        outside.unlink(missing_ok=True)
        (vpath / "lib" / "planted.py").unlink(missing_ok=True)


# 8. No reads in home ----------------------------------------------------------------------------

def test_no_reads_in_home(live) -> None:
    probe = HOME / ".daedalus-sandbox-read-probe"
    registry = load_registry()
    note = next(p for p in sorted(registry.projects["scratch"].rglob("*.md")) if ".obsidian" not in p.parts)
    state = HOME / ".config" / "daedalus" / "state.json"
    assert state.is_file() and note.is_file()
    probe.write_text("not for sandboxed eyes\n")
    libc = ctypes.CDLL(None, use_errno=True)
    assert libc.setxattr(str(probe).encode(), b"com.daedalus.probe", b"secret", 6, 0, 0) == 0
    try:
        found, _ = live.py(f"""
            import ctypes, json, os
            out = {{}}
            def probe(name, fn):
                try:
                    fn(); out[name] = "READ"
                except Exception as e:
                    out[name] = type(e).__name__
            probe("probe", lambda: open({str(probe)!r}).read())
            probe("state", lambda: open({str(state)!r}).read())
            probe("note", lambda: open({str(note)!r}).read())
            probe("list_home", lambda: os.listdir({str(HOME)!r}))
            probe("worktree_vault", lambda: open("vault/note.md").read())
            probe("worktree_git", lambda: open(".git").read())
            libc = ctypes.CDLL(None, use_errno=True)
            buf = ctypes.create_string_buffer(16)
            out["xattr"] = "READ" if libc.getxattr({str(probe).encode()!r}, b"com.daedalus.probe", buf, 16, 0, 0) >= 0 \\
                else "denied"
            probe("stat", lambda: os.stat({str(probe)!r}))
            probe("worktree", lambda: open("pyproject.toml").read())
            print(json.dumps(out))
        """)
    finally:
        probe.unlink()
    for name in ("probe", "state", "note", "list_home", "worktree_vault", "worktree_git", "xattr", "stat"):
        assert found[name] != "READ", f"a sandboxed run could read {name}: {found}"
    assert found["worktree"] == "READ", found


def test_no_metadata_reads_in_home(live) -> None:
    """Even stat is denied under home, outside pytest's upward search for its config files."""
    probe = HOME / ".daedalus-sandbox-meta-probe"
    probe.write_text("its existence is none of the run's business\n")
    try:
        found, _ = live.py(f"""
            import json, os
            try:
                os.stat({str(probe)!r}); out = "STAT OK"
            except Exception as e:
                out = type(e).__name__
            print(json.dumps({{"stat": out}}))
        """)
    finally:
        probe.unlink()
    assert found["stat"] == "PermissionError", f"a sandboxed run could stat {probe}: {found}"


# 9. No Keychain ---------------------------------------------------------------------------------

def test_no_keychain(live) -> None:
    """As an attacker would: HOME is put back to the real home first, since the clean env's HOME isn't a defense.

    The interpreter is the one granted "Always Allow", so unsandboxed this read
    succeeds silently. The probe reports only whether a value came back.
    """
    found, _ = live.py(f"""
        import json, os, subprocess
        os.environ["HOME"] = {str(HOME)!r}
        out = {{}}
        try:
            import keyring
            value = keyring.get_password("daedalus", "deepseek")
            out["keyring"] = "GOT A VALUE" if value else "None"
        except Exception as e:
            out["keyring"] = "raised " + type(e).__name__
        # /usr/bin/security isn't on the item's access list: if the item is reachable at all, it would ask
        # with a GUI prompt. So it's tried only when keyring already failed to reach the item.
        if out["keyring"] == "GOT A VALUE":
            out["security_exit"] = "skipped: keyring already reached the item"
        else:
            try:
                r = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", "daedalus", "-a", "deepseek",
                                    "-w"], capture_output=True, timeout=20, env=os.environ)
                out["security_exit"] = r.returncode
                out["security_printed"] = bool(r.stdout.strip())
            except subprocess.TimeoutExpired:
                out["security_exit"] = "TIMEOUT: a Keychain prompt may be waiting"
        print(json.dumps(out))
    """)
    assert found["keyring"] == "None" or found["keyring"].startswith("raised "), f"the Keychain was reachable: {found}"
    assert isinstance(found["security_exit"], int) and found["security_exit"] != 0, found
    assert found["security_printed"] is False, found


# 10. A timeout kills the whole group ------------------------------------------------------------

def test_a_timeout_kills_the_whole_group(live) -> None:
    marker = f"daedalus-timeout-{uuid.uuid4().hex}"
    (live.worktree / f"{marker}.py").write_text(textwrap.dedent(f"""
        import subprocess, sys, time
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "{marker}"])
        print("pids", child.pid, flush=True)
        time.sleep(120)
    """))
    start = time.monotonic()
    result = live.run(["python", f"{marker}.py"], ProjectConfig(allow=("python",), timeout_s=3))
    assert result.timed_out and time.monotonic() - start < 10
    assert "pids" in result.stdout_tail
    left = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
    assert left.returncode == 1 and left.stdout == "", f"left running: {left.stdout}"


# 11. Real pytest --------------------------------------------------------------------------------

def test_real_pytest_passes_and_fails(live) -> None:
    ok = live.run(["pytest", "-q"])
    assert ok.exit_code == 0 and "1 passed" in ok.stdout_tail, ok
    (live.worktree / "tests" / "test_fails.py").write_text(
        'def test_deliberately_fails() -> None:\n    assert 2 + 2 == 5, "deliberate failure for the sandbox check"\n')
    bad = live.run(["pytest", "-q"])
    assert bad.exit_code == 1, bad
    assert "deliberate failure for the sandbox check" in bad.stdout_tail and "1 failed, 1 passed" in bad.stdout_tail


def test_real_pytest_in_a_checkout_under_home() -> None:
    """The throwaway worktrees live outside home; this one is scratch2's own checkout, so pytest's
    upward config search really walks through home. The checkout is read-only to the run."""
    registry = load_registry()
    session = Session(registry, PROJECT)
    repo = session.active_repo
    assert HOME in repo.parents
    before = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout
    result = runner.run(session, ["pytest", "-q"], repo)
    assert result.exit_code == 0 and re.search(r"\b\d+ passed\b", result.stdout_tail), result  # however many
    after = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout
    assert after == before


def test_the_child_sees_only_the_allowlisted_environment(live, monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-TESTKEY")
    found, _ = live.py("import json, os\nprint(json.dumps({'keys': sorted(os.environ)}))\n")
    assert found["keys"] == sorted(runner.ENV_KEYS)


def test_pytest_counts_from_a_real_run(live) -> None:
    """The loop's baseline: collected, skipped, and xfailed, parsed from real sandboxed pytest output."""
    from daedalus.core.goals import parse_pytest

    (live.worktree / "tests" / "test_counts.py").write_text(
        "import pytest\n\n\ndef test_passes():\n    assert True\n\n\ndef test_fails():\n    assert 1 == 2\n\n\n"
        "@pytest.mark.skip(reason='later')\ndef test_skipped():\n    pass\n\n\n"
        "@pytest.mark.xfail(reason='known')\ndef test_xfailed():\n    assert False\n")
    result = live.run(["pytest", "-q"])
    counts = parse_pytest(result.stdout_tail)
    assert result.exit_code == 1 and counts is not None, result.stdout_tail
    assert (counts.collected, counts.passed, counts.failed, counts.skipped, counts.xfailed) == (5, 2, 1, 1, 1)


# 12. Overhead -----------------------------------------------------------------------------------

def test_overhead_of_the_sandbox(live, capsys) -> None:
    vpath = venv.ensure_venv(PROJECT, live.session.active_repo, spawn)
    sandboxed, plain = [], []
    for _ in range(5):
        start = time.monotonic()
        assert live.run(["pytest", "-q"]).exit_code == 0
        sandboxed.append(time.monotonic() - start)
    tmp = live.worktree.parent / "plain-tmp"
    tmp.mkdir()
    for _ in range(5):
        start = time.monotonic()
        done = spawn([str(vpath / "bin" / "pytest"), "-q"], cwd=live.worktree, env=runner.child_env(vpath, tmp),
                     timeout_s=60, limits=None)
        assert done.exit_code == 0
        plain.append(time.monotonic() - start)
    s, p = statistics.median(sandboxed), statistics.median(plain)
    with capsys.disabled():
        print(f"\n[overhead] pytest -q, median of 5: sandboxed {s * 1000:.0f} ms, unsandboxed {p * 1000:.0f} ms, "
              f"overhead {(s - p) * 1000:+.0f} ms")
    assert s < p + 5, "the sandbox added more than 5 s"
