"""Queue files at older schema versions, for migration tests (commit 11 follow-up A4; reused by commit 12's v5).

The DDL here is a frozen copy of what each version shipped, never derived from
the current code, so a change to queue.py can't quietly change the "old" file a
migration test starts from.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

V3_PROPOSALS = """
CREATE TABLE proposals (
    id            TEXT PRIMARY KEY,
    vault         TEXT NOT NULL,
    rel_path      TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('create', 'edit')),
    content       TEXT NOT NULL,
    base_sha256   TEXT,
    base_content  TEXT,
    source        TEXT NOT NULL CHECK (source IN ('deepseek', 'claude', 'human')),
    summary       TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    status        TEXT NOT NULL
                  CHECK (status IN ('pending', 'applying', 'applied', 'rejected', 'stale', 'failed')),
    status_reason TEXT,
    commit_sha    TEXT,
    target_path   TEXT,
    repo_path     TEXT,
    base_head     TEXT
)"""
V4_PROPOSALS = V3_PROPOSALS.replace("'stale', 'failed')", "'stale', 'failed',\n'superseded')")
CHANGESETS = (
    """CREATE TABLE changesets (
        id            TEXT PRIMARY KEY,
        repo_path     TEXT NOT NULL,
        base_head     TEXT,
        summary       TEXT NOT NULL,
        status        TEXT NOT NULL CHECK (status IN ('applying', 'applied', 'failed')),
        status_reason TEXT,
        commit_sha    TEXT,
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL
    )""",
    """CREATE TABLE changeset_paths (
        changeset_id  TEXT NOT NULL REFERENCES changesets (id),
        seq           INTEGER NOT NULL,
        target_path   TEXT NOT NULL,
        prior         BLOB,
        prior_mode    INTEGER,
        new           BLOB,
        staged        INTEGER NOT NULL,
        PRIMARY KEY (changeset_id, seq)
    )""",
)
INDEX = "CREATE INDEX proposals_by_status ON proposals (status, vault, id)"

_REASONS = ("'question', 'checkin', 'daily_cap', 'interrupted', 'preflight', 'max_iterations', 'budget', "
            "'honest_done_refused'")
_RUN_STATUSES = "'pending', 'running', 'done', 'stopped', 'failed'"
V5_PLAN_RUNS = (
    f"""CREATE TABLE plan_runs (
        id             TEXT PRIMARY KEY,
        plan_change_id TEXT NOT NULL,
        project        TEXT NOT NULL,
        slug           TEXT NOT NULL,
        version        INTEGER NOT NULL,
        branch         TEXT NOT NULL,
        worktree_path  TEXT NOT NULL,
        wt_slug        TEXT NOT NULL,
        status         TEXT NOT NULL CHECK (status IN ({_RUN_STATUSES})),
        stop_reason    TEXT CHECK (stop_reason IS NULL OR stop_reason IN ({_REASONS})),
        stop_text      TEXT,
        started_at     TEXT NOT NULL,
        ended_at       TEXT,
        updated_at     TEXT NOT NULL
    )""",
    "CREATE INDEX plan_runs_by_plan ON plan_runs (project, plan_change_id)",
    f"""CREATE TABLE plan_tasks (
        plan_run_id  TEXT NOT NULL REFERENCES plan_runs (id),
        task_id      TEXT NOT NULL,
        idx          INTEGER NOT NULL,
        summary      TEXT NOT NULL,
        status       TEXT NOT NULL CHECK (status IN ({_RUN_STATUSES})),
        stop_reason  TEXT CHECK (stop_reason IS NULL OR stop_reason IN ({_REASONS})),
        run_id       TEXT,
        base         TEXT,
        baseline     TEXT,
        iterations   INTEGER NOT NULL DEFAULT 0,
        cost_usd     REAL NOT NULL DEFAULT 0,
        text         TEXT,
        started_at   TEXT,
        ended_at     TEXT,
        PRIMARY KEY (plan_run_id, task_id)
    )""",
)
SAMPLE_PLAN_ROWS = (
    "INSERT INTO plan_runs VALUES ('pr000000000001', '0000000000p001', 'alpha', 'mathx', 2, 'agent/plan-mathx-v2', "
    "'/wt/a', 'plan-mathx-v2', 'done', NULL, NULL, '2026-11-27T01:00:00+00:00', '2026-11-27T02:00:00+00:00', "
    "'2026-11-27T02:00:00+00:00')",
    "INSERT INTO plan_runs VALUES ('pr000000000002', '0000000000p002', 'alpha', 'cli', 1, 'agent/plan-cli-v1', '/wt/b', "
    "'plan-cli-v1', 'stopped', 'question', 't1: asked q-x', '2026-11-27T03:00:00+00:00', NULL, "
    "'2026-11-27T03:30:00+00:00')",
    "INSERT INTO plan_runs VALUES ('pr000000000003', '0000000000p003', 'beta', 'io', 1, 'agent/plan-io-v1', '/wt/c', "
    "'plan-io-v1', 'failed', 'preflight', 't1: red test already passes', '2026-11-27T04:00:00+00:00', "
    "'2026-11-27T04:01:00+00:00', '2026-11-27T04:01:00+00:00')",
    "INSERT INTO plan_tasks VALUES ('pr000000000001', 't1', 1, 'multiply', 'done', NULL, 'r1', 'abc', "
    "'{\"exit_code\": 1}', 1, 0.0021, NULL, 's', 'e')",
    "INSERT INTO plan_tasks VALUES ('pr000000000001', 't2', 2, 'cli', 'done', NULL, 'r2', 'def', "
    "'{\"empty\": true}', 2, 0.0042, NULL, 's', 'e')",
    "INSERT INTO plan_tasks VALUES ('pr000000000002', 't1', 1, 'cli', 'stopped', 'question', 'r3', 'ghi', NULL, 1, "
    "0.001, 'asked q-x', 's', NULL)",
    "INSERT INTO plan_tasks VALUES ('pr000000000003', 't1', 1, 'io', 'failed', 'preflight', NULL, NULL, NULL, 0, 0, "
    "'red test already passes', NULL, 'e')",
)

DDL = {3: (V3_PROPOSALS, *CHANGESETS, INDEX), 4: (V4_PROPOSALS, *CHANGESETS, INDEX),
       5: (V4_PROPOSALS, *CHANGESETS, INDEX, *V5_PLAN_RUNS)}
SAMPLE_ROWS = {5: SAMPLE_PLAN_ROWS}

COLUMNS = ("id", "vault", "rel_path", "kind", "content", "base_sha256", "base_content", "source", "summary",
           "created_at", "updated_at", "status", "status_reason", "commit_sha", "target_path", "repo_path",
           "base_head")
V3_STATUSES = ("pending", "applying", "applied", "rejected", "stale", "failed")


def sample_proposals(statuses=V3_STATUSES) -> list[dict[str, Any]]:
    """One row per status, every column filled with a distinct value (NULLs too, where a column allows one)."""
    rows = []
    for i, status in enumerate(statuses):
        rows.append({
            "id": f"0000000000{i:04d}", "vault": "alpha", "rel_path": f"inbox/n{i}.md",
            "kind": "edit" if i % 2 else "create", "content": f"content {i} ✓\n",
            "base_sha256": f"{i:064x}" if i % 2 else None, "base_content": f"old {i}\n" if i % 2 else None,
            "source": ("deepseek", "claude", "human")[i % 3], "summary": f"summary {i}",
            "created_at": f"2026-11-2{i}T00:00:00+00:00", "updated_at": f"2026-11-2{i}T01:00:00+00:00",
            "status": status, "status_reason": f"reason {i}" if status in ("rejected", "stale", "failed") else None,
            "commit_sha": f"{i:040x}" if status == "applied" else None,
            "target_path": f"/repo/vault/inbox/n{i}.md" if status in ("applying", "applied") else None,
            "repo_path": "/repo" if status in ("applying", "applied") else None,
            "base_head": f"{i + 100:040x}" if status in ("applying", "applied") else None,
        })
    return rows


def sample_changesets() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Two change sets: an applied three-path one (a create, an edit, a delete) and a failed one-path one."""
    sets = [
        {"id": "cs000000000001", "repo_path": "/repo", "base_head": "a" * 40, "summary": "answer q-x",
         "status": "applied", "status_reason": None, "commit_sha": "b" * 40,
         "created_at": "2026-11-26T00:00:00+00:00", "updated_at": "2026-11-26T00:00:01+00:00"},
        {"id": "cs000000000002", "repo_path": "/repo", "base_head": None, "summary": "add: paper.pdf",
         "status": "failed", "status_reason": "interrupted", "commit_sha": None,
         "created_at": "2026-11-26T02:00:00+00:00", "updated_at": "2026-11-26T02:00:01+00:00"},
    ]
    paths = [
        {"changeset_id": "cs000000000001", "seq": 0, "target_path": "/repo/vault/decisions/x.md", "prior": None,
         "prior_mode": None, "new": b"# x\n", "staged": 1},
        {"changeset_id": "cs000000000001", "seq": 1, "target_path": "/repo/vault/questions/q-x.md",
         "prior": b"answered: false\n\x00binary", "prior_mode": 0o100644, "new": b"answered: true\n", "staged": 0},
        {"changeset_id": "cs000000000001", "seq": 2, "target_path": "/repo/vault/inbox/x.md", "prior": b"# draft\n",
         "prior_mode": 0o100600, "new": None, "staged": 0},
        {"changeset_id": "cs000000000002", "seq": 0, "target_path": "/repo/vault/sources/paper.md", "prior": None,
         "prior_mode": None, "new": b"# paper\n", "staged": 1},
    ]
    return sets, paths


