"""Live smoke test of the read layer against the real registry.

Run: uv run python scripts/smoke_read.py

Loads ~/.config/daedalus/vaults.toml with active="scratch", then lists notes,
reads a shared practice, finds its backlinks, and searches. It writes nothing
into any vault; afterwards `git status` in each vault should still be clean.
"""

from __future__ import annotations

from daedalus.core import Session, backlinks, index, list_notes, load_registry, read_note, search


def main() -> None:
    registry = load_registry()
    session = Session(registry, active="scratch")
    print(f"registry: {registry.source}")
    for name, root in session.scope:
        print(f"  {name:8} {root}")

    print("\nlist_notes:")
    for e in list_notes(session):
        print(f"  {e.vault:8} {e.rel_path:40} type={e.type} status={e.status}")

    note = read_note(session, "shared:practices/red-test-first.md")
    print("\nread_note('shared:practices/red-test-first.md'):")
    print(f"  path:        {note.path}")
    print(f"  type/status: {note.frontmatter['type']} / {note.frontmatter['status']}")
    print(f"  applies_to:  {note.frontmatter['applies_to']}")
    print(f"  links:       {[(l.target, l.key) for l in note.links]}")

    print("\nbacklinks('one-commit-one-change'):")
    for b in backlinks(session, "one-commit-one-change"):
        print(f"  {b.vault}:{b.rel_path} via {', '.join(b.via)}")

    print("\nsearch('failing test'):")
    for hit in search(session, "failing test"):
        print(f"  {hit.vault}:{hit.rel_path}\n    {hit.snippet}")

    print("\nindex files (outside every vault):")
    for _, root in session.scope:
        print(f"  {index.index_path(root)}")


if __name__ == "__main__":
    main()
