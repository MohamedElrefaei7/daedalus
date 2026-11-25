"""Part D: wikilink extraction and resolution, and backlinks."""

from __future__ import annotations

import pytest
from conftest import write_note, write_raw

from daedalus.core.links import Catalog, Link, extract_links, parse_link, resolve
from daedalus.core.tools import AmbiguousRefError, backlinks, read_note


@pytest.mark.parametrize(
    "inner,expected",
    [
        ("name", Link("name")),
        ("name|alias", Link("name", alias="alias")),
        ("name#heading", Link("name", heading="heading")),
        ("folder/name", Link("folder/name")),
        ("folder/name#Some heading|shown", Link("folder/name", heading="Some heading", alias="shown")),
        ("name\\|alias", Link("name", alias="alias")),  # escaped pipe inside a table
    ],
)
def test_parse_link_forms(inner, expected) -> None:
    assert parse_link(inner) == expected


def test_same_note_heading_link_is_not_a_link() -> None:
    assert parse_link("#heading") is None


def test_extract_from_body_and_frontmatter() -> None:
    fm = {"related": ["[[a]]", "[[b|B]]"], "shared_refs": ["[[practices/c]]"], "project_refs": [], "other": "[[x]]"}
    body = "See [[d#Why]] and [[e]].\n\n`[[in-code]]`\n\n```\n[[in-fence]]\n```\n"
    links = extract_links(fm, body)
    assert [(l.target, l.key) for l in links] == [
        ("a", "related"), ("b", "related"), ("practices/c", "shared_refs"), ("d", "body"), ("e", "body"),
    ]


def test_extract_ignores_unquoted_wikilink_items() -> None:
    assert extract_links({"related": [["a"]]}, "") == ()


@pytest.fixture
def catalog() -> Catalog:
    return Catalog.build("v", ["decisions/storage.md", "components/fetcher.md", "concepts/Quake Magnitude.md",
                               "inbox/storage.md", "a/b/deep.md"])


def test_resolve_by_stem(catalog) -> None:
    res = resolve("fetcher", [catalog])
    assert res.resolved and res.matches[0].rel_path == "components/fetcher.md"


def test_resolve_is_case_insensitive_and_strips_md(catalog) -> None:
    assert resolve("quake magnitude.md", [catalog]).matches[0].rel_path == "concepts/Quake Magnitude.md"


def test_resolve_by_vault_relative_path(catalog) -> None:
    res = resolve("inbox/storage", [catalog])
    assert res.resolved and res.matches[0].rel_path == "inbox/storage.md"


def test_resolve_by_trailing_path(catalog) -> None:
    assert resolve("b/deep", [catalog]).matches[0].rel_path == "a/b/deep.md"


def test_resolve_ambiguous_stem_returns_all(catalog) -> None:
    res = resolve("storage", [catalog])
    assert res.ambiguous
    assert [m.rel_path for m in res.matches] == ["decisions/storage.md", "inbox/storage.md"]


def test_resolve_unresolved(catalog) -> None:
    res = resolve("nothing", [catalog])
    assert not res.resolved and not res.ambiguous


def test_ambiguous_bare_ref_refused(world) -> None:
    write_note(world.alpha, "concepts/storage-engine.md", "concept")
    with pytest.raises(AmbiguousRefError) as exc:
        read_note(world.session, "storage-engine")
    assert {m.rel_path for m in exc.value.matches} == {"decisions/storage-engine.md", "concepts/storage-engine.md"}


def test_bare_ref_resolves(world) -> None:
    assert read_note(world.session, "[[usgs-fetcher]]").frontmatter["type"] == "component"


def test_frontmatter_links_count_as_backlinks(world) -> None:
    # storage-engine links usgs-fetcher in related and in the body; this one only in frontmatter.
    write_note(world.alpha, "experiments/fetch-speed.md", "experiment", "# Fetch speed\n", related=["[[usgs-fetcher]]"])
    hits = {(h.vault, h.rel_path): h.via for h in backlinks(world.session, "usgs-fetcher")}
    assert hits == {
        ("alpha", "decisions/storage-engine.md"): ("related", "body"),
        ("alpha", "experiments/fetch-speed.md"): ("related",),
    }


def test_shared_refs_backlink_into_shared_vault(world) -> None:
    hits = backlinks(world.session, "shared:practices/red-test-first.md")
    assert [(h.vault, h.rel_path, h.via) for h in hits] == [("alpha", "decisions/storage-engine.md", ("shared_refs",))]


def test_body_links_stay_in_their_vault(world) -> None:
    # A body link to a name that exists only in another vault doesn't resolve there.
    write_raw(world.beta, "inbox/mention.md", "Mentions [[usgs-fetcher]] by name.\n")
    assert all(h.vault == "alpha" for h in backlinks(world.session, "usgs-fetcher"))


def test_project_refs_backlink_across_projects(world) -> None:
    write_note(world.beta, "concepts/cross.md", "concept", project_refs=["[[usgs-fetcher]]"])
    hits = backlinks(world.session, "alpha:components/usgs-fetcher.md")
    assert ("beta", "concepts/cross.md", ("project_refs",)) in [(h.vault, h.rel_path, h.via) for h in hits]


def test_backlink_through_ambiguous_link_is_flagged(world) -> None:
    write_note(world.alpha, "inbox/usgs-fetcher.md", None, "draft\n")
    hits = backlinks(world.session, "alpha:components/usgs-fetcher.md")
    assert hits and all(h.ambiguous for h in hits)
