"""The proposal queue: SQLite at ~/.local/state/daedalus/queue.sqlite.

The queue lives outside every vault. The terminal and daedalus-mcp are
separate processes that write it at the same time, so it runs in WAL mode
with a busy timeout, and every operation is one short transaction on its
own connection.

Crash recovery: apply() marks a proposal "applying" (recording the target,
its repo, and HEAD) before it replaces the file. Every time the queue opens,
applying records whose apply is no longer running are reconciled: if a commit
carrying the proposal's Daedalus-Change trailer exists, the proposal is
applied; otherwise the old content is restored (or a create removed), the
path unstaged, and the proposal marked failed with reason "interrupted".

Change sets: a write of several paths as one commit (/answer, /promote, and
plan approval later). apply.apply_changes records one "applying" change set
with every path's prior and new content before it writes anything, so
reconciliation can restore all of them, or mark it applied if the commit
carrying its Daedalus-Change trailer landed.

Superseded (schema v4, commit 11): revising a plan marks its previous version
"superseded" if that version is still pending, in the same transaction that
inserts the revision. Nothing is ever deleted; every version stays readable.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from . import vaultio
from .errors import (  # re-exported: callers import these from daedalus.core.queue
    CommitFailed, DraftInvalid, LockBusy, NotPending, PlanApprovalNotYet, ProposalError, ProposalTooLarge,
    StaleProposal, UnknownProposal,
)

STATE_ROOT = Path("~/.local/state/daedalus").expanduser()
MAX_CONTENT_BYTES = 256 * 1024
SCHEMA_VERSION = 4

KINDS = ("create", "edit")
STATUSES = ("pending", "applying", "applied", "rejected", "stale", "failed", "superseded")

_TABLE = """
CREATE TABLE {name} (
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
                  CHECK (status IN ('pending', 'applying', 'applied', 'rejected', 'stale', 'failed',
                                    'superseded')),
    status_reason TEXT,
    commit_sha    TEXT,
    target_path   TEXT,  -- set when applying: the authorized real path
    repo_path     TEXT,  -- set when applying: the repo it commits into
    base_head     TEXT   -- set when applying: HEAD before the write
)
"""
_CHANGESETS = (
    """CREATE TABLE IF NOT EXISTS changesets (
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
    """CREATE TABLE IF NOT EXISTS changeset_paths (
        changeset_id  TEXT NOT NULL REFERENCES changesets (id),
        seq           INTEGER NOT NULL,
        target_path   TEXT NOT NULL,  -- the authorized real path
        prior         BLOB,           -- NULL: the path didn't exist
        prior_mode    INTEGER,
        new           BLOB,           -- NULL: the change deletes the path
        staged        INTEGER NOT NULL,  -- 1 if apply git-added it (a new or untracked path)
        PRIMARY KEY (changeset_id, seq)
    )""",
)
_V1_COLUMNS = (
    "id", "vault", "rel_path", "kind", "content", "base_sha256", "base_content", "source", "summary",
    "created_at", "updated_at", "status", "status_reason", "commit_sha",
)
_COLUMNS = _V1_COLUMNS + ("target_path", "repo_path", "base_head")

# Lowercase Crockford base32, which sorts in the same order as the numbers it encodes.
_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"

_reconciling = False


@dataclass(frozen=True)
class Proposal:
    id: str
    vault: str
    rel_path: str
    kind: str
    content: str
    base_sha256: str | None
    base_content: str | None
    source: str
    summary: str
    created_at: str
    updated_at: str
    status: str
    status_reason: str | None
    commit_sha: str | None
    target_path: str | None = None
    repo_path: str | None = None
    base_head: str | None = None


def queue_path() -> Path:
    return STATE_ROOT / "queue.sqlite"


def connect() -> sqlite3.Connection:
    path = queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30, isolation_level=None)  # autocommit: one statement, one transaction
    con.execute("PRAGMA busy_timeout = 30000")
    _ensure_wal(con)
    con.execute("PRAGMA synchronous = NORMAL")
    _ensure_schema(con)
    if not _reconciling and not vaultio.lock_held_here():
        if con.execute("SELECT 1 FROM proposals WHERE status = 'applying' UNION ALL "
                       "SELECT 1 FROM changesets WHERE status = 'applying' LIMIT 1").fetchone():
            reconcile()
    return con


