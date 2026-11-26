"""The system message, in six blocks ordered from most stable to least stable.

  1. the role preamble
  2. the shared vault's AGENTS.md
  3. the active project's AGENTS.md
  4. accepted shared practices (practices/**.md with status: accepted), by path,
     at most 12,000 characters in all, with a note naming any left out
  5. each pulled project, by name: its CONTEXT.md "Decisions index" and
     "Where things live" sections only, at most 6,000 characters each
  6. the active project's CONTEXT.md, last, because it changes most often: every
     section whole except Log, which keeps only its 8 newest entries, with a
     one-line note counting the older ones

DeepSeek's context cache matches on identical prefixes, so the order puts what
changes least first, and the assembly is byte-identical for identical files:
fixed order, sorted paths, no timestamps, no set iteration. Every file is read
through the path policy.
"""

from __future__ import annotations

import re

from daedalus.core import index, notes
from daedalus.core.policy import Op, authorize
from daedalus.core.registry import SHARED_NAME
from daedalus.core.session import Session

SEPARATOR = "\n\n---\n\n"
MAX_PRACTICES_CHARS = 12_000
MAX_PULLED_CHARS = 6_000
PULLED_SECTIONS = ("Decisions index", "Where things live")
LOG_ENTRIES = 8
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_DATED = re.compile(r"^(\d{4}-\d{2}-\d{2})")

PREAMBLE = """\
You are the Daedalus terminal agent, working for the user in their project knowledge vaults.

You can read notes in the active project's vault, any pulled project vaults, and the shared vault of \
practices and lessons, and you can read the active project's code. You can't change anything directly: \
propose_note, propose_edit, and propose_lesson queue proposals, and nothing is written until the user approves \
it in this terminal. Proposals are recorded as source deepseek; drafts that claim another source are refused. \
Pulled projects are read-only.

No design choice goes ahead without precedent. Before choosing a schema, a new dependency, an interface \
between components, or a data source, call find_precedent; cite what it returns. Only accepted notes in the active \
project's decisions/ and accepted shared practices count. If it finds none, call ask_user with your options and \
stop: the user decides, and their answer becomes an accepted decision.

Ground answers in the notes: read before you answer, cite notes as vault:path, and say when the vaults \
don't cover something. When a tool refuses, its message names the rule; adjust instead of retrying the same \
call. Keep answers short.

The documents below are the vaults' own rules and context: both AGENTS.md files, the accepted shared \
practices, pulled projects' indexes, then the active project's CONTEXT.md. Follow them."""


def _read(session: Session, path) -> str:
    auth = authorize(path, Op.READ, session)
    return auth.path.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str | None:
    """A '## heading' section's text (heading line included), up to the next heading of level 1 or 2."""
    lines = text.splitlines()
    want = heading.casefold()
    for i, line in enumerate(lines):
        m = re.match(r"^(#{1,2})\s+(.*?)\s*#*\s*$", line)
        if m and len(m.group(1)) == 2 and m.group(2).casefold() == want:
            end = next((j for j in range(i + 1, len(lines)) if re.match(r"^#{1,2}\s", lines[j])), len(lines))
            return "\n".join(lines[i:end]).strip()
    return None


def _cap(text: str, limit: int, what: str) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n\n[truncated: {what} is {len(text):,} characters; the first {limit:,} are shown]"


