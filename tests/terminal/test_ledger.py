"""The ledger: rows, spent(), reservations, and caps shared across sessions."""

from __future__ import annotations

import datetime as dt
import sqlite3
from decimal import Decimal

import pytest
from conftest import WED_OFF

from daedalus.terminal import ledger
from daedalus.terminal.ledger import CapExceeded
from daedalus.terminal.pricing import Usage


def reserve(session="s1", when=WED_OFF, worst="0.01", session_cap=1.0, daily_cap=3.0):
    return ledger.reserve(session_id=session, model="deepseek-flash", when=when, peak=False, input_tokens=100,
                          max_tokens=8000, worst_case=Decimal(worst), session_cap=session_cap, daily_cap=daily_cap)


def test_file_location_and_wal(state_root) -> None:
    reserve()
    assert ledger.path() == state_root / "ledger.sqlite"
    con = sqlite3.connect(ledger.path())
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    cols = [r[1] for r in con.execute("PRAGMA table_info(calls)")]
    assert cols == ["id", "ts_utc", "model", "peak", "prompt_cache_hit", "prompt_cache_miss", "completion",
                    "reasoning", "cost_usd", "estimated", "session_id"]


def test_reserve_settle_cancel() -> None:
    a = reserve(worst="0.05")
    r = ledger.row(a)
    assert (r["estimated"], r["cost_usd"], r["completion"]) == (1, 0.05, 8000)
    ledger.settle(a, Usage(10, 90, 50, 20), Decimal("0.001"), estimated=False)
    r = ledger.row(a)
    assert (r["prompt_cache_hit"], r["prompt_cache_miss"], r["completion"], r["reasoning"], r["estimated"]) == \
        (10, 90, 50, 20, 0)
    b = reserve()
    ledger.cancel(b)
    assert ledger.row(b) is None
    assert ledger.spent() == pytest.approx(0.001)


def test_spent_filters_by_session_and_time() -> None:
    yesterday = WED_OFF - dt.timedelta(days=1)
    ledger.settle(reserve("s1", yesterday), Usage(), Decimal("0.10"), False)
    ledger.settle(reserve("s1"), Usage(), Decimal("0.20"), False)
    ledger.settle(reserve("s2"), Usage(), Decimal("0.40"), True)
    assert ledger.spent() == pytest.approx(0.70)
    assert ledger.spent(session_id="s1") == pytest.approx(0.30)
    assert ledger.spent(since_utc=ledger.start_of_day(WED_OFF)) == pytest.approx(0.60)
    assert ledger.spent(since_utc=ledger.start_of_day(WED_OFF), session_id="s2") == pytest.approx(0.40)
    assert ledger.totals(session_id="s2").estimated_calls == 1


def test_caps_refuse_and_write_nothing() -> None:
    reserve("s1", worst="0.90")
    with pytest.raises(CapExceeded) as exc:
        reserve("s1", worst="0.20", session_cap=1.0)
    assert exc.value.cap == "session" and "session cap" in str(exc.value)
    reserve("s2", worst="1.95", session_cap=10)
    with pytest.raises(CapExceeded) as exc:
        reserve("s3", worst="0.20", daily_cap=3.0)
    assert exc.value.cap == "daily" and "daily cap" in str(exc.value)
    assert len(ledger.rows()) == 2


def test_daily_cap_resets_at_utc_midnight() -> None:
    late = dt.datetime(2026, 11, 25, 23, 59, tzinfo=dt.timezone.utc)
    reserve("s1", late, worst="2.95", session_cap=10)
    with pytest.raises(CapExceeded):
        reserve("s2", late, worst="0.10")
    reserve("s2", late + dt.timedelta(minutes=2), worst="0.10")  # 00:01 UTC Thursday


def test_two_processes_share_the_daily_cap(gworld) -> None:
    """Reservation and cap check are one transaction, so racers can't both spend the same headroom."""
    from core_conftest import spawn
    from workers_terminal import reserve_race

    # 20 racers, each wanting $0.20 of a $3.00 day: exactly 15 may get through.
    results = spawn(reserve_race, [(str(ledger.path().parent), i) for i in range(20)])
    assert sorted(results).count("ok") == 15
    assert ledger.spent() == pytest.approx(3.0)
