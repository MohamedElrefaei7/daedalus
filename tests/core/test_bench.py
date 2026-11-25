"""Benchmark: indexed search and authorize() on a 10,000-note vault.

The vault is generated once per session: ~250 words per note, five folders,
one related link per note. Timings are medians of 5 warm calls; search
includes the freshness walk.
"""

from __future__ import annotations

import os
import random
import statistics
import time
from pathlib import Path

import pytest
import yaml
from conftest import frontmatter, make_project, make_shared, write_registry

from daedalus.core.policy import Op, authorize
from daedalus.core.registry import load_registry
from daedalus.core.session import Session
from daedalus.core.tools import search

N_NOTES = 10_000
WORDS_PER_NOTE = 250
FOLDERS = {"decisions": "decision", "components": "component", "experiments": "experiment",
           "concepts": "concept", "goals": "goal"}
SEARCH_BUDGET_MS = 50.0
AUTHORIZE_BUDGET_MS = 1.0


@pytest.fixture(scope="session")
def big(tmp_path_factory: pytest.TempPathFactory):
    root = Path(os.path.realpath(tmp_path_factory.mktemp("bench")))
    shared = make_shared(root)
    vault = make_project(root, "big")
    rng = random.Random(2026)
    letters = "abcdefghijklmnopqrstuvwxyz"
    vocab = sorted({"".join(rng.choices(letters, k=rng.randint(4, 9))) for _ in range(3000)})
    folder_names = list(FOLDERS)
    for i in range(N_NOTES):
        folder = folder_names[i % len(folder_names)]
        fm = frontmatter(FOLDERS[folder], related=[f"[[note-{(i + 1) % N_NOTES:05d}]]"])
        body = " ".join(rng.choices(vocab, k=WORDS_PER_NOTE))
        dumped = yaml.safe_dump(fm, sort_keys=False, default_flow_style=None)
        (vault / folder / f"note-{i:05d}.md").write_text(f"---\n{dumped}---\n\n# Note {i}\n\n{body}\n")
    session = Session(load_registry(write_registry(root / "vaults.toml", shared, {"big": vault})), active="big")
    query = f"{vocab[100]} {vocab[2000]}"
    search(session, query)  # cold: builds the index
    return session, vault, query


def median_ms(fn, runs: int = 5) -> float:
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t0) * 1000)
    return statistics.median(times)


def test_bench_vault_shape(big) -> None:
    _, vault, _ = big
    assert sum(1 for f in FOLDERS for _ in (vault / f).glob("*.md")) == N_NOTES


def test_search_under_50ms(big, bench_results) -> None:
    session, _, query = big
    assert search(session, query), "benchmark query should match something"
    ms = median_ms(lambda: search(session, query))
    bench_results.append(f"search() on {N_NOTES} notes, warm, incl. freshness walk: median {ms:.1f} ms "
                         f"(budget {SEARCH_BUDGET_MS:.0f} ms)")
    assert ms < SEARCH_BUDGET_MS, f"search() median {ms:.1f} ms exceeds {SEARCH_BUDGET_MS} ms"


def test_authorize_under_1ms(big, bench_results) -> None:
    session, vault, _ = big
    path = vault / "decisions" / "note-00042.md"
    ms = median_ms(lambda: authorize(path, Op.READ, session))
    bench_results.append(f"authorize(): median {ms * 1000:.1f} µs (budget {AUTHORIZE_BUDGET_MS:.0f} ms)")
    assert ms < AUTHORIZE_BUDGET_MS, f"authorize() median {ms:.3f} ms exceeds {AUTHORIZE_BUDGET_MS} ms"