def _ensure_schema(con: sqlite3.Connection) -> None:
    """Create the tables, or migrate in place: v1 had no 'applying' status, v2 no change sets, v3 no
    'superseded' status. A status CHECK can't be altered in SQLite, so v1 and v3 rebuild the table, rows kept."""
    if con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION:
        return
    con.execute("BEGIN IMMEDIATE")
    try:
        version = con.execute("PRAGMA user_version").fetchone()[0]  # re-check under the lock
        if version < SCHEMA_VERSION:
            # One rebuild into the current definition covers every older status CHECK.
            exists = con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'proposals'").fetchone()
            con.execute("DROP TABLE IF EXISTS proposals_new")
            con.execute(_TABLE.format(name="proposals_new"))
            if exists:
                cols = ", ".join(_V1_COLUMNS if version < 2 else _COLUMNS)
                con.execute(f"INSERT INTO proposals_new ({cols}) SELECT {cols} FROM proposals")
                con.execute("DROP TABLE proposals")
            con.execute("ALTER TABLE proposals_new RENAME TO proposals")
            con.execute("CREATE INDEX IF NOT EXISTS proposals_by_status ON proposals (status, vault, id)")
        if version < 3:
            for ddl in _CHANGESETS:
                con.execute(ddl)
        if version < SCHEMA_VERSION:
            con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


def _ensure_wal(con: sqlite3.Connection, timeout: float = 30.0) -> None:
    """Switch to WAL once; WAL persists in the file, so later connections only read the mode.

    SQLite reports the switch as busy *without* calling the busy handler when
    another process holds the new database open, so the switch is retried here.
    """
    deadline = time.monotonic() + timeout
    delay = 0.005
    while con.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
        try:
            con.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) and "busy" not in str(e):
                raise
            if time.monotonic() > deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.2)


def _encode(n: int, width: int) -> str:
    out = []
    for _ in range(width):
        n, r = divmod(n, 32)
        out.append(_ALPHABET[r])
    return "".join(reversed(out))


