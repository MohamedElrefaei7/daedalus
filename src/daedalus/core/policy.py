"""The path policy: seven rules that every read and write passes through.

Deny by default. Each area lists, per op, exactly where that op may act;
anything not listed is refused. Nothing here is written as "allow unless X".

Rules (the number is PolicyError.rule):
  1. Location: inside a registered vault, the active repo, or the worktree.
     The repo is read-only; its only write is WRITE_CODE in the worktree.
  2. Shared vault: READ; PROPOSE into inbox/; USER_ACTION into practices/, lessons/.
  3. Pulled vaults: READ only.
  4. Off limits for every op: any .obsidian or .git component (case-folded),
     and anything inside <worktree>/vault/.
  5. Active vault writes, per op (see ACTIVE_VAULT_ALLOW). Never AGENTS.md or templates/.
  6. WRITE_CODE only in the worktree, never in your own checkout.
  7. Commands: argv[0]'s basename must be on the allowlist.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping

from .session import Area, Session


class Op(Enum):
    READ = "read"
    PROPOSE = "propose"
    CREATE_LOG = "create_log"
    WRITE_PLAN = "write_plan"
    USER_ACTION = "user_action"
    WRITE_CODE = "write_code"


class PolicyError(Exception):
    def __init__(self, rule: int, path: str | os.PathLike[str], reason: str) -> None:
        self.rule = rule
        self.path = os.fspath(path)
        self.reason = reason
        super().__init__(f"rule {rule}: {reason}: {self.path}")


@dataclass(frozen=True)
class Authorized:
    path: Path  # the resolved real path
    area: Area
    vault: str | None  # vault name for vault areas, else None
    rel: str  # path relative to the area's root, "/"-separated; "" for the root itself


# Allow tables. ANYWHERE means the whole area. Otherwise an entry ending in "/"
# allows anything inside that folder, and any other entry allows that exact
# root-level file. An op missing from a table is refused.
ANYWHERE = ("",)

ACTIVE_VAULT_ALLOW: Mapping[Op, tuple[str, ...]] = {
    Op.READ: ANYWHERE,
    Op.PROPOSE: ("inbox/", "questions/", "CONTEXT.md"),
    Op.CREATE_LOG: ("logs/",),
    Op.WRITE_PLAN: ("plans/",),
    Op.USER_ACTION: (
        "goals/", "sources/", "decisions/", "components/", "experiments/", "concepts/", "CONTEXT.md",
    ),
}
SHARED_ALLOW: Mapping[Op, tuple[str, ...]] = {
    Op.READ: ANYWHERE,
    Op.PROPOSE: ("inbox/",),
    Op.USER_ACTION: ("practices/", "lessons/"),
}
PULLED_ALLOW: Mapping[Op, tuple[str, ...]] = {Op.READ: ANYWHERE}
REPO_ALLOW: Mapping[Op, tuple[str, ...]] = {Op.READ: ANYWHERE}
WORKTREE_ALLOW: Mapping[Op, tuple[str, ...]] = {Op.READ: ANYWHERE, Op.WRITE_CODE: ANYWHERE}

# Human-only paths in a vault: no op writes them, whatever the tables say.
HUMAN_ONLY = ("AGENTS.md", "templates/")

# Path components that are off limits everywhere (rule 4), compared case-folded:
# macOS file systems ignore case, so .OBSIDIAN/app.json *is* .obsidian/app.json.
OFF_LIMITS = frozenset({".obsidian", ".git"})


def authorize(path: str | os.PathLike[str], op: Op, session: Session) -> Authorized:
    """Resolve ``path`` and check ``op`` against the seven rules, or raise PolicyError."""
    if not isinstance(op, Op):
        raise TypeError(f"op must be an Op, got {op!r}")
    raw = os.fspath(path)
    if not os.path.isabs(raw):
        raise PolicyError(1, raw, "path must be absolute")
    _check_off_limits(raw)
    try:
        # realpath follows every symlink, including a dangling final one, and
        # keeps any not-yet-existing tail as-is: a new file resolves to its
        # resolved parent plus its name.
        real = os.path.realpath(raw)
    except (OSError, ValueError) as e:
        raise PolicyError(1, raw, f"path can't be resolved ({e})") from None
    _check_off_limits(real)

    area, vault, rel = classify(real, session)
    if area is Area.OUTSIDE:
        raise PolicyError(1, real, "outside every registered vault, the active repo, and the worktree")
    if area is Area.WORKTREE and rel.split("/", 1)[0].casefold() == "vault":
        raise PolicyError(4, real, "the worktree's vault/ is off limits; vault notes go through the active vault")

    _check_area(area, op, rel, raw, real)
    return Authorized(path=Path(real), area=area, vault=vault, rel=rel)


def classify(real: str, session: Session) -> tuple[Area, str | None, str]:
    """Return (area, vault name, rel path) for an already-resolved path."""
    for root, area, vault in session.areas:
        if real == root:
            return area, vault, ""
        if real.startswith(root + os.sep):
            return area, vault, real[len(root) + 1 :].replace(os.sep, "/")
    return Area.OUTSIDE, None, ""


def _check_off_limits(path: str) -> None:
    for part in path.split(os.sep):
        if part.casefold() in OFF_LIMITS:
            raise PolicyError(4, path, f"'{part}' paths are off limits")


def _matches(rel: str, targets: Iterable[str]) -> bool:
    for target in targets:
        if target == "":
            return True
        if target.endswith("/"):
            if rel.startswith(target) and len(rel) > len(target):
                return True
        elif rel == target:
            return True
    return False


def _allow(rule: int, op: Op, rel: str, real: str, table: Mapping[Op, tuple[str, ...]], where: str) -> None:
    targets = table.get(op)
    if targets is None:
        raise PolicyError(rule, real, f"{op.name} is not allowed in {where}")
    if not _matches(rel, targets):
        allowed = ", ".join(targets)
        raise PolicyError(rule, real, f"{op.name} in {where} is allowed only in: {allowed}")


def _check_area(area: Area, op: Op, rel: str, raw: str, real: str) -> None:
    if area is Area.SHARED_VAULT:
        return _allow(2, op, rel, real, SHARED_ALLOW, "the shared vault")
    if area is Area.PULLED_VAULT:
        return _allow(3, op, rel, real, PULLED_ALLOW, "a pulled vault")
    if area is Area.ACTIVE_VAULT:
        if op is Op.WRITE_CODE:
            raise PolicyError(6, real, "WRITE_CODE is allowed only in the worktree")
        if op is not Op.READ and _matches(rel, HUMAN_ONLY):
            raise PolicyError(5, real, "AGENTS.md and templates/ are human-only")
        _allow(5, op, rel, real, ACTIVE_VAULT_ALLOW, "the active vault")
        if op is Op.CREATE_LOG and (os.path.lexists(raw) or os.path.lexists(real)):
            raise PolicyError(5, real, "logs are create-only, and this log already exists")
        return None
    if area is Area.ACTIVE_REPO:
        if op is Op.WRITE_CODE:
            raise PolicyError(6, real, "WRITE_CODE is never allowed in your own checkout; use the worktree")
        return _allow(1, op, rel, real, REPO_ALLOW, "the active repo, which is read-only")
    if area is Area.WORKTREE:
        return _allow(1, op, rel, real, WORKTREE_ALLOW, "the worktree, which takes only code")
    # Deny by default: an area with no branch above allows nothing.
    raise PolicyError(1, real, f"no rule allows {op.name} in {area.value}")


def authorize_command(argv: list[str], allowlist: set[str]) -> None:
    """Rule 7: allow ``argv`` only if its program's basename is allowlisted."""
    if isinstance(argv, (str, bytes)) or not argv:
        raise PolicyError(7, "", "command must be a non-empty argv list")
    prog = argv[0]
    if not isinstance(prog, str) or not prog:
        raise PolicyError(7, str(prog), "argv[0] must be a non-empty string")
    base = os.path.basename(prog)
    if base not in allowlist:
        has_sep = os.sep in prog or (os.altsep is not None and os.altsep in prog)
        what = "a path to a command" if has_sep else "command"
        raise PolicyError(7, prog, f"{what} {base!r} is not on the allowlist ({', '.join(sorted(allowlist)) or 'empty'})")
