"""daedalus.core: the safety core. Registry, session, path policy, and read tools."""

from .index import SearchUnavailable, require_fts5
from .links import Link
from .notes import Note, NoteSchemaError
from .policy import Authorized, Op, PolicyError, authorize, authorize_command
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
    read_note,
    search,
)

__all__ = [
    "SCHEMA", "Area", "Authorized", "AmbiguousRefError", "Backlink", "Link", "Note", "NoteEntry",
    "NoteSchemaError", "Op", "PolicyError", "RefError", "Registry", "RegistryError", "SearchHit",
    "SearchUnavailable", "Session", "SessionError", "authorize", "authorize_command", "backlinks",
    "list_notes", "load_registry", "read_note", "require_fts5", "search",
]
