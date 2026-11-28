"""Queue migrations open real old files and keep every row (commit 11 follow-up A4)."""

from __future__ import annotations

import hashlib
import sqlite3

import pytest
from queue_fixtures import V3_STATUSES, build_queue, dump, sample_changesets, sample_proposals, table_sql

from daedalus.core import queue
from daedalus.terminal import ledger


@pytest.fixture
def v3(state_root, monkeypatch):
    """A v3 queue with a row in every v3 status and a multi-path change set, plus a ledger with rows.

    Reconciliation is off: the 'applying' rows here point at no real repo, and this test is about the
    migration keeping them byte for byte (reconciliation has its own tests)."""
    monkeypatch.setattr(queue, "_reconciling", True)
    build_queue(queue.queue_path(), 3)
    with ledger.connect() as con:
        for i in range(3):
            con.execute("INSERT INTO calls (ts_utc, model, peak, prompt_cache_hit, prompt_cache_miss, completion, "
                        "reasoning, cost_usd, estimated, session_id, run_id) VALUES (?, 'deepseek-flash', 0, 1, 2, 3, "
                        "4, 0.01, 0, 's1', ?)", (f"2026-11-27T0{i}:00:00+00:00", "r1" if i else None))
    return queue.queue_path()


def _ledger_digest() -> str:
    con = sqlite3.connect(ledger.path())
    try:
        rows = con.execute("SELECT * FROM calls ORDER BY id").fetchall()
        return hashlib.sha256(repr((con.execute("PRAGMA user_version").fetchone(), rows)).encode()).hexdigest()
    finally:
        con.close()


def test_the_fixture_really_is_v3(v3) -> None:
    before = dump(v3)
    assert before["user_version"] == 3 and [p["status"] for p in before["proposals"]] == list(V3_STATUSES)
    assert "superseded" not in table_sql(v3, "proposals")
    con = sqlite3.connect(v3)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("UPDATE proposals SET status = 'superseded' WHERE id = ?", (before["proposals"][0]["id"],))
    finally:
        con.close()


def test_v3_to_v4_keeps_every_row_and_column(v3) -> None:
    before = dump(v3)
    ledger_before = _ledger_digest()
    with queue.connect() as con:
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    after = dump(v3)
    assert after["user_version"] == queue.SCHEMA_VERSION >= 4
    assert len(after["proposals"]) == len(before["proposals"]) == len(V3_STATUSES)
    assert after["proposals"] == before["proposals"] == sample_proposals()  # every column of every row
    sets, paths = sample_changesets()
    assert after["changesets"] == before["changesets"] and len(after["changesets"]) == len(sets)
    assert after["changeset_paths"] == before["changeset_paths"] and len(after["changeset_paths"]) == len(paths)
    assert [p[5] for p in after["changeset_paths"]][:3] == [b"# x\n", b"answered: true\n", None]  # BLOBs intact
    assert "superseded" in table_sql(v3, "proposals")
    assert table_sql(v3, "proposals_by_status") is not None and table_sql(v3, "proposals_new") is None
    assert _ledger_digest() == ledger_before  # the queue migration never touches the ledger
    assert queue.transition(before["proposals"][0]["id"], "pending", "superseded")  # the new status works


class _FailsMidRebuild(sqlite3.Connection):
    """Raises after the rows are copied into proposals_new, before the old table is dropped."""

    def execute(self, sql, *args):
        if sql.strip() == "DROP TABLE proposals":
            raise sqlite3.OperationalError("injected: disk I/O error")
        return super().execute(sql, *args)


def test_a_failed_rebuild_leaves_the_file_at_v3_with_every_row(v3, monkeypatch) -> None:
    before = dump(v3)
    real_connect = sqlite3.connect
    monkeypatch.setattr(queue.sqlite3, "connect", lambda *a, **k: real_connect(*a, factory=_FailsMidRebuild, **k))
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        queue.connect()
    monkeypatch.setattr(queue.sqlite3, "connect", real_connect)
    assert dump(v3) == before
    assert "superseded" not in table_sql(v3, "proposals") and table_sql(v3, "proposals_new") is None
    con = real_connect(v3)
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        con.close()


# v4 → v5 (commit 12): plan runs. Only tables are added; the proposals table isn't rebuilt. -------

@pytest.fixture
def v4(state_root, monkeypatch):
    monkeypatch.setattr(queue, "_reconciling", True)
    build_queue(queue.queue_path(), 4, proposals=sample_proposals((*V3_STATUSES, "superseded")))
    return queue.queue_path()


