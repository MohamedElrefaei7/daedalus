"""DeepSeek prices, peak hours, and what a call costs.

From https://api-docs.deepseek.com/quick_start/pricing, checked 2026-11-25.
Prices are USD per 1M tokens at the off-peak rate; peak is double.

All time math is in UTC: DeepSeek's windows are in UTC, and UTC has no
daylight saving, so the windows never move.

Chinese public holidays are off-peak in DeepSeek's billing, but they can't be
known here (the calendar is set by the State Council each year). So a weekday
inside a peak window is always priced as peak. On a holiday that
overestimates, which is the safe direction for a spend cap.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

PRICES = {
    "checked": "2026-11-25",
    "source": "https://api-docs.deepseek.com/quick_start/pricing",
    "currency": "USD",
    "per_tokens": 1_000_000,
    "peak_multiplier": 2,
    # Half-open [start, end) hours in UTC, Monday to Friday.
    "peak_windows_utc": ((1, 4), (6, 10)),
    "models": {
        "deepseek-flash": {"cache_hit": Decimal("0.003"), "cache_miss": Decimal("0.15"), "output": Decimal("0.60")},
        "deepseek-v4-pro": {"cache_hit": Decimal("0.022"), "cache_miss": Decimal("0.66"), "output": Decimal("1.98")},
    },
    # Retired names DeepSeek still accepts and bills at the Flash price.
    "aliases": {"deepseek-v4-flash": "deepseek-flash", "deepseek-v4-flash-vision-exp": "deepseek-flash"},
}


class UnknownModel(ValueError):
    pass


@dataclass(frozen=True)
class Usage:
    """Billed token counts for one call. Reasoning tokens are inside ``completion``."""

    prompt_cache_hit: int = 0
    prompt_cache_miss: int = 0
    completion: int = 0
    reasoning: int = 0

    @property
    def prompt(self) -> int:
        return self.prompt_cache_hit + self.prompt_cache_miss


def _utc(when: dt.datetime) -> dt.datetime:
    if when.tzinfo is None:
        raise ValueError("pass an aware datetime; pricing does all time math in UTC")
    return when.astimezone(dt.timezone.utc)


def is_peak(when: dt.datetime) -> bool:
    """Monday to Friday, 01:00-04:00 or 06:00-10:00 UTC, each window half-open (04:00:00 is off-peak)."""
    t = _utc(when)
    if t.weekday() >= 5:
        return False
    return any(start <= t.hour < end for start, end in PRICES["peak_windows_utc"])


def period(when: dt.datetime) -> str:
    return "peak" if is_peak(when) else "off-peak"


def rates(model: str, when: dt.datetime) -> dict[str, Decimal]:
    """Per-token USD rates for ``model`` at ``when``."""
    name = PRICES["aliases"].get(model, model)
    try:
        table = PRICES["models"][name]
    except KeyError:
        raise UnknownModel(f"no price for model {model!r}; known: {', '.join(PRICES['models'])}") from None
    mult = Decimal(PRICES["peak_multiplier"]) if is_peak(when) else Decimal(1)
    per = Decimal(PRICES["per_tokens"])
    return {k: v * mult / per for k, v in table.items()}


def cost(usage: Usage, model: str, when_utc: dt.datetime) -> Decimal:
    """Cache-hit, cache-miss, and completion tokens priced separately; reasoning bills as output."""
    r = rates(model, when_utc)
    return (usage.prompt_cache_hit * r["cache_hit"] + usage.prompt_cache_miss * r["cache_miss"]
            + usage.completion * r["output"])


def worst_case(input_tokens: int, max_tokens: int, model: str, when_utc: dt.datetime) -> Decimal:
    """The most a call can cost: every input token a cache miss, and all of max_tokens used."""
    return cost(Usage(prompt_cache_miss=input_tokens, completion=max_tokens), model, when_utc)
