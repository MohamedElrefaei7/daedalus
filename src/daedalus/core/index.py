"""Full-text search: one SQLite FTS5 database per vault, kept outside the vault.

The database lives at ~/.cache/daedalus/index/<sha1 of vault realpath>.sqlite.
Indexing is system state, not an agent write, so it never touches a vault:
the shared vault stays untouched and every vault's git status stays clean.

Freshness: each file's (rel_path, mtime_ns, size) is stored. Before every
query the vault is walked with os.scandir; new and changed files are
re-indexed and deleted ones dropped. Dot-folders (.obsidian/, .git/,
.daedalus/, .trash/, ...) and symlinks are skipped, and only *.md is indexed.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from pathlib import Path
from typing import Iterator, NamedTuple

CACHE_ROOT = Path("~/.cache/daedalus").expanduser()
SCHEMA_VERSION = 1

_TOKEN = re.compile(r"\w+")
_QUERY_PART = re.compile(r'"([^"]*)"|(\S+)')


class SearchUnavailable(RuntimeError):
    """This Python's SQLite can't do full-text search."""


_fts5_checked = False


def require_fts5() -> None:
    """Fail with a clear message if SQLite lacks FTS5."""
    global _fts5_checked
    if _fts5_checked:
        return
    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE VIRTUAL TABLE probe USING fts5(x)")
    except sqlite3.OperationalError as e:
        raise SearchUnavailable(
            f"daedalus search needs SQLite with the FTS5 extension, but the SQLite "
            f"{sqlite3.sqlite_version} linked into this Python lacks it ({e}). "
            "Use a Python build whose sqlite3 includes FTS5 (python.org, Homebrew, uv, and conda builds do)."
        ) from None
    finally:
        con.close()
    _fts5_checked = True


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)


def walk_markdown(
    root: str | os.PathLike[str], stat: bool = False
) -> Iterator[tuple[str, tuple[int, int] | None]]:
    """Yield ("/"-separated rel path, (mtime_ns, size) or None) for every *.md under ``root``.

    Skips dot-entries (so .obsidian/, .git/, .daedalus/ never appear) and
    symlinks (so nothing outside the vault leaks in). Directories are opened
    by fd and files stat'ed relative to them, which avoids re-resolving the
    full path for every file.
    """
    try:
        fd = os.open(root, _DIR_FLAGS)
    except OSError:
        return
    try:
        yield from _walk(fd, "", stat)
    finally:
        os.close(fd)


def _walk(fd: int, prefix: str, stat: bool) -> Iterator[tuple[str, tuple[int, int] | None]]:
    subdirs = []
    with os.scandir(fd) as it:
        for entry in it:
            name = entry.name
            if name.startswith(".") or entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                subdirs.append(name)
            elif name[-3:].lower() == ".md" and entry.is_file(follow_symlinks=False):
                if not stat:
                    yield prefix + name, None
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                yield prefix + name, (st.st_mtime_ns, st.st_size)
    for name in subdirs:
        try:
            sub = os.open(name, _DIR_FLAGS, dir_fd=fd)
        except OSError:
            continue
        try:
            yield from _walk(sub, f"{prefix}{name}/", stat)
        finally:
            os.close(sub)


def index_path(vault_root: str | os.PathLike[str]) -> Path:
    real = os.path.realpath(vault_root)
    return CACHE_ROOT / "index" / (hashlib.sha1(real.encode("utf-8")).hexdigest() + ".sqlite")


def to_fts_query(query: str) -> str:
    """Turn free text into a safe FTS5 query.

    Each whitespace-separated chunk becomes a phrase of its word tokens
    ("one-commit" -> "one commit"), a double-quoted span becomes one phrase,
    and all parts must match. Only word characters reach FTS5, so no user
    input is parsed as FTS5 syntax.
    """
    parts = []
    for m in _QUERY_PART.finditer(query):
        words = _TOKEN.findall(m.group(1) if m.group(1) is not None else m.group(2))
        if words:
            parts.append('"' + " ".join(words) + '"')
    return " ".join(parts)


class Hit(NamedTuple):
    rel_path: str
    snippet: str
    rank: float  # bm25: lower is better


class SearchIndex:
    def __init__(self, vault_root: str | os.PathLike[str]) -> None:
        require_fts5()
        self.root = os.path.realpath(vault_root)
        self.db_path = index_path(self.root)
        if self.db_path.resolve().is_relative_to(self.root):
            raise ValueError(f"index {self.db_path} would be inside the vault {self.root}")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(self.db_path)
        self.con.execute("PRAGMA synchronous = NORMAL")
        self._ensure_schema()
        # In-memory copy of the files table, so a query that finds nothing
        # changed doesn't re-read it. Reloaded whenever another connection has
        # written to the database (PRAGMA data_version changes).
        self._known: dict[str, tuple[int, int, int]] | None = None
        self._data_version: int | None = None

    def _known_files(self) -> dict[str, tuple[int, int, int]]:
        (version,) = self.con.execute("PRAGMA data_version").fetchone()
        if self._known is None or version != self._data_version:
            self._known = {
                rel: (m, s, doc) for rel, m, s, doc in self.con.execute("SELECT rel, mtime_ns, size, doc FROM files")
            }
            self._data_version = version
        return self._known

    def _ensure_schema(self) -> None:
        (version,) = self.con.execute("PRAGMA user_version").fetchone()
        if version == SCHEMA_VERSION:
            return
        self.con.executescript(
            f"""
            DROP TABLE IF EXISTS files;
            DROP TABLE IF EXISTS docs;
            CREATE TABLE files (
                rel TEXT PRIMARY KEY,
                mtime_ns INTEGER NOT NULL,
                size INTEGER NOT NULL,
                doc INTEGER NOT NULL
            );
            CREATE VIRTUAL TABLE docs USING fts5(
                rel UNINDEXED, title, body, tokenize = 'porter unicode61 remove_diacritics 2'
            );
            PRAGMA user_version = {SCHEMA_VERSION};
            """
        )

    def refresh(self) -> tuple[int, int]:
        """Bring the index up to date with the vault; returns (reindexed, dropped)."""
        disk = dict(walk_markdown(self.root, stat=True))
        known = self._known_files()
        changed = [rel for rel, sig in disk.items() if (k := known.get(rel)) is None or (k[0], k[1]) != sig]
        dropped = [rel for rel in known if rel not in disk]
        if not changed and not dropped:
            return 0, 0
        self._known = None  # rebuilt from the database after this write
        with self.con:
            for rel in dropped:
                self.con.execute("DELETE FROM docs WHERE rowid = ?", (known[rel][2],))
                self.con.execute("DELETE FROM files WHERE rel = ?", (rel,))
            for rel in changed:
                if rel in known:
                    self.con.execute("DELETE FROM docs WHERE rowid = ?", (known[rel][2],))
                try:
                    # The stored signature is the one taken *before* reading, so a
                    # write that lands mid-read is picked up on the next refresh.
                    body = Path(self.root, rel).read_text(encoding="utf-8", errors="replace")
                except FileNotFoundError:
                    self.con.execute("DELETE FROM files WHERE rel = ?", (rel,))
                    continue
                title = rel.rsplit("/", 1)[-1][:-3]
                cur = self.con.execute("INSERT INTO docs (rel, title, body) VALUES (?, ?, ?)", (rel, title, body))
                mtime_ns, size = disk[rel]  # type: ignore[misc]
                self.con.execute(
                    "INSERT OR REPLACE INTO files (rel, mtime_ns, size, doc) VALUES (?, ?, ?, ?)",
                    (rel, mtime_ns, size, cur.lastrowid),
                )
        return len(changed), len(dropped)

    def search(self, query: str, limit: int = 20) -> list[Hit]:
        self.refresh()
        fts_query = to_fts_query(query)
        if not fts_query or limit <= 0:
            return []
        rows = self.con.execute(
            """
            SELECT rel, snippet(docs, -1, '[', ']', '…', 12), bm25(docs, 0.0, 5.0, 1.0)
            FROM docs WHERE docs MATCH ? ORDER BY bm25(docs, 0.0, 5.0, 1.0) LIMIT ?
            """,
            (fts_query, limit),
        )
        return [Hit(rel, " ".join(snippet.split()), rank) for rel, snippet, rank in rows]

    def close(self) -> None:
        self.con.close()


_open: dict[tuple[str, str], SearchIndex] = {}


def open_index(vault_root: str | os.PathLike[str]) -> SearchIndex:
    """A cached SearchIndex for ``vault_root``, reopened if its file was deleted."""
    real = os.path.realpath(vault_root)
    key = (real, str(CACHE_ROOT))
    idx = _open.get(key)
    if idx is None or not idx.db_path.exists():
        if idx is not None:
            idx.close()
        idx = _open[key] = SearchIndex(real)
    return idx
