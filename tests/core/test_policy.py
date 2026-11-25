"""Part C: the seven-rule path policy. Each refusal asserts its rule number."""

from __future__ import annotations

import os

import pytest

from daedalus.core.policy import Op, PolicyError, authorize, authorize_command
from daedalus.core.session import Area, Session

WRITE_OPS = [op for op in Op if op is not Op.READ]


def refused(path, op, session, rule: int) -> PolicyError:
    with pytest.raises(PolicyError) as exc:
        authorize(path, op, session)
    assert exc.value.rule == rule, f"expected rule {rule}, got rule {exc.value.rule}: {exc.value}"
    return exc.value


# Allow paths: one per op, so the suite can't pass by refusing everything ----

@pytest.mark.parametrize(
    "op,rel",
    [
        (Op.READ, "decisions/storage-engine.md"),
        (Op.PROPOSE, "inbox/new-idea.md"),
        (Op.PROPOSE, "questions/which-db.md"),
        (Op.PROPOSE, "CONTEXT.md"),
        (Op.CREATE_LOG, "logs/2026-11-25-run-2.md"),
        (Op.WRITE_PLAN, "plans/plan-v1.md"),
        (Op.USER_ACTION, "decisions/new.md"),
        (Op.USER_ACTION, "CONTEXT.md"),
    ],
)
def test_allowed_in_active_vault(world, op, rel) -> None:
    auth = authorize(world.alpha / rel, op, world.session)
    assert auth.area is Area.ACTIVE_VAULT and auth.vault == "alpha" and auth.rel == rel
    assert auth.path == world.alpha / rel


def test_allowed_write_code_in_worktree(world) -> None:
    auth = authorize(world.worktree / "src" / "main.py", Op.WRITE_CODE, world.session)
    assert auth.area is Area.WORKTREE and auth.rel == "src/main.py"


@pytest.mark.parametrize("op,rel", [(Op.READ, "practices/red-test-first.md"), (Op.PROPOSE, "inbox/x.md"),
                                    (Op.USER_ACTION, "practices/x.md"), (Op.USER_ACTION, "lessons/x.md")])
def test_allowed_in_shared_vault(world, op, rel) -> None:
    auth = authorize(world.shared / rel, op, world.session)
    assert auth.area is Area.SHARED_VAULT and auth.vault == "shared"


@pytest.mark.parametrize("where", ["beta", "repo", "worktree"])
def test_read_allowed_in_pulled_repo_and_worktree(world, where) -> None:
    path = {"beta": world.beta / "concepts" / "quake-magnitude.md",
            "repo": world.alpha_repo / "README.md",
            "worktree": world.worktree / "src" / "main.py"}[where]
    assert authorize(path, Op.READ, world.session).area in (Area.PULLED_VAULT, Area.ACTIVE_REPO, Area.WORKTREE)


# Rule 1: location ----------------------------------------------------------------

def test_rule1_traversal_out_of_vault(world) -> None:
    refused(f"{world.alpha}/inbox/../../../outside.md", Op.READ, world.session, 1)


def test_rule1_outside_path(world) -> None:
    refused("/etc/hosts", Op.READ, world.session, 1)


def test_rule1_relative_path(world) -> None:
    refused("inbox/x.md", Op.PROPOSE, world.session, 1)


def test_rule1_registered_but_not_pulled_vault(world) -> None:
    refused(world.gamma / "concepts" / "secret.md", Op.READ, world.session, 1)


def test_rule1_symlink_escaping_vault(world) -> None:
    outside = world.root / "outside"
    outside.mkdir()
    (world.alpha / "inbox" / "escape").symlink_to(outside)
    refused(world.alpha / "inbox" / "escape" / "x.md", Op.PROPOSE, world.session, 1)


def test_rule1_dangling_symlink_escaping_vault(world) -> None:
    # A write through a dangling symlink would create its target, so the
    # target is what gets checked.
    (world.alpha / "inbox" / "dangling.md").symlink_to(world.root / "nowhere" / "new.md")
    refused(world.alpha / "inbox" / "dangling.md", Op.PROPOSE, world.session, 1)


@pytest.mark.parametrize("op", [Op.PROPOSE, Op.USER_ACTION, Op.CREATE_LOG, Op.WRITE_PLAN])
def test_rule1_repo_is_read_only(world, op) -> None:
    refused(world.alpha_repo / "README.md", op, world.session, 1)


@pytest.mark.parametrize("op", [Op.PROPOSE, Op.USER_ACTION, Op.CREATE_LOG, Op.WRITE_PLAN])
def test_rule1_worktree_takes_only_code(world, op) -> None:
    refused(world.worktree / "src" / "notes.md", op, world.session, 1)


def test_rule1_no_worktree_in_session(world) -> None:
    session = Session(world.registry, active="alpha")
    refused(world.worktree / "src" / "main.py", Op.WRITE_CODE, session, 1)


# Rule 2: shared vault ----------------------------------------------------------------

def test_rule2_propose_into_shared_practices(world) -> None:
    refused(world.shared / "practices" / "new.md", Op.PROPOSE, world.session, 2)


@pytest.mark.parametrize("op", [Op.CREATE_LOG, Op.WRITE_PLAN, Op.WRITE_CODE])
def test_rule2_other_writes_to_shared(world, op) -> None:
    refused(world.shared / "inbox" / "x.md", op, world.session, 2)


def test_rule2_user_action_into_shared_inbox(world) -> None:
    refused(world.shared / "inbox" / "x.md", Op.USER_ACTION, world.session, 2)


