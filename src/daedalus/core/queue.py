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
    CommitFailed, DraftInvalid, LockBusy, NotPending, ProposalError, ProposalTooLarge, StaleProposal,
    UnknownProposal,
)

STATE_ROOT = Path("~/.local/state/daedalus").expanduser()
MAX_CONTENT_BYTES = 256 * 1024
SCHEMA_VERSION = 2

KINDS = ("create", "edit")
STATUSES = ("pending", "applying", "applied", "rejected", "stale", "failed")

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
                  CHECK (status IN ('pending', 'applying', 'applied', 'rejected', 'stale', 'failed')),
    status_reason TEXT,
    commit_sha    TEXT,
    target_path   TEXT,  -- set when applying: the authorized real path
    repo_path     TEXT,  -- set when applying: the repo it commits into
    base_head     TEXT   -- set when applying: HEAD before the write
)
"""
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
        if con.execute("SELECT 1 FROM proposals WHERE status = 'applying' LIMIT 1").fetchone():
            reconcile()
    return con


def _ensure_schema(con: sqlite3.Connection) -> None:
    """Create the table, or migrate a version-1 queue (no 'applying' status) in place."""
    if con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION:
        return
    con.execute("BEGIN IMMEDIATE")
    try:
        if con.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:  # re-check under the lock
            exists = con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'proposals'").fetchone()
            con.execute(_TABLE.format(name="proposals_v2"))
            if exists:
                cols = ", ".join(_V1_COLUMNS)
                con.execute(f"INSERT INTO proposals_v2 ({cols}) SELECT {cols} FROM proposals")
                con.execute("DROP TABLE proposals")
            con.execute("ALTER TABLE proposals_v2 RENAME TO proposals")
            con.execute("CREATE INDEX proposals_by_status ON proposals (status, vault, id)")
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
    base_sha256: str | None = None, base_content: str | None = None,
) -> Proposal:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    size = len(content.encode("utf-8"))
    if size > MAX_CONTENT_BYTES:
        raise ProposalTooLarge(f"content is {size} bytes; the cap is {MAX_CONTENT_BYTES}")
    now = _now()
    with closing(connect()) as con:
        for _ in range(10):  # an id collision needs the same millisecond and 25 random bits
            pid = new_id()
            try:
                con.execute(
                    f"INSERT INTO proposals ({', '.join(_V1_COLUMNS)}) VALUES ({', '.join('?' * len(_V1_COLUMNS))})",
                    (pid, vault, rel_path, kind, content, base_sha256, base_content, source, summary,
                     now, now, "pending", None, None),
                )
                return get(pid, con)
            except sqlite3.IntegrityError:
                continue
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
