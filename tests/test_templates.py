"""Contract test for the vault templates.

Parses the real files under templates/ and config/; nothing is mocked. If a
template, a schema block in the docs, or the example registry drifts from
SCHEMA and VAULTS below, a test here fails and names the file and key.
"""

from __future__ import annotations

import difflib
import re
import tomllib
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
TEMPLATES = REPO / "templates"

# The frontmatter schema, defined once. The schema blocks in both AGENTS.md
# files and in CLAUDE.md must say exactly this (test_schema_block_matches_docs).
SCHEMA = {
    "base": ["type", "status", "created", "related", "shared_refs", "project_refs", "source"],
    "extra": {
        "component": ["code"],
        "goal": ["done_when", "max_iterations", "budget_usd", "checkin_every"],
        "question": ["options", "answered"],
        "plan": ["spec", "decisions", "tasks"],
        "practice": ["applies_to"],
        "lesson": ["applies_to"],
    },
    "enums": {
        "status": ["draft", "proposed", "accepted", "superseded"],
        "source": ["human", "deepseek", "claude"],
    },
}

# What each vault template must contain.
VAULTS = {
    "project-vault": {
        "folders": [
            "goals", "sources", "logs", "inbox", "questions", "decisions",
            "components", "experiments", "concepts", "plans", "templates",
        ],
        "files": ["AGENTS.md", "CONTEXT.md", ".gitignore"],
        "types": ["decision", "component", "experiment", "concept", "goal", "question", "plan"],
        "gitignore": [".obsidian/workspace.json", ".obsidian/workspace-mobile.json", ".daedalus/"],
    },
    "shared-vault": {
        "folders": ["practices", "lessons", "inbox", "templates"],
        "files": ["AGENTS.md", ".gitignore"],
        "types": ["practice", "lesson"],
        "gitignore": [".obsidian/workspace.json", ".obsidian/workspace-mobile.json", ".daedalus/"],
    },
}

CONTEXT_SECTIONS = ["## Log", "## Decisions index", "## Where things live", "## Up Next"]

SCHEMA_DOCS = {
    "templates/project-vault/AGENTS.md": TEMPLATES / "project-vault" / "AGENTS.md",
    "templates/shared-vault/AGENTS.md": TEMPLATES / "shared-vault" / "AGENTS.md",
    "CLAUDE.md": REPO / "CLAUDE.md",
}


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_empty_list(v: object) -> bool:
    return v == [] and isinstance(v, list)


# The value each template ships with, checked only when the key is present
# (presence is checked by the base-key and type-specific-key tests).
TEMPLATE_VALUES = {
    "status": (lambda v: v == "draft", '"draft"'),
    "source": (lambda v: v == "human", '"human"'),
    "created": (lambda v: v == "{{date}}", 'the string "{{date}}"'),
    "related": (_is_empty_list, "an empty list []"),
    "shared_refs": (_is_empty_list, "an empty list []"),
    "project_refs": (_is_empty_list, "an empty list []"),
    "code": (_is_empty_list, "an empty list []"),
    "options": (_is_empty_list, "an empty list []"),
    "decisions": (_is_empty_list, "an empty list []"),
    "tasks": (_is_empty_list, "an empty list []"),
    "applies_to": (_is_empty_list, "an empty list []"),
    "answered": (lambda v: v is False, "false"),
    "spec": (lambda v: isinstance(v, str), "a string"),
    "done_when": (lambda v: isinstance(v, str), "a string"),
    "max_iterations": (lambda v: _is_int(v) and v > 0, "a positive integer"),
    "checkin_every": (lambda v: _is_int(v) and v > 0, "a positive integer"),
    "budget_usd": (
        lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0,
        "a non-negative number",
    ),
}

FRONTMATTER = re.compile(r"\A---\n(.*?\n)---\n", re.DOTALL)
SCHEMA_BLOCK = re.compile(r"^```yaml\n(# Base keys.*?)^```", re.DOTALL | re.MULTILINE)
EXTRA_MARKER = "# Extra required keys by type"