def test_v4_to_v5_adds_the_plan_run_tables_and_keeps_every_row(v4) -> None:
    before, proposals_sql = dump(v4), table_sql(v4, "proposals")
    assert before["user_version"] == 4 and table_sql(v4, "plan_runs") is None
    with queue.connect() as con:
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    after = dump(v4)
    assert after["user_version"] == queue.SCHEMA_VERSION >= 5
    assert {k: v for k, v in after.items() if k != "user_version"} == \
        {k: v for k, v in before.items() if k != "user_version"}
    assert table_sql(v4, "proposals") == proposals_sql, "v4 → v5 must not rebuild the proposals table"
    assert table_sql(v4, "plan_runs") and table_sql(v4, "plan_tasks") and table_sql(v4, "plan_runs_by_plan")


class _FailsAddingPlanTasks(sqlite3.Connection):
    def execute(self, sql, *args):
        if "CREATE TABLE IF NOT EXISTS plan_tasks" in sql:
            raise sqlite3.OperationalError("injected: disk I/O error")
        return super().execute(sql, *args)


def test_a_failed_v5_migration_leaves_the_file_at_v4(v4, monkeypatch) -> None:
    before = dump(v4)
    real_connect = sqlite3.connect
    monkeypatch.setattr(queue.sqlite3, "connect", lambda *a, **k: real_connect(*a, factory=_FailsAddingPlanTasks, **k))
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        queue.connect()
    monkeypatch.setattr(queue.sqlite3, "connect", real_connect)
    assert dump(v4) == before and table_sql(v4, "plan_runs") is None


# v5 → v6 (commit 12b): plan_runs is rebuilt for the final_suite stop reason; plan_tasks is untouched. ---------

@pytest.fixture
def v5(state_root, monkeypatch):
    monkeypatch.setattr(queue, "_reconciling", True)
    build_queue(queue.queue_path(), 5, proposals=sample_proposals((*V3_STATUSES, "superseded")))
    return queue.queue_path()


def _plan_rows(path):
    con = sqlite3.connect(path)
    try:
        return (con.execute("SELECT * FROM plan_runs ORDER BY id").fetchall(),
                con.execute("SELECT * FROM plan_tasks ORDER BY plan_run_id, idx").fetchall())
    finally:
        con.close()


def test_v5_to_v6_rebuilds_plan_runs_keeping_every_row(v5) -> None:
    before, (runs_before, tasks_before) = dump(v5), _plan_rows(v5)
    tasks_sql = table_sql(v5, "plan_tasks")
    assert len(runs_before) == 3 and len(tasks_before) == 4 and "final_suite" not in table_sql(v5, "plan_runs")
    with queue.connect() as con:
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    runs_after, tasks_after = _plan_rows(v5)
    assert dump(v5)["user_version"] == 6
    assert [r[:-1] for r in runs_after] == runs_before and all(r[-1] is None for r in runs_after)  # + final_output
    assert tasks_after == tasks_before and table_sql(v5, "plan_tasks") == tasks_sql
    assert {k: v for k, v in dump(v5).items() if k != "user_version"} == \
        {k: v for k, v in before.items() if k != "user_version"}
    assert "final_suite" in table_sql(v5, "plan_runs") and table_sql(v5, "plan_runs_by_plan")
    from daedalus.core import planruns

    planruns.set_run("pr000000000001", "failed", reason="final_suite", text="t", final_output="1 failed")
    assert planruns.get("pr000000000001").final_output == "1 failed"


class _FailsCopyingPlanRuns(sqlite3.Connection):
    def execute(self, sql, *args):
        if sql.strip() == "DROP TABLE plan_runs":
            raise sqlite3.OperationalError("injected: disk I/O error")
        return super().execute(sql, *args)


def test_a_failed_v6_migration_leaves_the_file_at_v5(v5, monkeypatch) -> None:
    before, rows = dump(v5), _plan_rows(v5)
    real_connect = sqlite3.connect
    monkeypatch.setattr(queue.sqlite3, "connect", lambda *a, **k: real_connect(*a, factory=_FailsCopyingPlanRuns, **k))
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        queue.connect()
    monkeypatch.setattr(queue.sqlite3, "connect", real_connect)
    assert dump(v5) == before and _plan_rows(v5) == rows and table_sql(v5, "plan_runs_new") is None