def recent_log(text: str, vault: str, keep: int = LOG_ENTRIES) -> str:
    """``text`` with its "## Log" section cut to the ``keep`` newest "### " entries, plus a note counting the rest.

    Every other section, and any text in Log before its first entry, stays
    whole. Newest means the latest dated heading ("### 2026-11-26 — ...") when
    every entry is dated, else the last in the file, which is where /end
    appends. Kept entries stay in file order.
    """
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines)
                  if (m := _HEADING.match(line.rstrip("\n"))) and len(m.group(1)) == 2
                  and m.group(2).casefold() == "log"), None)
    if start is None:
        return text
    end = next((j for j in range(start + 1, len(lines))
                if (m := _HEADING.match(lines[j].rstrip("\n"))) and len(m.group(1)) <= 2), len(lines))
    entry_starts = [j for j in range(start + 1, end)
                    if (m := _HEADING.match(lines[j].rstrip("\n"))) and len(m.group(1)) == 3]
    if len(entry_starts) <= keep:
        return text
    bounds = list(zip(entry_starts, entry_starts[1:] + [end]))
    entries = ["".join(lines[a:b]) for a, b in bounds]
    titles = [_HEADING.match(lines[a].rstrip("\n")).group(2) for a, _ in bounds]
    dates = [m.group(1) if (m := _DATED.match(t)) else None for t in titles]
    order = list(range(len(entries)))
    if all(dates):
        order.sort(key=lambda i: (dates[i], i))  # stable: same-day entries keep file order
    newest = sorted(order[-keep:])
    older = len(entries) - keep
    what = "older Log entry is" if older == 1 else "older Log entries are"
    note = f"[{older} {what} in CONTEXT.md and not shown here; read_note {vault}:CONTEXT.md for the full log.]\n\n"
    head = "".join(lines[: entry_starts[0]])
    kept = "".join(entries[i] for i in newest)
    if not kept.endswith("\n\n") and end < len(lines):
        kept += "\n"
    return head + note + kept + "".join(lines[end:])


def practices_block(session: Session) -> str:
    root = session.registry.shared
    included: list[str] = []
    left_out: list[str] = []
    used = 0
    for rel, _ in sorted(index.walk_markdown(root), key=lambda item: item[0]):
        if not rel.startswith("practices/"):
            continue
        note, _errors = notes.read_note(root / rel, session, strict=False)
        if note.frontmatter.get("status") != "accepted":
            continue
        text = f"## {SHARED_NAME}:{rel}\n\n{_read(session, root / rel).strip()}"
        cost = len(text) + 2
        if used + cost > MAX_PRACTICES_CHARS:
            if not included and not left_out:  # a single practice over the cap: show its head
                included.append(_cap(text, MAX_PRACTICES_CHARS, f"{SHARED_NAME}:{rel}"))
                used = MAX_PRACTICES_CHARS
            else:
                left_out.append(f"{SHARED_NAME}:{rel}")
            continue
        included.append(text)
        used += cost
    body = "\n\n".join(included) if included else "(no accepted practices)"
    if left_out:
        body += (f"\n\n[truncated: the {MAX_PRACTICES_CHARS:,}-character cap for practices left out "
                 f"{len(left_out)} accepted practice(s); read them with read_note: {', '.join(left_out)}]")
    return f"# Accepted shared practices\n\n{body}"


def pulled_block(session: Session, name: str) -> str:
    text = _read(session, session.registry.projects[name] / "CONTEXT.md")
    sections = [s for s in (_section(text, h) for h in PULLED_SECTIONS) if s]
    body = "\n\n".join(sections) if sections else f"(no {' or '.join(repr(h) for h in PULLED_SECTIONS)} section)"
    return f"# Pulled project ({name}, read-only): CONTEXT.md, {' and '.join(PULLED_SECTIONS)}\n\n" + \
        _cap(body, MAX_PULLED_CHARS, f"{name}'s CONTEXT.md sections")


def blocks(session: Session) -> list[str]:
    """The six blocks, in order (block 5 is one entry per pulled project, so there may be more or fewer)."""
    out = [
        PREAMBLE,
        f"# Shared vault ({SHARED_NAME}): AGENTS.md\n\n{_read(session, session.registry.shared / 'AGENTS.md').strip()}",
        f"# Active project ({session.active}): AGENTS.md\n\n{_read(session, session.active_vault / 'AGENTS.md').strip()}",
        practices_block(session),
    ]
    out += [pulled_block(session, name) for name in sorted(session.pulled)]
    context = recent_log(_read(session, session.active_vault / "CONTEXT.md"), session.active)
    out.append(f"# Active project ({session.active}): CONTEXT.md\n\n{context.strip()}")
    return out


def system_prompt(session: Session) -> str:
    """The system message for ``session``; identical bytes for identical files."""
    return SEPARATOR.join(blocks(session)) + "\n"
