"""daedalus.core: the safety core. Registry, session, path policy, read and write tools.

apply() is deliberately not re-exported here: binding the name ``apply`` in
this package would shadow the daedalus.core.apply module. Import it as
``from daedalus.core.apply import apply``.
"""

from .apply import diff, pending, recent_rejections, reject
from .index import SearchUnavailable, require_fts5
from .links import Link
from .notes import Note, NoteSchemaError
from .policy import Authorized, Op, PolicyError, authorize, authorize_command
from .queue import (
    CommitFailed, DraftInvalid, NotPending, Proposal, ProposalError, ProposalTooLarge, StaleProposal,
)
from .registry import Registry, RegistryError, load_registry
from .schema import SCHEMA
from .session import Area, Session, SessionError
from .tools import (
    AmbiguousRefError,
    Backlink,
    NoteEntry,
    RefError,
    SearchHit,
    backlinks,
    list_notes,
    propose_edit,
    propose_lesson,
    propose_note,
    read_note,
    search,
    write_log,
)

__all__ = [
    "SCHEMA", "Area", "Authorized", "AmbiguousRefError", "Backlink", "Link", "Note", "NoteEntry",
    "NoteSchemaError", "Op", "PolicyError", "RefError", "Registry", "RegistryError", "SearchHit",
    "SearchUnavailable", "Session", "SessionError", "authorize", "authorize_command", "backlinks",
    "list_notes", "load_registry", "read_note", "require_fts5", "search",
    "CommitFailed", "DraftInvalid", "NotPending", "Proposal", "ProposalError", "ProposalTooLarge",
    "StaleProposal", "diff", "pending", "propose_edit", "propose_lesson", "propose_note",
    "recent_rejections", "reject", "write_log",
]
