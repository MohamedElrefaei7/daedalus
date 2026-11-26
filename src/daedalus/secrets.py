"""API keys: in the macOS Keychain (through ``keyring``), never in a file.

Service ``daedalus``, username = the provider (``deepseek``). The environment
variable DEEPSEEK_API_KEY overrides the Keychain; it exists for tests and CI.

    python -m daedalus.secrets set deepseek     # prompts with getpass, stores in the Keychain
    python -m daedalus.secrets status           # says whether a key is present, never prints it

(This module is daedalus.secrets; inside the package, ``import secrets`` with
absolute imports still means the standard library.)
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

import keyring
from keyring.errors import KeyringError

SERVICE = "daedalus"
PROVIDERS = {"deepseek": "DEEPSEEK_API_KEY"}

SET_HINT = "python -m daedalus.secrets set deepseek"


def env_var(provider: str) -> str:
    try:
        return PROVIDERS[provider]
    except KeyError:
        raise ValueError(f"unknown provider {provider!r}; known: {', '.join(PROVIDERS)}") from None


def get_key(provider: str = "deepseek") -> str | None:
    """The key from the environment override, else the Keychain; None if neither has one."""
    from_env = os.environ.get(env_var(provider), "").strip()
    if from_env:
        return from_env
    try:
        stored = keyring.get_password(SERVICE, provider)
    except KeyringError:
        return None
    return stored.strip() if stored and stored.strip() else None


def key_source(provider: str = "deepseek") -> str | None:
    """Where the key would come from: the env var's name, "Keychain", or None."""
    if os.environ.get(env_var(provider), "").strip():
        return env_var(provider)
    try:
        stored = keyring.get_password(SERVICE, provider)
    except KeyringError:
        return None
    return "Keychain" if stored and stored.strip() else None


def set_key(provider: str, key: str) -> None:
    key = key.strip()
    if not key:
        raise ValueError("the key is empty")
    keyring.set_password(SERVICE, provider, key)


def missing_key_message(provider: str = "deepseek") -> str:
    return (f"No {provider} API key. Store one in the Keychain with: {SET_HINT}\n"
            f"(or set {env_var(provider)} for this shell only)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m daedalus.secrets", description="Manage Daedalus API keys.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("set", help="store a key in the Keychain").add_argument("provider", choices=sorted(PROVIDERS))
    sub.add_parser("status", help="say whether each key is present (never prints it)")
    args = parser.parse_args(argv)

    if args.cmd == "set":
        key = getpass.getpass(f"{args.provider} API key (input hidden): ")
        try:
            set_key(args.provider, key)
        except (ValueError, KeyringError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(f"stored the {args.provider} key in the Keychain (service {SERVICE!r}, account {args.provider!r})")
        return 0
    for provider in sorted(PROVIDERS):
        where = key_source(provider)
        print(f"{provider}: " + (f"key present ({where})" if where else f"no key; set one with: {SET_HINT}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
