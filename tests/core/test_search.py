"""Part F: the FTS5 search index."""

from __future__ import annotations

import os
import sqlite3

import pytest
from conftest import tree_hash, write_note, write_raw

from daedalus.core import index
from daedalus.core.index import SearchIndex, index_path, require_fts5, to_fts_query
from daedalus.core.tools import search


def rels(hits) -> set[tuple[str, str]]:
    return {(h.vault, h.rel_path) for h in hits}


def test_fts5_available() -> None:
    require_fts5()
    con = sqlite3.connect(":memory:")
    con.execute("CREATE VIRTUAL TABLE t USING fts5(x)")


def test_missing_fts5_fails_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    class NoFts5:
        def execute(self, sql):
            raise sqlite3.OperationalError("no such module: fts5")

        def close(self):
            pass

    monkeypatch.setattr(index, "_fts5_checked", False)
    monkeypatch.setattr(index.sqlite3, "connect", lambda *_: NoFts5())
    with pytest.raises(index.SearchUnavailable, match="needs SQLite with the FTS5 extension"):
        require_fts5()


def test_finds_a_phrase(world) -> None:
    write_note(world.alpha, "concepts/rupture.md", "concept", "Fault rupture propagates along the strike.\n")
    write_note(world.alpha, "concepts/other.md", "concept", "Strike first, then fault the rupture.\n")
    assert rels(search(world.session, '"fault rupture propagates"')) == {("alpha", "concepts/rupture.md")}
    # Unquoted words must all match, in any order.
    assert rels(search(world.session, "rupture strike")) == {("alpha", "concepts/rupture.md"), ("alpha", "concepts/other.md")}


def test_snippet_highlights_match(world) -> None:
    write_note(world.alpha, "concepts/rupture.md", "concept", "Fault rupture propagates along the strike.\n")
    (hit,) = search(world.session, "propagates")
    assert "[propagates]" in hit.snippet


def test_stemming(world) -> None:
    write_note(world.alpha, "concepts/t.md", "concept", "A test that fails when the invariant breaks.\n")
    assert ("alpha", "concepts/t.md") in rels(search(world.session, "failing test"))


def test_searches_pulled_and_shared_not_unpulled(world) -> None:
    write_raw(world.beta, "inbox/b.md", "zircon\n")
    write_raw(world.shared, "inbox/s.md", "zircon\n")
    write_raw(world.gamma, "inbox/g.md", "zircon\n")
    assert rels(search(world.session, "zircon")) == {("beta", "inbox/b.md"), ("shared", "inbox/s.md")}


def test_picks_up_modified_file(world) -> None:
    path = write_raw(world.alpha, "inbox/draft.md", "first version: basalt\n")
    assert rels(search(world.session, "basalt")) == {("alpha", "inbox/draft.md")}
    path.write_text("second version: obsidian-free granite, now longer\n")
    assert search(world.session, "basalt") == []
    assert rels(search(world.session, "granite")) == {("alpha", "inbox/draft.md")}


def test_picks_up_new_file(world) -> None:
    assert search(world.session, "gneiss") == []
    write_raw(world.alpha, "inbox/new.md", "gneiss\n")
    assert rels(search(world.session, "gneiss")) == {("alpha", "inbox/new.md")}


def test_drops_deleted_file(world) -> None:
    path = write_raw(world.alpha, "inbox/doomed.md", "schist\n")
    assert search(world.session, "schist")
    path.unlink()
    assert search(world.session, "schist") == []


def test_skips_dot_folders_symlinks_and_non_markdown(world) -> None:
    write_raw(world.alpha, ".obsidian/x.md", "feldspar\n")
    write_raw(world.alpha, ".daedalus/x.md", "feldspar\n")
    write_raw(world.alpha, "sources/x.txt", "feldspar\n")
    outside = write_raw(world.root, "outside/secret.md", "feldspar\n")
    (world.alpha / "inbox" / "leak.md").symlink_to(outside)
    assert search(world.session, "feldspar") == []


def test_never_writes_inside_the_vault(world) -> None:
    roots = [world.alpha, world.beta, world.shared]
    write_note(world.alpha, "concepts/x.md", "concept", "quartz\n")
    before = [tree_hash(r) for r in roots]
    search(world.session, "quartz")  # first query builds the index
    assert [tree_hash(r) for r in roots] == before
    path = write_raw(world.alpha, "inbox/y.md", "quartz again\n")
    path.with_name("y.md").write_text("quartz, edited\n")
    before = [tree_hash(r) for r in roots]
    search(world.session, "quartz")  # this one re-indexes the changed vault
    assert [tree_hash(r) for r in roots] == before


def test_index_lives_in_cache_named_by_vault_hash(world) -> None:
    search(world.session, "anything")
    path = index_path(world.alpha)
    assert path.exists() and path.parent == index.CACHE_ROOT / "index"
    assert path.name == __import__("hashlib").sha1(os.path.realpath(world.alpha).encode()).hexdigest() + ".sqlite"


def test_index_inside_vault_refused(world, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(index, "CACHE_ROOT", world.alpha / ".daedalus")
    with pytest.raises(ValueError, match="inside the vault"):
        SearchIndex(world.alpha)


def test_index_rebuilt_after_cache_deleted(world) -> None:
    write_raw(world.alpha, "inbox/m.md", "mica\n")
    assert search(world.session, "mica")
    index_path(world.alpha).unlink()
    assert rels(search(world.session, "mica")) == {("alpha", "inbox/m.md")}


def test_second_connection_writes_are_seen(world) -> None:
    # Another process indexing the same vault must not make this one insert duplicates.
    write_raw(world.alpha, "inbox/a.md", "olivine\n")
    first = index.open_index(world.alpha)
    assert len(first.search("olivine")) == 1
    assert len(first.search("olivine")) == 1  # nothing changed: served from the in-memory file table
    write_raw(world.alpha, "inbox/b.md", "olivine too\n")
    other = SearchIndex(world.alpha)
    other.refresh()
    other.close()
    assert sorted(h.rel_path for h in first.search("olivine")) == ["inbox/a.md", "inbox/b.md"]


@pytest.mark.parametrize("query",['"', "AND", "NOT x", "a*", "col:x", "(", "one-commit", "^", "--", ""])
def test_query_syntax_never_errors(world, query) -> None:
    search(world.session, query)


def test_to_fts_query() -> None:
    assert to_fts_query('one-commit "red test" fails') == '"one commit" "red test" "fails"'
    assert to_fts_query('" -- *') == ""
