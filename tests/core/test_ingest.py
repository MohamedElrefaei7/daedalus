"""/add: local documents into sources/ as markdown. Real tiny fixtures, built per test."""

from __future__ import annotations

import contextlib
import hashlib
import os
import socket
from pathlib import Path

import doc_fixtures as docs
import pytest
import yaml
from conftest import git, head, write_note
from test_apply import commits_since

from daedalus.core import ingest as ingest_mod
from daedalus.core import notes, queue, tools
from daedalus.core.ingest import NO_TEXT, IngestError, ingest
from daedalus.core.schema import SOURCE_KEYS

BUILDERS = {
    "pdf": (docs.make_pdf, docs.PDF_PHRASE),
    "docx": (docs.make_docx, docs.DOCX_PHRASE),
    "pptx": (docs.make_pptx, docs.PPTX_PHRASE),
    "xlsx": (docs.make_xlsx, docs.XLSX_PHRASE),
}


class FakeConverter:
    """Records every path it's asked to convert."""

    def __init__(self, markdown: str = "# Fake\n\n" + "words " * 20) -> None:
        self.calls: list[str] = []
        self.markdown = markdown

    def convert_local(self, path):
        self.calls.append(path)
        return type("Result", (), {"markdown": self.markdown})()


@pytest.fixture
def docdir(tmp_path) -> Path:
    d = tmp_path / "docs"
    d.mkdir()
    return d


def frontmatter(path: Path) -> dict:
    raw, _ = notes.split_frontmatter(path.read_text())
    return yaml.safe_load(raw)


def frontmatter_keys(path: Path) -> tuple[str, ...]:
    raw, _ = notes.split_frontmatter(path.read_text())
    return tuple(line.split(":", 1)[0] for line in raw.splitlines() if line and not line.startswith(" "))


def unchanged(w, before) -> None:
    assert w.snapshot() == before, "a refused /add changed a vault, a HEAD, or the queue"
    assert queue.changesets() == [], "a refused /add left a change-set record"
    assert not any((w.alpha / "sources").glob("*.md")), "a refused /add left a file in sources/"


# 1. Each format converts -----------------------------------------------------------------------

@pytest.mark.parametrize("kind", sorted(BUILDERS))
def test_each_format_converts_to_one_committed_source_note(gworld, docdir, kind) -> None:
    build, phrase = BUILDERS[kind]
    src = build(docdir / f"fixture.{kind}")
    repo, base = gworld.alpha_repo, head(gworld.alpha_repo)

    done = ingest(gworld.session, src)

    note = gworld.alpha / done.rel_path
    assert done.rel_path.startswith("sources/") and note.is_file()
    assert phrase in note.read_text()
    assert frontmatter_keys(note) == SOURCE_KEYS
    fm = frontmatter(note)
    assert fm["original_name"] == f"fixture.{kind}" and fm["source"] == "human"
    assert fm["original_sha256"] == hashlib.sha256(src.read_bytes()).hexdigest() == done.original_sha256
    assert fm["original_bytes"] == src.stat().st_size
    assert fm["converter"] == f"markitdown {ingest_mod.importlib.metadata.version('markitdown')}"
    assert isinstance(fm["added"], str) and len(fm["added"]) == 10
    assert commits_since(repo, base) == 1
    assert git(repo, "log", "-1", "--format=%s").strip() == f"add: fixture.{kind}"
    assert git(repo, "show", "--name-only", "--format=", "HEAD").split() == [f"vault/{done.rel_path}"]
    assert git(repo, "status", "--porcelain") == ""
    assert done.chars > 50


def test_two_page_pdf_keeps_both_pages(gworld, docdir) -> None:
    done = ingest(gworld.session, docs.make_pdf(docdir / "report.pdf"))
    text = (gworld.alpha / done.rel_path).read_text()
    assert "page 1" in text and "page 2" in text


def test_title_from_first_heading_else_filename(gworld, docdir) -> None:
    d = ingest(gworld.session, docs.make_docx(docdir / "x.docx", heading="Fetcher design notes"))
    assert d.title == "Fetcher design notes" and d.rel_path == "sources/fetcher-design-notes.md"
    p = ingest(gworld.session, docs.make_pdf(docdir / "Quarterly Report.pdf"))  # pdfminer gives no headings
    assert p.title == "Quarterly Report" and p.rel_path == "sources/quarterly-report.md"


