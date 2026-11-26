"""The system message: a fixed role preamble, the shared AGENTS.md, then the project's AGENTS.md.

The assembly must be byte-identical across calls with the same vault state:
DeepSeek's context cache matches on identical prefixes, so a timestamp or a
set-ordered list in here would turn every call into a cache miss. Files are
read in a fixed order, through the path policy.

CONTEXT.md loading and /end come in commit 6.
"""

from __future__ import annotations

from daedalus.core.policy import Op, authorize
from daedalus.core.registry import SHARED_NAME
from daedalus.core.session import Session

PREAMBLE = """\
You are the Daedalus terminal agent, working for the user in their project knowledge vaults.

You can read notes in the active project's vault, any pulled project vaults, and the shared vault of \
practices and lessons, and you can read the active project's code. You can't change anything directly: \
propose_note queues a new note, and nothing is written until the user approves it in this terminal. \
Proposals are recorded as source deepseek; drafts that claim another source are refused.

Ground answers in the notes: read before you answer, cite notes as vault:path, and say when the vaults \
don't cover something. When a tool refuses, its message names the rule; adjust instead of retrying the same \
call. Keep answers short.

The two documents below are the vaults' own rules. Follow them."""


def _read(session: Session, path) -> str:
    auth = authorize(path, Op.READ, session)
    return auth.path.read_text(encoding="utf-8")


def system_prompt(session: Session) -> str:
    """The system message for ``session``; identical bytes for identical vault state."""
    parts = [PREAMBLE]
    sources = (
        (f"Shared vault ({SHARED_NAME}): AGENTS.md", session.registry.shared / "AGENTS.md"),
        (f"Active project ({session.active}): AGENTS.md", session.active_vault / "AGENTS.md"),
    )
    for title, path in sources:
        parts.append(f"# {title}\n\n{_read(session, path).strip()}")
    return "\n\n---\n\n".join(parts) + "\n"
