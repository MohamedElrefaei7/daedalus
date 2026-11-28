"""Stopgap approval CLI until commit 5's REPL: python -m daedalus.queue_cli.

    python -m daedalus.queue_cli --vault scratch list
    python -m daedalus.queue_cli --vault scratch diff <id>
    python -m daedalus.queue_cli --vault scratch apply <id> [--accept-decisions]   (a plan needs the flag)
    python -m daedalus.queue_cli --vault scratch reject <id> "<reason>"

Deliberately thin: every command is one call into daedalus.core, so the REPL
can replace this by calling the same functions.
"""

from __future__ import annotations

import argparse
import sys

from daedalus.core import (
    PolicyError, ProposalError, RegistryError, Session, SessionError, diff, load_registry, pending, reject,
)
from daedalus.core import plans, queue
from daedalus.core.apply import apply, is_plan_proposal


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m daedalus.queue_cli", description="Review the proposal queue.")
    parser.add_argument("--vault", required=True, help="the active project (a registry name)")
    parser.add_argument("--registry", help="path to vaults.toml (default ~/.config/daedalus/vaults.toml)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list pending proposals")
    sub.add_parser("diff", help="show a proposal's diff").add_argument("id")
    ap = sub.add_parser("apply", help="apply a proposal and commit it")
    ap.add_argument("id")
    ap.add_argument("--accept-decisions", action="store_true",
                    help="for a plan: accept its new decisions as precedent (required; without it a plan is refused)")
    rej = sub.add_parser("reject", help="reject a proposal with a reason")
    rej.add_argument("id")
    rej.add_argument("reason")
    args = parser.parse_args(argv)

    try:
        session = Session(load_registry(args.registry), active=args.vault)
        in_scope = {name for name, _ in session.scope}
        if args.command == "list":
            rows = [p for p in pending() if p.vault in in_scope]
            for p in rows:
                print(f"{p.id}  {p.kind:6}  {p.vault}:{p.rel_path}  ({p.source}) {p.summary}")
            if not rows:
                print("no pending proposals")
        elif args.command == "diff":
            print(diff(args.id), end="")
        elif args.command == "apply":
            if is_plan_proposal(queue.get(args.id)):
                if not args.accept_decisions:
                    print(f"error: {args.id} is a plan; approving it makes its new decisions binding precedent, so "
                          "pass --accept-decisions to say you accept them. Nothing was written.", file=sys.stderr)
                    return 1
                done = plans.approve_plan(session, args.id)
                print(f"approved plan {done.slug} v{done.version} in commit {done.commit_sha}: "
                      + ", ".join((done.plan_ref, *done.decision_refs)))
            else:
                p = apply(session, args.id)
                print(f"applied {p.id}: {p.vault}:{p.rel_path} in commit {p.commit_sha}")
        elif args.command == "reject":
            p = reject(args.id, args.reason)
            print(f"rejected {p.id}: {p.status_reason}")
    except (PolicyError, ProposalError, RegistryError, SessionError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