@pytest.mark.parametrize("ext", [".md", ".txt"])
def test_md_and_txt_are_copied_as_is(gworld, docdir, ext) -> None:
    body = "# Field notes\n\n" + docs.FILLER + "\n"
    src = docdir / f"notes{ext}"
    src.write_text(body, encoding="utf-8")
    conv = FakeConverter()
    done = ingest(gworld.session, src, conv)
    assert conv.calls == []  # no converter for text
    text = (gworld.alpha / done.rel_path).read_text()
    assert text.endswith(body) and frontmatter(gworld.alpha / done.rel_path)["converter"] == "copy"


def test_non_utf8_text_is_refused(gworld, docdir) -> None:
    src = docdir / "latin.txt"
    src.write_bytes(("caf\xe9 " * 40).encode("latin-1"))
    before = gworld.snapshot()
    with pytest.raises(IngestError, match="isn't UTF-8"):
        ingest(gworld.session, src)
    unchanged(gworld, before)


# 2. Scanned PDF ------------------------------------------------------------------------------

def test_scanned_pdf_is_refused_with_no_text_layer(gworld, docdir) -> None:
    src = docs.make_scanned_pdf(docdir / "scan.pdf")
    before, base = gworld.snapshot(), head(gworld.alpha_repo)
    with pytest.raises(IngestError) as e:
        ingest(gworld.session, src)
    assert NO_TEXT in str(e.value) and str(e.value) == f"scan.pdf: {NO_TEXT}"
    assert head(gworld.alpha_repo) == base
    unchanged(gworld, before)


# 3. Unsupported, oversized, corrupt -------------------------------------------------------------

@pytest.mark.parametrize("case", ["unsupported", "oversized", "corrupt"])
def test_bad_files_are_refused_with_nothing_written(gworld, docdir, case) -> None:
    if case == "unsupported":
        src, match = docdir / "tool.exe", r"\.exe isn't supported"
        src.write_bytes(b"MZ" + os.urandom(500))
    elif case == "oversized":
        src, match = docs.make_big(docdir / "huge.pdf", 26 * 1024 * 1024), "26.0 MB; the cap is 25 MB"
    else:
        src, match = docs.make_corrupt_pdf(docdir / "broken.pdf"), "couldn't be converted"
    before = gworld.snapshot()
    with pytest.raises(IngestError, match=match):
        ingest(gworld.session, src)
    unchanged(gworld, before)


def test_markdown_over_2mb_is_refused_not_truncated(gworld, docdir) -> None:
    src = docs.make_pdf(docdir / "a.pdf")
    before = gworld.snapshot()
    with pytest.raises(IngestError, match="the cap is 2 MB"):
        ingest(gworld.session, src, FakeConverter("x" * (2 * 1024 * 1024 + 1)))
    unchanged(gworld, before)


def test_a_file_changed_during_conversion_is_refused(gworld, docdir) -> None:
    src = docs.make_pdf(docdir / "a.pdf")

    class Mutating(FakeConverter):
        def convert_local(self, path):
            Path(path).write_bytes(Path(path).read_bytes() + b"%changed")
            return super().convert_local(path)

    before = gworld.snapshot()
    with pytest.raises(IngestError, match="changed while it was being converted"):
        ingest(gworld.session, src, Mutating())
    unchanged(gworld, before)


# 4. Paths are validated before the converter sees them ------------------------------------------

def test_bad_paths_never_reach_the_converter(gworld, docdir, state_root) -> None:
    conv = FakeConverter()
    folder = docdir / "a-folder.pdf"
    folder.mkdir()
    link = docdir / "sneaky.pdf"
    target = docs.make_pdf(gworld.alpha / "inbox" / "inside.pdf")
    link.symlink_to(target)
    fifo = docdir / "pipe.pdf"
    os.mkfifo(fifo)
    git_file = docs.make_pdf(gworld.alpha_repo / ".git" / "x.pdf")
    state_file = docs.make_pdf(state_root / "x.pdf")
    cases = [
        (folder, "is a directory"),
        (link, "inside the alpha vault"),
        (fifo, "is a FIFO"),
        (git_file, "inside the alpha vault|.git"),
        (state_file, "state folder"),
        ("https://example.com/report.pdf", "never a URL"),
        (docdir / "missing.pdf", "no such file"),
    ]
    for path, match in cases:
        with pytest.raises(IngestError, match=match):
            ingest(gworld.session, path, conv)
    assert conv.calls == [], "a refused path reached the converter"


