"""python -m daedalus.sandbox run --project <name> [--worktree <path>] -- <program> [args...]

Runs one command through the sandbox for a registered project, without
touching state.json. The worktree defaults to the project's own checkout,
which the run can read but never write (its vault/ and .git stay unreadable).
Prints the output tails, then one line with the exit code and timing; exits
with the command's exit code, or 2 if it was refused.
"""

from __future__ import annotations

import argparse
import sys

from ..core.policy import PolicyError
from ..core.registry import RegistryError, load_registry
from ..core.session import Session, SessionError
from .errors import SandboxError
from .runner import run


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m daedalus.sandbox")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run one command sandboxed")
    r.add_argument("--project", required=True)
    r.add_argument("--worktree", help="default: the project's own checkout (read-only to the run)")
    r.add_argument("--registry", help="vaults.toml to use instead of ~/.config/daedalus/vaults.toml")
    r.add_argument("command", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    try:
        registry = load_registry(args.registry)
        worktree = args.worktree
        session = Session(registry, args.project, worktree=worktree) if worktree else Session(registry, args.project)
        result = run(session, command, worktree or session.active_repo)
    except PolicyError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    except (SandboxError, SessionError, RegistryError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    sys.stdout.write(result.stdout_tail)
    sys.stdout.flush()
    sys.stderr.write(result.stderr_tail)
    state = "timed out" if result.timed_out else f"exit {result.exit_code}"
    print(f"— sandboxed: {state} in {result.duration_s:.2f} s", file=sys.stderr)
    return 124 if result.timed_out else result.exit_code


if __name__ == "__main__":
    sys.exit(main())