def rel(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


def vault_of(path: Path) -> str:
    return path.relative_to(TEMPLATES).parts[0]


TEMPLATE_FILES = sorted(TEMPLATES.glob("*/templates/*.md"))
by_template = pytest.mark.parametrize(
    "path", TEMPLATE_FILES, ids=[f"{vault_of(p)}/{p.name}" for p in TEMPLATE_FILES]
)


def split_frontmatter(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    m = FRONTMATTER.match(text)
    if not m:
        pytest.fail(f"{rel(path)}: no '---'-delimited frontmatter block at the top of the file")
    return m.group(1), text[m.end():]


def load_frontmatter(path: Path) -> dict:
    raw, _ = split_frontmatter(path)
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        pytest.fail(f"{rel(path)}: frontmatter is not valid YAML:\n{e}")
    if not isinstance(data, dict):
        pytest.fail(f"{rel(path)}: frontmatter must be a YAML mapping, got {type(data).__name__}")
    return data


def schema_block(name: str) -> str:
    m = SCHEMA_BLOCK.search(SCHEMA_DOCS[name].read_text(encoding="utf-8"))
    if not m:
        pytest.fail(f"{name}: no ```yaml schema block starting with '# Base keys'")
    return m.group(1)


# 1. Skeleton folders and files exist -----------------------------------------

@pytest.mark.parametrize(
    "vault,folder",
    [(v, f) for v, spec in VAULTS.items() for f in spec["folders"]],
    ids=[f"{v}/{f}" for v, spec in VAULTS.items() for f in spec["folders"]],
)
def test_skeleton_folder_exists(vault: str, folder: str) -> None:
    d = TEMPLATES / vault / folder
    assert d.is_dir(), f"templates/{vault}: missing folder '{folder}/'"
    assert (d / ".gitkeep").is_file(), (
        f"templates/{vault}/{folder}/: missing .gitkeep, so git won't track the folder while it's empty"
    )


@pytest.mark.parametrize(
    "vault,name",
    [(v, f) for v, spec in VAULTS.items() for f in spec["files"]],
    ids=[f"{v}/{f}" for v, spec in VAULTS.items() for f in spec["files"]],
)
def test_skeleton_file_exists(vault: str, name: str) -> None:
    assert (TEMPLATES / vault / name).is_file(), f"templates/{vault}: missing file '{name}'"


@pytest.mark.parametrize(
    "vault,type_",
    [(v, t) for v, spec in VAULTS.items() for t in spec["types"]],
    ids=[f"{v}/{t}.md" for v, spec in VAULTS.items() for t in spec["types"]],
)
def test_note_template_exists(vault: str, type_: str) -> None:
    path = TEMPLATES / vault / "templates" / f"{type_}.md"
    assert path.is_file(), f"templates/{vault}/templates: missing note template '{type_}.md'"


def test_project_context_has_sections() -> None:
    path = TEMPLATES / "project-vault" / "CONTEXT.md"
    lines = path.read_text(encoding="utf-8").splitlines()
    missing = [s for s in CONTEXT_SECTIONS if s not in lines]
    assert not missing, f"{rel(path)}: missing section heading(s) {missing}"


# 2. Frontmatter parses --------------------------------------------------------

@by_template
def test_frontmatter_parses(path: Path) -> None:
    load_frontmatter(path)


# 3. Base keys present ---------------------------------------------------------

@by_template
def test_base_keys_present(path: Path) -> None:
    fm = load_frontmatter(path)
    missing = [k for k in SCHEMA["base"] if k not in fm]
    assert not missing, f"{rel(path)}: missing base key(s) {missing}"


# 4. Type matches filename -----------------------------------------------------

@by_template
def test_type_matches_filename(path: Path) -> None:
    known = VAULTS[vault_of(path)]["types"]
    assert path.stem in known, (
        f"{rel(path)}: '{path.stem}' is not a note type for {vault_of(path)} (expected one of {known})"
    )
    fm = load_frontmatter(path)
    assert fm.get("type") == path.stem, (
        f"{rel(path)}: key 'type' is {fm.get('type')!r}, expected {path.stem!r} (the filename stem)"
    )


# 5. Type-specific keys present ------------------------------------------------

@by_template
def test_type_specific_keys_present(path: Path) -> None:
    required = SCHEMA["extra"].get(path.stem, [])
    fm = load_frontmatter(path)
    missing = [k for k in required if k not in fm]
    assert not missing, (
        f"{rel(path)}: missing type-specific key(s) {missing} required for type '{path.stem}'"
    )


# 6. Enum values valid ---------------------------------------------------------

@by_template
@pytest.mark.parametrize("key", list(SCHEMA["enums"]))
def test_enum_value_valid(path: Path, key: str) -> None:
    fm = load_frontmatter(path)
    allowed = SCHEMA["enums"][key]
    assert fm.get(key) in allowed, (
        f"{rel(path)}: key '{key}' is {fm.get(key)!r}, expected one of {allowed}"
    )


# Template values: what a fresh note starts with --------------------------------

@by_template
def test_template_values(path: Path) -> None:
    fm = load_frontmatter(path)
    bad = [
        f"key '{k}' is {fm[k]!r}, expected {want}"
        for k, (ok, want) in TEMPLATE_VALUES.items()
        if k in fm and not ok(fm[k])
    ]
    assert not bad, f"{rel(path)}:\n  " + "\n  ".join(bad)


@by_template
def test_body_starts_with_title(path: Path) -> None:
    _, body = split_frontmatter(path)
    first = next((line for line in body.splitlines() if line.strip()), "")
    assert first == "# {{title}}", (
        f"{rel(path)}: body must start with '# {{{{title}}}}', found {first!r}"
    )


QUOTED = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\']|\'\')*\'')


@by_template
def test_frontmatter_has_no_comments(path: Path) -> None:
    # Obsidian's Properties panel drops YAML comments when it rewrites
    # frontmatter, so field docs belong in the body, not in comments.
    raw, _ = split_frontmatter(path)
    commented = [
        ln for ln in raw.splitlines()
        if re.search(r"(^|\s)#", QUOTED.sub('""', ln))
    ]
    assert not commented, (
        f"{rel(path)}: frontmatter has YAML comment(s), which Obsidian strips; "
        f"move them into the body: {commented}"
    )


# 7. Schema and docs agree -----------------------------------------------------

@pytest.mark.parametrize("name", list(SCHEMA_DOCS))
def test_schema_block_matches_docs(name: str) -> None:
    block = schema_block(name)
    assert EXTRA_MARKER in block, f"{name}: schema block lacks the '{EXTRA_MARKER}' line"
    base_text, extra_text = block.split(EXTRA_MARKER, 1)
    doc_base = list(yaml.safe_load(base_text) or {})
    doc_extra = {
        t: [k.strip() for k in str(v).split(",")]
        for t, v in (yaml.safe_load(extra_text) or {}).items()
    }

    problems = []
    problems += [f"missing base key '{k}'" for k in SCHEMA["base"] if k not in doc_base]
    problems += [f"base key '{k}' is not in SCHEMA" for k in doc_base if k not in SCHEMA["base"]]
    for t, keys in SCHEMA["extra"].items():
        problems += [f"missing key '{k}' for type '{t}'" for k in keys if k not in doc_extra.get(t, [])]
    for t, keys in doc_extra.items():
        problems += [
            f"key '{k}' for type '{t}' is not in SCHEMA"
            for k in keys if k not in SCHEMA["extra"].get(t, [])
        ]
    for key, values in SCHEMA["enums"].items():
        line = next((ln for ln in base_text.splitlines() if ln.startswith(f"{key}:")), "")
        problems += [
            f"enum value '{v}' for key '{key}' is not listed"
            for v in values if not re.search(rf"\b{re.escape(v)}\b", line)
        ]
    assert not problems, f"{name}: schema block disagrees with SCHEMA:\n  " + "\n  ".join(problems)


def test_schema_blocks_identical() -> None:
    names = list(SCHEMA_DOCS)
    reference = schema_block(names[0])
    for name in names[1:]:
        other = schema_block(name)
        diff = "".join(difflib.unified_diff(
            reference.splitlines(keepends=True), other.splitlines(keepends=True),
            fromfile=names[0], tofile=name,
        ))
        assert other == reference, f"{name}: schema block is not an exact copy of {names[0]}'s:\n{diff}"


# 8. Gitignore covers workspace and .daedalus/ ---------------------------------

@pytest.mark.parametrize("vault", list(VAULTS))
def test_gitignore_entries(vault: str) -> None:
    path = TEMPLATES / vault / ".gitignore"
    lines = {ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()}
    missing = [e for e in VAULTS[vault]["gitignore"] if e not in lines]
    assert not missing, f"{rel(path)}: missing entr(ies) {missing}"


# 9. Example registry parses ---------------------------------------------------

def test_example_registry_parses() -> None:
    path = REPO / "config" / "vaults.example.toml"
    with path.open("rb") as f:
        data = tomllib.load(f)
    assert isinstance(data.get("shared"), str), f"{rel(path)}: key 'shared' must be a string"
    projects = data.get("projects")
    assert isinstance(projects, dict), f"{rel(path)}: key 'projects' must be a table"
    bad = [k for k, v in projects.items() if not isinstance(v, str)]
    assert not bad, f"{rel(path)}: project path(s) {bad} must be strings"