def test_shared_vault_and_other_projects_are_inside_vaults_too(gworld) -> None:
    conv = FakeConverter()
    for root in (gworld.shared, gworld.gamma):
        src = docs.make_pdf(root / "inbox" / "x.pdf")
        with pytest.raises(IngestError, match="inside the"):
            ingest(gworld.session, src, conv)
    assert conv.calls == []


# 5. Duplicates --------------------------------------------------------------------------------

def test_the_same_file_is_refused_the_second_time_even_renamed(gworld, docdir) -> None:
    src = docs.make_docx(docdir / "design.docx")
    first = ingest(gworld.session, src)
    renamed = docdir / "design (final).docx"
    renamed.write_bytes(src.read_bytes())
    for path in (src, renamed):
        before = gworld.snapshot()
        with pytest.raises(IngestError) as e:
            ingest(gworld.session, path)
        assert first.ref in str(e.value) and "already in the vault" in str(e.value)
        unchanged_but_first(gworld, before)


def unchanged_but_first(w, before) -> None:
    assert w.snapshot() == before
    assert len([cs for cs in queue.changesets() if cs.status == "applied"]) == 1


# 6. Names don't collide ---------------------------------------------------------------------------

def test_a_title_like_an_existing_note_gets_its_own_stem(gworld, docdir) -> None:
    done = ingest(gworld.session, docs.make_docx(docdir / "a.docx", heading="Storage engine"))
    assert done.rel_path == "sources/storage-engine-2.md"  # decisions/storage-engine.md has the stem
    m = tools.resolve_ref(gworld.session, "storage-engine-2")
    assert (m.vault, m.rel_path) == ("alpha", "sources/storage-engine-2.md")
    shared = ingest(gworld.session, docs.make_docx(docdir / "b.docx", heading="Red test first"))
    assert shared.rel_path == "sources/red-test-first-2.md"  # a stem in the shared vault counts too
    tools.resolve_ref(gworld.session, "red-test-first-2")
    gamma = ingest(gworld.session, docs.make_docx(docdir / "c.docx", heading="Secret"))
    assert gamma.rel_path == "sources/secret-2.md"  # and in any registered vault, pulled or not


def test_second_document_with_the_same_title_gets_a_suffix(gworld, docdir) -> None:
    a, b = docdir / "a.md", docdir / "b.md"
    a.write_text("# Weekly sync\n\n" + docs.FILLER + "\n")
    b.write_text("# Weekly sync\n\nA different week. " + docs.FILLER + "\n")
    assert ingest(gworld.session, a).rel_path == "sources/weekly-sync.md"
    assert ingest(gworld.session, b).rel_path == "sources/weekly-sync-2.md"


# 7. The original never enters git ---------------------------------------------------------------

def test_the_original_never_enters_git(gworld, docdir) -> None:
    for kind, (build, _) in BUILDERS.items():
        ingest(gworld.session, build(docdir / f"orig.{kind}"))
    history = git(gworld.alpha_repo, "log", "--all", "--name-only", "--format=").split()
    assert history, "expected the add commits in history"
    binaries = [p for p in history if Path(p).suffix.lower() in (".pdf", ".docx", ".pptx", ".xlsx")]
    assert binaries == [], f"originals entered git: {binaries}"
    assert not [p for p in (gworld.alpha / "sources").iterdir() if p.suffix != ".md" and not p.name.startswith(".")]


# The queue's change-set guarantees apply ----------------------------------------------------------

def test_a_failing_hook_leaves_no_source_note(gworld, docdir) -> None:
    from daedalus.core.errors import CommitFailed
    from test_apply import install_hook

    install_hook(gworld.alpha_repo)
    base = head(gworld.alpha_repo)
    with pytest.raises(CommitFailed):
        ingest(gworld.session, docs.make_pdf(docdir / "a.pdf"))
    assert head(gworld.alpha_repo) == base and not any((gworld.alpha / "sources").glob("*.md"))
    assert git(gworld.alpha_repo, "status", "--porcelain") == ""


