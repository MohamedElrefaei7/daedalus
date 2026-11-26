"""~/.config/daedalus/config.toml: the agent profile and spend caps.

Every key is optional; a missing file means all defaults. An unknown key is
an error, so a misspelled cap (dailly_cap_usd) can't silently fall back to
the default.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

DEFAULT_CONFIG = Path("~/.config/daedalus/config.toml")

REASONING_EFFORTS = ("low", "high", "max")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    model: str = "deepseek-flash"
    reasoning_effort: str = "low"  # sent on every request; DeepSeek's default is high
    max_tokens: int = 8000  # sent on every request; thinking mode would default to 64K
    session_cap_usd: float = 1.00
    daily_cap_usd: float = 3.00


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    p = Path(os.path.expanduser(os.fspath(path if path is not None else DEFAULT_CONFIG)))
    try:
        with open(p, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return Config()
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{p}: not valid TOML: {e}") from None
    known = {f.name: f for f in fields(Config)}
    unknown = sorted(set(data) - set(known))
    if unknown:
        raise ConfigError(f"{p}: unknown key(s) {', '.join(unknown)}; known: {', '.join(known)}")
    values = {}
    for name, value in data.items():
        want = known[name].type
        if want == "float" and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        ok = {"str": str, "int": int, "float": float}[want]
        if not isinstance(value, ok) or isinstance(value, bool):
            raise ConfigError(f"{p}: {name} must be a {want}, got {value!r}")
        values[name] = value
    cfg = Config(**values)
    from .pricing import PRICES

    if cfg.model not in PRICES["models"]:
        raise ConfigError(f"{p}: model {cfg.model!r} has no price; known: {', '.join(PRICES['models'])}")
    if cfg.reasoning_effort not in REASONING_EFFORTS:
        raise ConfigError(f"{p}: reasoning_effort must be one of {', '.join(REASONING_EFFORTS)}")
    if not 1 <= cfg.max_tokens <= 393216:
        raise ConfigError(f"{p}: max_tokens must be between 1 and 393216")
    for cap in ("session_cap_usd", "daily_cap_usd"):
        if getattr(cfg, cap) < 0:
            raise ConfigError(f"{p}: {cap} can't be negative")
    return cfg