@pytest.mark.parametrize("rel", ["AGENTS.md", "templates/practice.md"])
def test_rule2_shared_human_only(world, rel) -> None:
    for op in WRITE_OPS:
        refused(world.shared / rel, op, world.session, 2)


def test_rule2_symlink_in_inbox_to_shared_practices(world) -> None:
    (world.alpha / "inbox" / "link").symlink_to(world.shared / "practices")
    err = refused(world.alpha / "inbox" / "link" / "new.md", Op.PROPOSE, world.session, 2)
    assert err.path == str(world.shared / "practices" / "new.md")


# Rule 3: pulled vaults ----------------------------------------------------------------

@pytest.mark.parametrize("op", WRITE_OPS)
def test_rule3_any_write_to_pulled_vault(world, op) -> None:
    refused(world.beta / "inbox" / "x.md", op, world.session, 3)


# Rule 4: off-limits paths -------------------------------------------------------------

@pytest.mark.parametrize("name", [".obsidian", ".OBSIDIAN", ".Obsidian"])
def test_rule4_obsidian_case_folded(world, name) -> None:
    refused(world.alpha / name / "app.json", Op.READ, world.session, 4)


@pytest.mark.parametrize("name", [".git", ".GIT"])
def test_rule4_git(world, name) -> None:
    refused(world.alpha_repo / name / "config", Op.READ, world.session, 4)


def test_rule4_symlink_into_obsidian(world) -> None:
    (world.alpha / "inbox" / "settings").symlink_to(world.alpha / ".obsidian")
    refused(world.alpha / "inbox" / "settings" / "app.json", Op.READ, world.session, 4)


@pytest.mark.parametrize("name", ["vault", "VAULT"])
def test_rule4_worktree_vault(world, name) -> None:
    refused(world.worktree / name / "x.md", Op.READ, world.session, 4)
    refused(world.worktree / name / "inbox" / "copy.md", Op.WRITE_CODE, world.session, 4)


@pytest.mark.parametrize("op", list(Op))
def test_rule4_applies_to_every_op(world, op) -> None:
    refused(world.alpha / ".obsidian" / "app.json", op, world.session, 4)


# Rule 5: active vault writes by op ---------------------------------------------------

def test_rule5_propose_to_decisions(world) -> None:
    refused(world.alpha / "decisions" / "new.md", Op.PROPOSE, world.session, 5)


def test_rule5_create_log_onto_existing_log(world) -> None:
    refused(world.alpha / "logs" / "2026-11-25-run-1.md", Op.CREATE_LOG, world.session, 5)


@pytest.mark.parametrize(
    "op,rel",
    [
        (Op.CREATE_LOG, "inbox/x.md"),
        (Op.WRITE_PLAN, "inbox/x.md"),
        (Op.PROPOSE, "plans/x.md"),
        (Op.PROPOSE, "logs/x.md"),
        (Op.USER_ACTION, "inbox/x.md"),
        (Op.USER_ACTION, "plans/x.md"),
        (Op.PROPOSE, "inbox"),  # the folder itself, not a file in it
        (Op.PROPOSE, "new-root-file.md"),
        (Op.PROPOSE, "context.md"),  # case variants of allowed names are refused
    ],
)
def test_rule5_op_outside_its_folders(world, op, rel) -> None:
    refused(world.alpha / rel, op, world.session, 5)


@pytest.mark.parametrize("rel", ["AGENTS.md", "templates/decision.md", "templates/new.md"])
@pytest.mark.parametrize("op", [op for op in WRITE_OPS if op is not Op.WRITE_CODE])
def test_rule5_human_only(world, rel, op) -> None:
    refused(world.alpha / rel, op, world.session, 5)


def test_rule5_create_log_through_existing_symlink(world) -> None:
    (world.alpha / "logs" / "alias.md").symlink_to(world.alpha / "logs" / "2026-11-25-run-1.md")
    refused(world.alpha / "logs" / "alias.md", Op.CREATE_LOG, world.session, 5)


# Rule 6: code only in the worktree --------------------------------------------------

def test_rule6_write_code_in_own_checkout(world) -> None:
    refused(world.alpha_repo / "src" / "main.py", Op.WRITE_CODE, world.session, 6)


def test_rule6_write_code_in_active_vault(world) -> None:
    refused(world.alpha / "inbox" / "x.py", Op.WRITE_CODE, world.session, 6)


# Rule 7: commands -----------------------------------------------------------------------

def test_rule7_command_not_allowlisted() -> None:
    with pytest.raises(PolicyError) as exc:
        authorize_command(["rm", "-rf", ".."], {"pytest"})
    assert exc.value.rule == 7


@pytest.mark.parametrize("argv", [[], "pytest", ["/bin/rm", "x"], ["./rm"], [""], ["pytest/"]])
def test_rule7_refusals(argv) -> None:
    with pytest.raises(PolicyError) as exc:
        authorize_command(argv, {"pytest"})
    assert exc.value.rule == 7


@pytest.mark.parametrize("argv", [["pytest", "-q"], [os.path.join("", ".venv", "bin", "pytest")]])
def test_rule7_allowed(argv) -> None:
    authorize_command(argv, {"pytest"})


# Performance and contract -----------------------------------------------------------------

def test_authorized_path_is_resolved(world) -> None:
    auth = authorize(f"{world.alpha}/inbox/./sub/../x.md", Op.PROPOSE, world.session)
    assert auth.path == world.alpha / "inbox" / "x.md" and auth.rel == "inbox/x.md"


def test_op_must_be_an_op(world) -> None:
    with pytest.raises(TypeError):
        authorize(world.alpha / "inbox" / "x.md", "PROPOSE", world.session)  # type: ignore[arg-type]