def test_source_notes_are_readable_and_searchable(gworld, docdir) -> None:
    done = ingest(gworld.session, docs.make_pdf(docdir / "readings.pdf"))
    note, errors = tools.read_note(gworld.session, done.ref, strict=False)
    assert errors == [] and docs.PDF_PHRASE in note.body
    assert [h.rel_path for h in tools.search(gworld.session, "seismic readings")] == [done.rel_path]


def test_existing_typed_note_in_sources_is_ignored_by_the_hash_check(gworld, docdir) -> None:
    write_note(gworld.alpha, "sources/hand-written.md", None, "# a note I wrote\n")
    done = ingest(gworld.session, docs.make_pdf(docdir / "a.pdf"))
    assert done.rel_path.startswith("sources/")


# PDF glyph artifacts --------------------------------------------------------------------------

GLYPHY = ("(cid:131) 555-0100 # jane@example.com (cid:239) example.com/in/jane § github.com/jane\n\n"
          "Experience(cid:12)(cid:3)\tPlanned the storage layer and the fetcher for the station network.\n")


def test_pdf_glyph_artifacts_are_stripped(gworld, docdir) -> None:
    """pdfminer writes (cid:NNN) for icon-font glyphs it can't map; none reach the note."""
    src = docs.make_pdf(docdir / "glyphs.pdf")
    done = ingest(gworld.session, src, conv=FakeConverter(GLYPHY))
    body = notes.split_frontmatter((gworld.alpha / done.rel_path).read_text())[1]
    assert "(cid:" not in body
    assert "555-0100 # jane@example.com example.com/in/jane § github.com/jane\n" in body
    assert "ExperiencePlanned the storage layer" in body
    assert done.chars == len(ingest_mod.strip_glyph_artifacts(GLYPHY))


def test_glyph_markers_are_kept_outside_pdfs(gworld, docdir) -> None:
    """Only PDF text has pdfminer's markers; a markdown file that mentions one is copied as-is."""
    src = docdir / "about-cids.md"
    src.write_text("# pdfminer markers\n\nA glyph with no mapping shows up as (cid:131) in extracted text.\n")
    done = ingest(gworld.session, src)
    assert "as (cid:131) in extracted" in (gworld.alpha / done.rel_path).read_text()


# No network -----------------------------------------------------------------------------------

class NetworkBlocked(AssertionError):
    """A socket connect or DNS lookup happened during ingest."""


@contextlib.contextmanager
def no_network(monkeypatch):
    """Every socket connect and DNS lookup inside the block raises, and is recorded.

    Recorded as well as raised, so a converter that catches the error and
    carries on still fails the test.
    """
    attempts: list[str] = []

    def refuse(name):
        def blocked(*args, **kwargs):
            attempts.append(f"{name}{args!r}")
            raise NetworkBlocked(f"{name} called during ingest with {args!r}")
        return blocked

    with monkeypatch.context() as m:
        m.setattr(socket.socket, "connect", refuse("socket.connect"))
        m.setattr(socket.socket, "connect_ex", refuse("socket.connect_ex"))
        m.setattr(socket, "create_connection", refuse("socket.create_connection"))
        m.setattr(socket, "getaddrinfo", refuse("socket.getaddrinfo"))
        yield attempts


def test_conversion_never_touches_the_network(gworld, docdir, monkeypatch) -> None:
    """Every format converts, with the real MarkItDown built from scratch, while the network is blocked."""
    monkeypatch.setattr(ingest_mod, "_converter", None)  # so construction happens under the block too
    sources = {kind: build(docdir / f"net.{kind}") for kind, (build, _) in BUILDERS.items()}
    (docdir / "net.md").write_text("# Notes\n\n" + docs.FILLER + "\n")
    (docdir / "net.txt").write_text(docs.FILLER + "\n")
    sources |= {"md": docdir / "net.md", "txt": docdir / "net.txt"}

    with no_network(monkeypatch) as attempts:
        added = {kind: ingest(gworld.session, src) for kind, src in sorted(sources.items())}

    assert attempts == [], f"ingest reached for the network: {attempts}"
    assert ingest_mod._converter is not None, "the real converter wasn't used"
    for kind, (_, phrase) in BUILDERS.items():
        assert phrase in (gworld.alpha / added[kind].rel_path).read_text(), kind
    assert sorted(added) == ["docx", "md", "pdf", "pptx", "txt", "xlsx"]