def new_id() -> str:
    """14 characters: 9 encode the millisecond (sortable by time), 5 are random."""
    return _encode(time.time_ns() // 1_000_000, 9) + _encode(secrets.randbits(25), 5)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _row(row: tuple) -> Proposal:
    return Proposal(**dict(zip(_COLUMNS, row)))


def insert(
    *, vault: str, rel_path: str, kind: str, content: str, source: str, summary: str,
    base_sha256: str | None = None, base_content: str | None = None, supersede: str | None = None,
) -> Proposal:
    """Insert a pending proposal. With ``supersede``, that proposal is marked superseded in the same
    transaction if it's still pending (a plan revision); any other status is left as it is."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    size = len(content.encode("utf-8"))
    if size > MAX_CONTENT_BYTES:
        raise ProposalTooLarge(f"content is {size} bytes; the cap is {MAX_CONTENT_BYTES}")
    now = _now()
    with closing(connect()) as con:
        for _ in range(10):  # an id collision needs the same millisecond and 25 random bits
            pid = new_id()
            con.execute("BEGIN IMMEDIATE")
            try:
                con.execute(
                    f"INSERT INTO proposals ({', '.join(_V1_COLUMNS)}) VALUES ({', '.join('?' * len(_V1_COLUMNS))})",
                    (pid, vault, rel_path, kind, content, base_sha256, base_content, source, summary,
                     now, now, "pending", None, None),
                )
                if supersede is not None:
                    con.execute("UPDATE proposals SET status = 'superseded', status_reason = ?, updated_at = ? "
                                "WHERE id = ? AND status = 'pending'", (f"superseded by {pid}", now, supersede))
                con.execute("COMMIT")
            except sqlite3.IntegrityError:
                con.execute("ROLLBACK")
                continue
            except BaseException:
                con.execute("ROLLBACK")
                raise
            return get(pid, con)
    raise ProposalError("could not allocate a unique proposal id")


def get(change_id: str, con: sqlite3.Connection | None = None) -> Proposal:
    if con is None:
        with closing(connect()) as own:
            return get(change_id, own)
    row = con.execute(f"SELECT {', '.join(_COLUMNS)} FROM proposals WHERE id = ?", (change_id,)).fetchone()
    if row is None:
        raise UnknownProposal(f"no proposal {change_id!r}")
    return _row(row)


def transition(
    change_id: str, from_status: str, to_status: str, *, reason: str | None = None, commit_sha: str | None = None,
    **applying: str | None,
) -> bool:
    """Compare-and-set the status; False if the record wasn't in ``from_status``.

    ``applying`` may set target_path, repo_path, base_head.
    """
    if to_status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    unknown = set(applying) - {"target_path", "repo_path", "base_head"}
    if unknown:
        raise ValueError(f"unknown columns {sorted(unknown)}")
    sets = ["status = ?", "status_reason = ?", "commit_sha = COALESCE(?, commit_sha)", "updated_at = ?"]
    args: list[object] = [to_status, reason, commit_sha, _now()]
    for col, value in applying.items():
        sets.append(f"{col} = ?")
        args.append(value)
    with closing(connect()) as con:
        cur = con.execute(
            f"UPDATE proposals SET {', '.join(sets)} WHERE id = ? AND status = ?", (*args, change_id, from_status)
        )
        return cur.rowcount == 1


def select(status: str | None = None, vault: str | None = None, newest_first: bool = False,
           limit: int | None = None) -> list[Proposal]:
    where, args = [], []
    if status is not None:
        where.append("status = ?")
        args.append(status)
    if vault is not None:
        where.append("vault = ?")
        args.append(vault)
    sql = f"SELECT {', '.join(_COLUMNS)} FROM proposals"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at DESC, id DESC" if newest_first else " ORDER BY id"
    if limit is not None:
        sql += " LIMIT ?"
        args.append(limit)
    with closing(connect()) as con:
        return [_row(r) for r in con.execute(sql, args)]


def statuses(ids: Iterable[str] | None = None) -> dict[str, str]:
    """{id: status} for every record (or just ``ids``); used by integrity checks."""
    with closing(connect()) as con:
        rows = con.execute("SELECT id, status FROM proposals").fetchall()
    wanted = set(ids) if ids is not None else None
    return {pid: st for pid, st in rows if wanted is None or pid in wanted}


# Crash recovery -------------------------------------------------------------------------

INTERRUPTED = "interrupted"


def reconcile() -> list[str]:
    """Settle every 'applying' record whose apply is no longer running; returns their ids.

    Taking the write lock without waiting is the test for "no longer running":
    a live apply holds it for its whole duration. If it's held, try again next open.
    """
    global _reconciling
    if _reconciling or vaultio.lock_held_here():
        return []
    try:
        lock = vaultio.write_lock(timeout=0)
        lock.__enter__()
    except LockBusy:
        return []
    _reconciling = True
    try:
        settled = []
        for rec in select(status="applying"):
            try:
                _reconcile_one(rec)
                settled.append(rec.id)
            except Exception:
                continue  # leave it applying; the next open retries
        for cs in changesets(status="applying"):
            try:
                _reconcile_changeset(cs)
                settled.append(cs.id)
            except Exception:
                continue
        return settled
    finally:
        _reconciling = False
        lock.__exit__(None, None, None)


def _reconcile_one(rec: Proposal) -> None:
    repo = Path(rec.repo_path) if rec.repo_path else None
    target = Path(rec.target_path) if rec.target_path else None
    if repo is None or target is None:
        transition(rec.id, "applying", "failed", reason=f"{INTERRUPTED}; no target was recorded")
        return

    sha = vaultio.find_commit_with_trailer(repo, rec.id, since=rec.base_head)
    if sha is not None:  # the commit landed; only the status update was lost
        transition(rec.id, "applying", "applied", commit_sha=sha)
        return

    reason = INTERRUPTED
    ours = rec.content.encode("utf-8")
    old = None if rec.kind == "create" else (rec.base_content or "").encode("utf-8")
    dirfd = vaultio.open_dir_nofollow(target.parent)
    try:
        current = vaultio.read_at(dirfd, target.name)
        now = None if current is None else current[0]
        if now == ours:  # our write is in place: undo it
            vaultio.restore_at(dirfd, target.name, None if old is None else (old, current[1]))
        elif now == old:  # the crash came before the replace: nothing to undo
            pass
        else:  # someone changed the file after the crash: don't overwrite their work
            reason = f"{INTERRUPTED}; the target has changed since, so it was left as is"
    finally:
        os.close(dirfd)
    # apply() stages only paths HEAD doesn't have (creates, untracked drafts),
    # so only those can be left staged by a crash.
    rel = os.path.relpath(target, repo)
    if not vaultio.in_head(repo, rel):
        vaultio.unstage(repo, rel)
    transition(rec.id, "applying", "failed", reason=reason)


# Change sets ------------------------------------------------------------------------------

@dataclass(frozen=True)
class ChangePath:
    target_path: str
    prior: bytes | None
    prior_mode: int | None
    new: bytes | None
    staged: bool


@dataclass(frozen=True)
class ChangeSet:
    id: str
    repo_path: str
    base_head: str | None
    summary: str
    status: str
    status_reason: str | None
    commit_sha: str | None
    paths: tuple[ChangePath, ...]


def begin_changeset(change_id: str, repo: Path, base_head: str | None, summary: str,
                    paths: list[ChangePath]) -> None:
    """Record a change set as applying, with every path's prior and new content, in one transaction."""
    now = _now()
    with closing(connect()) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute("INSERT INTO changesets (id, repo_path, base_head, summary, status, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, 'applying', ?, ?)", (change_id, str(repo), base_head, summary, now, now))
            con.executemany(
                "INSERT INTO changeset_paths (changeset_id, seq, target_path, prior, prior_mode, new, staged) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(change_id, i, cp.target_path, cp.prior, cp.prior_mode, cp.new, int(cp.staged))
                 for i, cp in enumerate(paths)])
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise


