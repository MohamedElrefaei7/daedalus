"""/add: bring a local document into the active project as a markdown note in sources/.

ingest(session, src_path), in order:
  1. Validate the path: it resolves (realpath) to a regular file (not a
     directory, device, or FIFO), outside every registered vault, any .git,
     and daedalus's state folder, at most 25 MB, with an allowed extension.
     Only local paths: never a URL.
  2. Hash the original's bytes (SHA-256). If a note in the active vault's
     sources/ already records that hash, refuse, naming it.
  3. Convert into memory. .pdf, .docx, .pptx, .xlsx go through MarkItDown
     (local converters only: no plugins, no LLM client, no Document
     Intelligence endpoint); .md and .txt are copied as-is after a UTF-8 check.
     The file is hashed again afterwards; if it changed meanwhile, refuse.
     PDF text loses pdfminer's "(cid:NNN)" markers for glyphs it couldn't
     map to characters (icon fonts: a phone or envelope symbol).
  4. Refuse empty results (fewer than 50 non-whitespace characters): a
     scanned PDF has no text layer, and an empty note would look like success.
  5. Refuse markdown over 2 MB; never truncate.
  6. Frontmatter: exactly schema.SOURCE_KEYS.
  7. Only the markdown goes into git, never the original: the hash
     identifies it without storing it.
  8. The file is named after the title, slugged, with -2, -3, ... until the
     stem matches no note stem in any registered vault, so a bare link to it
     can't be ambiguous.
  9. One path, one commit (`add: <original_name>`), as USER_ACTION into
     sources/, through apply.apply_changes: the write lock, a stale check,
     atomic write, path-limited commit, rollback, and crash reconciliation.

MarkItDown performs I/O with the privileges of this process, and its README
says untrusted input shouldn't be passed to it directly; step 1 runs before
it ever sees a path.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.metadata
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from . import index, notes, queue
from .accept import slugify
from .apply import Change, apply_changes, repo_for
from .errors import ProposalError
from .policy import Op, authorize
from .schema import SOURCE_KEYS
from .session import Session

MAX_INPUT_BYTES = 25 * 1024 * 1024
MAX_MARKDOWN_BYTES = 2 * 1024 * 1024
MIN_TEXT_CHARS = 50
CONVERTED = (".pdf", ".docx", ".pptx", ".xlsx")
COPIED = (".md", ".txt")
ALLOWED = CONVERTED + COPIED
NO_TEXT = "no text layer — likely a scanned document; OCR isn't enabled"
_CID = re.compile(r"\(cid:\d+\)[ \t]*")  # pdfminer's marker for a glyph with no character mapping
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.M)


class IngestError(ProposalError):
    """/add refused the file; nothing was written."""


class Converter(Protocol):
    def convert_local(self, path: str) -> Any: ...  # returns an object with .markdown


_converter: Any = None


def converter() -> Any:
    """The one MarkItDown instance: built-in local converters only, created on first use."""
    global _converter
    if _converter is None:
        from markitdown import MarkItDown

        _converter = MarkItDown(enable_plugins=False)  # and never llm_client, docintel_endpoint, or a URL
    return _converter


def converter_version() -> str:
    return f"markitdown {importlib.metadata.version('markitdown')}"


@dataclass(frozen=True)
class SourceNote:
    vault: str
    rel_path: str
    title: str
    chars: int  # of the converted markdown body
    original_name: str
    original_sha256: str
    commit_sha: str

    @property
    def ref(self) -> str:
        return f"{self.vault}:{self.rel_path}"


# 1. The path -----------------------------------------------------------------------------

def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def validate_path(session: Session, src_path: str | os.PathLike[str]) -> tuple[Path, os.stat_result]:
    raw = os.fspath(src_path)
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
        raise IngestError(f"{raw}: /add takes a local file path, never a URL")
    real = Path(os.path.realpath(os.path.expanduser(raw)))
    try:
        st = os.stat(real)
    except FileNotFoundError:
        raise IngestError(f"{raw}: no such file") from None
    except OSError as e:
        raise IngestError(f"{raw}: can't be read ({e.strerror})") from None
    if not stat.S_ISREG(st.st_mode):
        kind = ("a directory" if stat.S_ISDIR(st.st_mode) else "a FIFO" if stat.S_ISFIFO(st.st_mode)
                else "a device" if stat.S_ISCHR(st.st_mode) or stat.S_ISBLK(st.st_mode) else "not a regular file")
        raise IngestError(f"{raw}: is {kind}; /add takes a regular file")
    reg = session.registry
    for name, root in [("the shared vault", reg.shared), *((f"the {n} vault", r) for n, r in reg.projects.items())]:
        if _inside(real, root):
            raise IngestError(f"{raw} resolves to {real}, inside {name}; /add brings files in from outside the vaults")
    if any(part.casefold() == ".git" for part in real.parts):
        raise IngestError(f"{raw} resolves into a .git folder ({real})")
    state = Path(os.path.realpath(queue.STATE_ROOT))
    if _inside(real, state):
        raise IngestError(f"{raw} resolves into daedalus's state folder ({state})")
    ext = real.suffix.lower()
    if ext not in ALLOWED:
        raise IngestError(f"{real.name}: {ext or 'no extension'} isn't supported; /add takes "
                          f"{', '.join(ALLOWED)}")
    if st.st_size > MAX_INPUT_BYTES:
        raise IngestError(f"{real.name} is {st.st_size / 1024 / 1024:.1f} MB; the cap is "
                          f"{MAX_INPUT_BYTES // 1024 // 1024} MB")
    return real, st


def _hash_file(path: Path) -> tuple[str, int]:
    h, n = hashlib.sha256(), 0
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise IngestError(f"{path} stopped being a regular file")
        while chunk := os.read(fd, 1 << 20):
            h.update(chunk)
            n += len(chunk)
            if n > MAX_INPUT_BYTES:
                raise IngestError(f"{path.name} grew past {MAX_INPUT_BYTES // 1024 // 1024} MB while being read")
    finally:
        os.close(fd)
    return h.hexdigest(), n


# 2. Duplicates ---------------------------------------------------------------------------

def existing_source(session: Session, sha256: str) -> str | None:
    """The active vault's sources/ note recording ``sha256``, as vault:path, or None."""
    root = session.active_vault
    for rel, _ in sorted(index.walk_markdown(root), key=lambda item: item[0]):
        if not rel.startswith("sources/"):
            continue
        try:
            note, _errors = notes.read_note(root / rel, session, strict=False)
        except (UnicodeDecodeError, OSError):
            continue
        if str(note.frontmatter.get("original_sha256", "")) == sha256:
            return f"{session.active}:{rel}"
    return None


