"""Process workers for the terminal tests (no pytest import, so a spawned child can load it)."""

from __future__ import annotations

from pathlib import Path


def reserve_race(state_root: str, i: int, results) -> None:
    import datetime as dt
    from decimal import Decimal

    from daedalus.core import queue
    from daedalus.terminal import ledger

    queue.STATE_ROOT = Path(state_root)
    try:
        ledger.reserve(session_id=f"racer-{i}", model="deepseek-flash",
                       when=dt.datetime(2026, 11, 25, 5, tzinfo=dt.timezone.utc), peak=False, input_tokens=1,
                       max_tokens=1, worst_case=Decimal("0.20"), session_cap=1.0, daily_cap=3.0)
        results.put("ok")
    except ledger.CapExceeded:
        results.put("capped")
    except Exception as e:  # report, don't hang the parent
        results.put(f"error: {e!r}")
