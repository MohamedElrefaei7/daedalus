"""The spend ledger: one row per DeepSeek call, in ~/.local/state/daedalus/ledger.sqlite.

The ledger is the single source of truth for spend caps. Every terminal
writes the same file (WAL mode, short transactions, the queue's pattern), so
two terminals running at once share the daily cap.

A call's row is written *before* the request leaves, as a reservation priced
at the worst case, in the same transaction that checks the caps. Two
terminals can't both pass the check on the same headroom. The row is then
updated with the billed numbers (estimated = 0), with an estimate if the
stream was interrupted (estimated = 1), or deleted if the request was never
answered with a stream (nothing was billed). A process killed mid-call
leaves the worst-case reservation in place, which errs toward overcounting.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from daedalus.core import queue
from daedalus.core.queue import _ensure_wal

from .pricing import Usage

SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS calls (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc            TEXT    NOT NULL,   -- request start, ISO 8601 with +00:00
    model             TEXT    NOT NULL,
    peak              INTEGER NOT NULL,   -- 0/1: priced at the peak rate
    prompt_cache_hit  INTEGER NOT NULL,
    prompt_cache_miss INTEGER NOT NULL,
    completion        INTEGER NOT NULL,   -- includes reasoning
    reasoning         INTEGER NOT NULL,
    cost_usd          REAL    NOT NULL,
    estimated         INTEGER NOT NULL,   -- 1 until billed usage arrives
    session_id        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS calls_ts ON calls (ts_utc);
CREATE INDEX IF NOT EXISTS calls_session ON calls (session_id);
"""


class CapExceeded(Exception):
    def __init__(self, cap: str, limit: float, spent: float, worst_case: float) -> None:
        self.cap = cap  # "session" or "daily"
        self.limit = limit
        self.spent = spent
        self.worst_case = worst_case
        key = f"{cap}_cap_usd"
        when = "this session" if cap == "session" else "today (UTC)"
        super().__init__(
            f"Not sent: the {cap} cap would be crossed. This call could cost up to ${worst_case:.4f}, "
            f"${spent:.4f} is already spent {when}, and the {cap} cap is ${limit:.2f}. "
            f"Raise {key} in ~/.config/daedalus/config.toml"
            + (", or start a new session." if cap == "session" else ", or wait for 00:00 UTC.")
        )


@dataclass(frozen=True)
class Totals:
    calls: int
    prompt_cache_hit: int
    prompt_cache_miss: int
    completion: int
    reasoning: int
    cost_usd: float
    estimated_calls: int


def path() -> Path:
    return queue.STATE_ROOT / "ledger.sqlite"


def connect() -> sqlite3.Connection:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p, timeout=30, isolation_level=None)
    con.execute("PRAGMA busy_timeout = 30000")
    _ensure_wal(con)
    if con.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        con.execute("BEGIN IMMEDIATE")
        for statement in _DDL.split(";"):  # not executescript(): it would commit the open transaction
            if statement.strip():
                con.execute(statement)
        con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        con.execute("COMMIT")
    return con


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def start_of_day(when: dt.datetime) -> dt.datetime:
    """00:00 UTC of ``when``'s UTC date: the daily cap resets then."""
    t = when.astimezone(dt.timezone.utc)
    return t.replace(hour=0, minute=0, second=0, microsecond=0)


def _iso(when: dt.datetime) -> str:
    if when.tzinfo is None:
        raise ValueError("ledger times must be aware datetimes")
    return when.astimezone(dt.timezone.utc).isoformat(timespec="microseconds")


def _where(since_utc: dt.datetime | None, session_id: str | None) -> tuple[str, list]:
    clauses, args = [], []
    if since_utc is not None:
        clauses.append("ts_utc >= ?")
        args.append(_iso(since_utc))
    if session_id is not None:
        clauses.append("session_id = ?")
        args.append(session_id)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", args