# 3-5. Conversion ---------------------------------------------------------------------------

def convert(path: Path, conv: Converter | None) -> tuple[str, str]:
    """(markdown, converter label), in memory."""
    if path.suffix.lower() in COPIED:
        try:
            return path.read_bytes().decode("utf-8"), "copy"
        except UnicodeDecodeError as e:
            raise IngestError(f"{path.name} isn't UTF-8 text (byte {e.start}); save it as UTF-8 first") from None
    conv = conv or converter()
    try:
        result = conv.convert_local(str(path))
    except Exception as e:  # MarkItDown raises its own FileConversionException and friends; pass the reason on
        detail = " ".join(str(e).split())[:400]
        raise IngestError(f"{path.name} couldn't be converted ({type(e).__name__}: {detail})") from None
    markdown = getattr(result, "markdown", None)
    if not isinstance(markdown, str):
        raise IngestError(f"{path.name}: the converter returned no markdown")
    if path.suffix.lower() == ".pdf":
        markdown = strip_glyph_artifacts(markdown)
    return markdown, converter_version()


def strip_glyph_artifacts(text: str) -> str:
    """Drop "(cid:NNN)" markers, and the spaces after them, from PDF text."""
    return _CID.sub("", text)


def check_text(markdown: str, name: str, copied: bool) -> None:
    if len("".join(markdown.split())) < MIN_TEXT_CHARS:
        if copied:
            raise IngestError(f"{name} has fewer than {MIN_TEXT_CHARS} non-whitespace characters; nothing to add")
        raise IngestError(f"{name}: {NO_TEXT}")
    size = len(markdown.encode("utf-8"))
    if size > MAX_MARKDOWN_BYTES:
        raise IngestError(f"{name} converts to {size / 1024 / 1024:.1f} MB of markdown; the cap is "
                          f"{MAX_MARKDOWN_BYTES // 1024 // 1024} MB. Split the document and add the parts.")


