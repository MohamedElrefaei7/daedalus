"""The code tools: reads follow the worktree; edit_code's exactly-once rule; write_code and delete_code."""

from __future__ import annotations

import os
import stat

import pytest
from conftest import write_raw

from daedalus.core import tools
from daedalus.core.policy import PolicyError
from daedalus.core.session import Session
from daedalus.core.tools import CodeEditError, EditLog, delete_code, edit_code, read_code, write_code


def checkout_session(w) -> Session:
    return Session(w.registry, "alpha", pulled=("beta",))


# 4. Reads follow the worktree -----------------------------------------------------------------

def test_read_code_returns_the_worktrees_edit_and_the_checkouts_without_one(world) -> None:
    write_raw(world.alpha_repo, "src/main.py", "print('checkout')\n")
    edit_code(world.session, "src/main.py", "print('hi')", "print('edited')")
    assert read_code(world.session, "src/main.py").text == "print('edited')\n"
    assert read_code(checkout_session(world), "src/main.py").text == "print('checkout')\n"


def test_list_files_follows_the_worktree_and_leaves_out_vault(gworld) -> None:
    from conftest import git

    wt = gworld.worktree
    (wt / ".git").unlink()  # the world's fake worktree: make it a real repo for ls-files
    git(wt, "init", "-q")
    write_code(gworld.session, "src/added.py", "x = 1\n")
    files = tools.list_files(gworld.session).files
    assert "src/added.py" in files and "src/main.py" in files
    assert not any(f.startswith("vault/") for f in files)
    assert "src/added.py" not in tools.list_files(checkout_session(gworld)).files


# 5. edit_code matching ------------------------------------------------------------------------

def test_zero_and_two_matches_are_refused_and_the_file_is_unchanged(world) -> None:
    target = world.worktree / "src" / "twice.py"
    write_raw(world.worktree, "src/twice.py", "x = 1\ny = 2\nx = 1\n")
    before = target.read_bytes()
    with pytest.raises(CodeEditError, match="old matches 0 times"):
        edit_code(world.session, "src/twice.py", "z = 3", "z = 4")
    with pytest.raises(CodeEditError, match="old matches 2 times"):
        edit_code(world.session, "src/twice.py", "x = 1", "x = 9")
    assert target.read_bytes() == before


def test_exactly_one_match_is_replaced_and_the_mode_is_kept(world) -> None:
    target = world.worktree / "bin" / "tool.sh"
    write_raw(world.worktree, "bin/tool.sh", "#!/bin/sh\necho one\necho two\n")
    target.chmod(0o755)
    log = EditLog()
    edit = edit_code(world.session, "bin/tool.sh", "echo one", "echo uno", log)
    assert target.read_text() == "#!/bin/sh\necho uno\necho two\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert (edit.op, edit.rel_path) == ("edit", "bin/tool.sh") and log.edits == [edit]


def test_edit_code_refuses_empty_old_binary_and_non_utf8(world) -> None:
    write_raw(world.worktree, "src/a.py", "a = 1\n")
    with pytest.raises(CodeEditError, match="non-empty"):
        edit_code(world.session, "src/a.py", "", "x")
    with pytest.raises(CodeEditError, match="NUL byte"):
        edit_code(world.session, "src/a.py", "a = 1", "a = '\0'")
    (world.worktree / "src" / "latin.py").write_bytes("caf\xe9 = 1\n".encode("latin-1"))
    with pytest.raises(CodeEditError, match="isn't valid UTF-8"):
        edit_code(world.session, "src/latin.py", "1", "2")
    (world.worktree / "src" / "blob.bin").write_bytes(b"\x00\x01abc")
    with pytest.raises(CodeEditError, match="NUL byte"):
        edit_code(world.session, "src/blob.bin", "abc", "xyz")
    with pytest.raises(CodeEditError, match="doesn't exist; create it with write_code"):
        edit_code(world.session, "src/missing.py", "a", "b")


# write_code, delete_code ----------------------------------------------------------------------

def test_write_code_creates_folders_and_files_and_logs_it(world) -> None:
    log = EditLog()
    edit = write_code(world.session, "src/pkg/new.py", "def f():\n    return 1\n", log)
    path = world.worktree / "src" / "pkg" / "new.py"
    assert path.read_text() == "def f():\n    return 1\n" and stat.S_IMODE(path.stat().st_mode) == 0o644
    assert edit.op == "create" and edit.before_sha256 is None and log.paths() == ["src/pkg/new.py"]
    again = write_code(world.session, "src/pkg/new.py", "x = 2\n", log)
    assert again.op == "write" and log.paths() == ["src/pkg/new.py"]


def test_write_code_caps_and_refusals(world) -> None:
    with pytest.raises(CodeEditError, match="at most 65,536"):
        write_code(world.session, "big.py", "x" * (64 * 1024 + 1))
    write_raw(world.worktree, "huge.txt", "y" * (64 * 1024 + 10))
    with pytest.raises(CodeEditError, match="too large to replace whole"):
        write_code(world.session, "huge.txt", "small\n")
    with pytest.raises(CodeEditError, match="NUL byte"):
        write_code(world.session, "bin.dat", "a\0b")
    with pytest.raises(CodeEditError, match="UTF-8"):
        write_code(world.session, "bad.py", "\ud800")


def test_delete_code(world) -> None:
    log = EditLog()
    delete_code(world.session, "src/main.py", log)
    assert not (world.worktree / "src" / "main.py").exists() and log.edits[0].op == "delete"
    with pytest.raises(CodeEditError, match="doesn't exist"):
        delete_code(world.session, "src/main.py")


def test_edits_never_reach_the_checkout(world) -> None:
    write_raw(world.alpha_repo, "src/app.py", "print('hi')\n")
    for fn, args in ((write_code, ("x",)), (delete_code, ())):
        with pytest.raises(PolicyError) as exc:
            fn(world.session, str(world.alpha_repo / "src" / "app.py"), *args)
        assert exc.value.rule == 6
    assert (world.alpha_repo / "src" / "app.py").read_text() == "print('hi')\n"


def test_a_symlink_swapped_in_after_authorization_is_restored_and_refused(world, monkeypatch) -> None:
    """The same realpath re-check as apply: if the target stops resolving to the authorized path, undo."""
    write_raw(world.alpha_repo, "src/app.py", "print('hi')\n")
    real_check = tools.check_still_authorized

    def swapped(path):
        os.replace(path, str(path) + ".moved")
        os.symlink(world.alpha_repo / "src" / "app.py", path)
        real_check(path)

    monkeypatch.setattr(tools, "check_still_authorized", swapped)
    with pytest.raises(PolicyError):
        write_code(world.session, "src/main.py", "print('evil')\n")
    assert (world.alpha_repo / "src" / "app.py").read_text() == "print('hi')\n"