def finish_changeset(change_id: str, status: str, *, reason: str | None = None, commit_sha: str | None = None) -> bool:
    if status not in ("applied", "failed"):
        raise ValueError("a change set finishes applied or failed")
    with closing(connect()) as con:
        cur = con.execute("UPDATE changesets SET status = ?, status_reason = ?, commit_sha = ?, updated_at = ? "
                          "WHERE id = ? AND status = 'applying'", (status, reason, commit_sha, _now(), change_id))
        return cur.rowcount == 1


def changesets(status: str | None = None, change_id: str | None = None) -> list[ChangeSet]:
    where, args = [], []
    if status is not None:
        where.append("status = ?")
        args.append(status)
    if change_id is not None:
        where.append("id = ?")
        args.append(change_id)
    sql = "SELECT id, repo_path, base_head, summary, status, status_reason, commit_sha FROM changesets"
    if where:
        sql += " WHERE " + " AND ".join(where)
    with closing(connect()) as con:
        out = []
        for row in con.execute(sql + " ORDER BY id", args).fetchall():
            paths = tuple(ChangePath(t, p, m, n, bool(st)) for t, p, m, n, st in con.execute(
                "SELECT target_path, prior, prior_mode, new, staged FROM changeset_paths WHERE changeset_id = ? "
                "ORDER BY seq", (row[0],)))
            out.append(ChangeSet(*row, paths=paths))
        return out


def _reconcile_changeset(cs: ChangeSet) -> None:
    """Applied if its commit landed; otherwise every path we wrote goes back to its prior content."""
    repo = Path(cs.repo_path)
    sha = vaultio.find_commit_with_trailer(repo, cs.id, since=cs.base_head)
    if sha is not None:
        finish_changeset(cs.id, "applied", commit_sha=sha)
        return
    left = []
    for cp in cs.paths:
        target = Path(cp.target_path)
        dirfd = vaultio.open_dir_nofollow(target.parent)
        try:
            current = vaultio.read_at(dirfd, target.name)
            now = None if current is None else current[0]
            if now == cp.new:  # our write (or delete) is in place: undo it
                prior = None if cp.prior is None else (cp.prior, cp.prior_mode or 0o644)
                vaultio.restore_at(dirfd, target.name, prior)
            elif now != cp.prior:  # changed by someone after the crash: leave it
                left.append(target.name)
        finally:
            os.close(dirfd)
        if cp.staged:
            vaultio.unstage(repo, os.path.relpath(target, repo))
    reason = INTERRUPTED if not left else f"{INTERRUPTED}; changed since, so left as is: {', '.join(left)}"
    finish_changeset(cs.id, "failed", reason=reason)