def _insert(con: sqlite3.Connection, table: str, rows: list[dict[str, Any]]) -> None:
    for r in rows:
        con.execute(f"INSERT INTO {table} ({', '.join(r)}) VALUES ({', '.join('?' * len(r))})", tuple(r.values()))


def build_queue(path: Path, version: int, *, proposals=None, changesets=None, extra_sql: tuple[str, ...] = ()) -> None:
    """A queue file at ``version`` holding the sample rows (or the ones given), in WAL mode like the real one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, isolation_level=None)
    try:
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("BEGIN")
        for ddl in DDL[version]:
            con.execute(ddl)
        _insert(con, "proposals", sample_proposals() if proposals is None else proposals)
        sets, paths = sample_changesets() if changesets is None else changesets
        _insert(con, "changesets", sets)
        _insert(con, "changeset_paths", paths)
        for sql in (*SAMPLE_ROWS.get(version, ()), *extra_sql):
            con.execute(sql)
        con.execute(f"PRAGMA user_version = {version}")
        con.execute("COMMIT")
    finally:
        con.close()


def dump(path: Path) -> dict[str, Any]:
    """Every row of every queue table, by primary key, plus the schema version."""
    con = sqlite3.connect(path)
    try:
        out: dict[str, Any] = {"user_version": con.execute("PRAGMA user_version").fetchone()[0]}
        out["proposals"] = [dict(zip(COLUMNS, r)) for r in
                            con.execute(f"SELECT {', '.join(COLUMNS)} FROM proposals ORDER BY id")]
        out["changesets"] = [r for r in con.execute("SELECT * FROM changesets ORDER BY id")]
        out["changeset_paths"] = [r for r in con.execute("SELECT * FROM changeset_paths ORDER BY changeset_id, seq")]
        return out
    finally:
        con.close()


def table_sql(path: Path, name: str) -> str | None:
    con = sqlite3.connect(path)
    try:
        row = con.execute("SELECT sql FROM sqlite_master WHERE name = ?", (name,)).fetchone()
        return row[0] if row else None
    finally:
        con.close()
