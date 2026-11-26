"""The session on disk: ``~/.config/daedalus/state.json``.

``{"active": "scratch", "pulled": []}`` names the active project and the
projects pulled in for reading. Core functions still take a Session
explicitly; this module is how front ends (daedalus-mcp now, the REPL's
/vault and /pull in commit 6) build one.

    python -m daedalus.state show | set-active <name> | pull <name> | drop <name>

(daedalus/state.py is the -m entry point; it calls main() here.)
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any

from .registry import DEFAULT_REGISTRY, Registry, RegistryError, load_registry
from .session import Session, SessionError

DEFAULT_STATE = Path("~/.config/daedalus/state.json")

FIX_HINT = "python -m daedalus.state set-active <name>"


class StateError(Exception):
    """state.json is missing, malformed, or names a project the registry doesn't know."""


def state_file(state_path: str | os.PathLike[str] | None = None) -> Path:
    return Path(os.path.expanduser(os.fspath(state_path if state_path is not None else DEFAULT_STATE)))


def read_state(state_path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """The raw ``{"active": ..., "pulled": [...]}``, shape-checked but not checked against the registry."""
    path = state_file(state_path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise StateError(f"{path}: no session state yet. Pick the active project with: {FIX_HINT}") from None
    except OSError as e:
        raise StateError(f"{path}: can't read the session state ({e.strerror})") from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise StateError(f"{path}: not valid JSON ({e}). Rewrite it with: {FIX_HINT}") from None
    if not isinstance(data, dict):
        raise StateError(f"{path}: must be a JSON object like {{\"active\": \"scratch\", \"pulled\": []}}. "
                         f"Rewrite it with: {FIX_HINT}")
    active = data.get("active")
    if not isinstance(active, str) or not active:
        raise StateError(f"{path}: no active project. Set one with: {FIX_HINT}")
    pulled = data.get("pulled", [])
    if not isinstance(pulled, list) or not all(isinstance(p, str) for p in pulled):
        raise StateError(f"{path}: 'pulled' must be a list of project names. Fix it with: python -m daedalus.state drop <name>")
    return {"active": active, "pulled": pulled}


def _session(registry: Registry, active: str, pulled: list[str] | tuple[str, ...], where: Path) -> Session:
    try:
        return Session(registry, active=active, pulled=tuple(pulled))
    except SessionError as e:
        raise StateError(
            f"{where}: {e}. Registered projects live in {registry.source}; fix the state with: "
            f"{FIX_HINT}, or python -m daedalus.state drop <name>"
        ) from None


def load_session(
    registry_path: str | os.PathLike[str] | None = None, state_path: str | os.PathLike[str] | None = None
) -> Session:
    """Build the Session that state.json describes. Raises StateError or RegistryError."""
    registry = load_registry(registry_path)
    state = read_state(state_path)
    return _session(registry, state["active"], state["pulled"], state_file(state_path))


def save_state(
    active: str,
    pulled: list[str] | tuple[str, ...] = (),
    registry_path: str | os.PathLike[str] | None = None,
    state_path: str | os.PathLike[str] | None = None,
) -> Session:
    """Validate against the registry, then write state.json atomically; returns the new Session."""
    path = state_file(state_path)
    session = _session(load_registry(registry_path), active, list(pulled), path)
    data = json.dumps({"active": session.active, "pulled": list(session.pulled)}, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{secrets.token_hex(6)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        try:
            os.write(fd, data.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    dirfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dirfd)
    finally:
        os.close(dirfd)
    return session


# The three changes: used by the CLI below and by the REPL's /vault, /pull, /drop --------------

def set_active(name: str, registry_path=None, state_path=None) -> Session:
    """Make ``name`` active. A pulled project that becomes active leaves pulled."""
    try:
        pulled = [p for p in read_state(state_path)["pulled"] if p != name]
    except StateError:
        pulled = []  # no usable state yet: set-active is how you make one
    return save_state(name, pulled, registry_path, state_path)


def pull(name: str, registry_path=None, state_path=None) -> Session:
    """Pull ``name`` in for reading. Pulling one that's already pulled changes nothing."""
    state = read_state(state_path)
    if name == state["active"]:
        raise StateError(f"{name!r} is the active project, so it can't also be pulled")
    pulled = list(state["pulled"])
    if name not in pulled:
        pulled.append(name)
    return save_state(state["active"], pulled, registry_path, state_path)


def drop(name: str, registry_path=None, state_path=None) -> Session:
    """Stop pulling ``name``."""
    state = read_state(state_path)
    if name not in state["pulled"]:
        raise StateError(f"{name!r} is not pulled (pulled: {', '.join(state['pulled']) or 'none'})")
    return save_state(state["active"], [p for p in state["pulled"] if p != name], registry_path, state_path)


# The stopgap CLI ---------------------------------------------------------------------

def _describe(session: Session) -> str:
    lines = [f"active:  {session.active}  ({session.active_vault})", f"repo:    {session.active_repo}"]
    lines.append("pulled:  " + (", ".join(session.pulled) if session.pulled else "(none)"))
    lines.append(f"shared:  {session.registry.shared}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m daedalus.state", description="Show or change the session.")
    parser.add_argument("--registry", help=f"vault registry (default {DEFAULT_REGISTRY})")
    parser.add_argument("--state", help=f"session state file (default {DEFAULT_STATE})")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show", help="print the active project, pulled projects, and paths")
    for name, text in (("set-active", "make <name> the active project"), ("pull", "pull <name> in for reading"),
                       ("drop", "stop pulling <name>")):
        sub.add_parser(name, help=text).add_argument("name")
    args = parser.parse_args(argv)

    try:
        if args.cmd == "show":
            print(_describe(load_session(args.registry, args.state)))
            return 0
        change = {"set-active": set_active, "pull": pull, "drop": drop}[args.cmd]
        session = change(args.name, args.registry, args.state)
    except (StateError, RegistryError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(_describe(session))
    return 0


if __name__ == "__main__":
    sys.exit(main())