# 6-8. The note ------------------------------------------------------------------------------

def title_of(markdown: str, path: Path) -> str:
    m = _HEADING.search(markdown)
    title = " ".join(m.group(1).split()) if m else ""
    title = re.sub(r"[*_`]+", "", title).strip()
    return title[:200] or path.stem


def source_note(title: str, name: str, sha256: str, size: int, conv: str, markdown: str) -> str:
    values = {"title": title, "original_name": name, "original_sha256": sha256, "original_bytes": size,
              "converter": conv, "added": dt.date.today().isoformat(), "source": "human"}
    assert tuple(values) == SOURCE_KEYS
    lines = [f"{k}: {v}" if isinstance(v, int) or k == "source" else f"{k}: {json.dumps(v, ensure_ascii=False)}"
             for k, v in values.items()]
    body = markdown if markdown.endswith("\n") else markdown + "\n"
    return "---\n" + "\n".join(lines) + "\n---\n\n" + body


def taken_stems(session: Session) -> set[str]:
    """Every note stem in every registered vault, case-folded (templates/ aside, as for links)."""
    reg = session.registry
    stems = set()
    for root in [reg.shared, *reg.projects.values()]:
        for rel, _ in index.walk_markdown(root):
            stems.add(rel.rsplit("/", 1)[-1][:-3].casefold())
    return stems


def free_stem(session: Session, title: str) -> str:
    base = slugify(title) if re.search(r"[a-z0-9]", title.lower()) else "source"
    taken = taken_stems(session)
    stem, n = base, 2
    while stem.casefold() in taken or os.path.lexists(session.active_vault / "sources" / f"{stem}.md"):
        stem, n = f"{base}-{n}", n + 1
    return stem


# 9. The commit -------------------------------------------------------------------------------

def ingest(session: Session, src_path: str | os.PathLike[str], conv: Converter | None = None) -> SourceNote:
    """Convert a local document into sources/<slug>.md in the active vault and commit it."""
    path, _ = validate_path(session, src_path)  # 1, before any converter sees the path
    sha256, size = _hash_file(path)  # 2
    dup = existing_source(session, sha256)
    if dup is not None:
        raise IngestError(f"{path.name} is already in the vault as {dup} (same SHA-256), so it isn't added again")
    markdown, label = convert(path, conv)  # 3
    if _hash_file(path) != (sha256, size):
        raise IngestError(f"{path.name} changed while it was being converted; nothing was added")
    check_text(markdown, path.name, path.suffix.lower() in COPIED)  # 4, 5
    title = title_of(markdown, path)  # 6
    stem = free_stem(session, title)  # 8
    rel = f"sources/{stem}.md"
    target = authorize(session.active_vault / rel, Op.USER_ACTION, session)
    content = source_note(title, path.name, sha256, size, label, markdown)
    repo = repo_for(session.registry, session.active)
    sha = apply_changes(repo, [Change(target.path, content, None)], summary=f"add: {path.name}"[:200])  # 7, 9
    return SourceNote(session.active, rel, title, len(markdown), path.name, sha256, sha)