def _spent(con: sqlite3.Connection, since_utc: dt.datetime | None, session_id: str | None) -> float:
    where, args = _where(since_utc, session_id)
    return float(con.execute(f"SELECT COALESCE(SUM(cost_usd), 0) FROM calls{where}", args).fetchone()[0])


def spent(since_utc: dt.datetime | None = None, session_id: str | None = None) -> float:
    """USD spent (estimated rows included) since ``since_utc`` and/or in ``session_id``."""
    with closing(connect()) as con:
        return _spent(con, since_utc, session_id)


def totals(since_utc: dt.datetime | None = None, session_id: str | None = None) -> Totals:
    where, args = _where(since_utc, session_id)
    with closing(connect()) as con:
        row = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(prompt_cache_hit),0), COALESCE(SUM(prompt_cache_miss),0), "
            "COALESCE(SUM(completion),0), COALESCE(SUM(reasoning),0), COALESCE(SUM(cost_usd),0), "
            f"COALESCE(SUM(estimated),0) FROM calls{where}", args,
        ).fetchone()
    return Totals(*row[:5], float(row[5]), row[6])


def _over(spent_usd: float, worst: float, cap: float) -> bool:
    # cost_usd is REAL, so sums pick up float noise (15 x 0.20 = 3.0000000000000004).
    # Compare at nanodollar resolution, far below any price DeepSeek charges.
    return round(spent_usd + worst, 9) > round(cap, 9)


def reserve(*, session_id: str, model: str, when: dt.datetime, peak: bool, input_tokens: int, max_tokens: int,
            worst_case: Decimal, session_cap: float, daily_cap: float) -> int:
    """Check both caps against the worst case and, in the same transaction, write the reservation row.

    Raises CapExceeded (and writes nothing) if either cap would be crossed.
    """
    worst = float(worst_case)
    with closing(connect()) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            in_session = _spent(con, None, session_id)
            if _over(in_session, worst, session_cap):
                raise CapExceeded("session", session_cap, in_session, worst)
            today = _spent(con, start_of_day(when), None)
            if _over(today, worst, daily_cap):
                raise CapExceeded("daily", daily_cap, today, worst)
            cur = con.execute(
                "INSERT INTO calls (ts_utc, model, peak, prompt_cache_hit, prompt_cache_miss, completion, reasoning, "
                "cost_usd, estimated, session_id) VALUES (?, ?, ?, 0, ?, ?, 0, ?, 1, ?)",
                (_iso(when), model, int(peak), input_tokens, max_tokens, worst, session_id),
            )
            con.execute("COMMIT")
            return int(cur.lastrowid)
        except BaseException:
            con.execute("ROLLBACK")
            raise


def settle(row_id: int, usage: Usage, cost_usd: Decimal, estimated: bool) -> None:
    """Replace a reservation with the billed numbers (estimated=False) or an interrupted call's estimate."""
    with closing(connect()) as con:
        con.execute(
            "UPDATE calls SET prompt_cache_hit = ?, prompt_cache_miss = ?, completion = ?, reasoning = ?, "
            "cost_usd = ?, estimated = ? WHERE id = ?",
            (usage.prompt_cache_hit, usage.prompt_cache_miss, usage.completion, usage.reasoning,
             float(cost_usd), int(estimated), row_id),
        )


def cancel(row_id: int) -> None:
    """Drop a reservation for a request that was never answered with a stream, so never billed."""
    with closing(connect()) as con:
        con.execute("DELETE FROM calls WHERE id = ?", (row_id,))


def row(row_id: int) -> dict | None:
    with closing(connect()) as con:
        con.row_factory = sqlite3.Row
        r = con.execute("SELECT * FROM calls WHERE id = ?", (row_id,)).fetchone()
    return dict(r) if r else None


def rows(session_id: str | None = None) -> list[dict]:
    where, args = _where(None, session_id)
    with closing(connect()) as con:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(f"SELECT * FROM calls{where} ORDER BY id", args)]
