"""Test 2: peak pricing, in UTC, half-open windows; and the price table itself."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from fakes import WED_OFF, WED_PEAK

from daedalus.terminal.pricing import PRICES, UnknownModel, Usage, cost, is_peak, period, worst_case

UTC = dt.timezone.utc
U = Usage(prompt_cache_hit=9_100, prompt_cache_miss=3_380, completion=312, reasoning=180)


def at(y, mo, d, h, mi=0, s=0):
    return dt.datetime(y, mo, d, h, mi, s, tzinfo=UTC)


@pytest.mark.parametrize("model", sorted(PRICES["models"]))
def test_peak_is_exactly_double(model) -> None:
    assert cost(U, model, WED_PEAK) == 2 * cost(U, model, WED_OFF)
    assert cost(U, model, WED_OFF) > 0


def test_weekend_is_off_peak() -> None:
    sat = at(2026, 11, 28, 2)
    assert sat.weekday() == 5 and not is_peak(sat)
    assert cost(U, "deepseek-flash", sat) == cost(U, "deepseek-flash", WED_OFF)
    assert not is_peak(at(2026, 11, 29, 7))  # Sunday, inside the second window


@pytest.mark.parametrize("when, peak", [
    (at(2026, 11, 25, 0, 59, 59), False), (at(2026, 11, 25, 1, 0, 0), True),
    (at(2026, 11, 25, 3, 59, 59), True), (at(2026, 11, 25, 4, 0, 0), False),
    (at(2026, 11, 25, 5, 59, 59), False), (at(2026, 11, 25, 6, 0, 0), True),
    (at(2026, 11, 25, 9, 59, 59), True), (at(2026, 11, 25, 10, 0, 0), False),
    (at(2026, 11, 27, 9, 0), True),  # Friday
    (at(2026, 11, 30, 1, 30), True),  # Monday
])
def test_window_edges_are_half_open(when, peak) -> None:
    assert is_peak(when) is peak
    assert period(when) == ("peak" if peak else "off-peak")


def test_time_math_is_utc_whatever_the_zone() -> None:
    eastern_dst = dt.timezone(dt.timedelta(hours=-4))
    eastern_std = dt.timezone(dt.timedelta(hours=-5))
    # 22:00 the evening before, Eastern, is 02:00 or 03:00 UTC on Wednesday: peak either way.
    assert is_peak(dt.datetime(2026, 11, 24, 21, 0, tzinfo=eastern_std))  # 02:00 UTC Wed
    assert is_peak(dt.datetime(2026, 7, 14, 22, 0, tzinfo=eastern_dst))  # 02:00 UTC Wed
    with pytest.raises(ValueError):
        is_peak(dt.datetime(2026, 11, 25, 2, 0))  # naive: refused, never guessed


def test_cost_prices_each_kind_separately() -> None:
    flash = PRICES["models"]["deepseek-flash"]
    want = (9_100 * flash["cache_hit"] + 3_380 * flash["cache_miss"] + 312 * flash["output"]) / Decimal(1_000_000)
    assert cost(U, "deepseek-flash", WED_OFF) == want
    # Reasoning is part of completion; it isn't billed a second time.
    assert cost(U, "deepseek-flash", WED_OFF) == cost(Usage(9_100, 3_380, 312, 0), "deepseek-flash", WED_OFF)


def test_worst_case_is_all_miss_and_full_max_tokens() -> None:
    pro = PRICES["models"]["deepseek-v4-pro"]
    assert worst_case(10_000, 8_000, "deepseek-v4-pro", WED_PEAK) == \
        2 * (10_000 * pro["cache_miss"] + 8_000 * pro["output"]) / Decimal(1_000_000)


def test_table_has_checked_date_and_known_models() -> None:
    assert PRICES["checked"] == "2026-11-25"
    assert PRICES["models"]["deepseek-flash"] == {
        "cache_hit": Decimal("0.003"), "cache_miss": Decimal("0.15"), "output": Decimal("0.60")}
    assert PRICES["models"]["deepseek-v4-pro"] == {
        "cache_hit": Decimal("0.022"), "cache_miss": Decimal("0.66"), "output": Decimal("1.98")}
    assert cost(U, "deepseek-v4-flash", WED_OFF) == cost(U, "deepseek-flash", WED_OFF)  # retired alias
    with pytest.raises(UnknownModel):
        cost(U, "gpt-9", WED_OFF)
