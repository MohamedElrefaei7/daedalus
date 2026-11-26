"""Functions run in separate processes by the concurrency tests.

They live in their own module (no pytest import) so a spawned child can import
them. Each child points the queue and index at the test's temp dirs itself,
because module attributes patched in the parent don't cross a spawn.
"""

from __future__ import annotations

from pathlib import Path


def _setup(registry_path: str, state_root: str, cache_root: str, active: str = "alpha"):
    from daedalus.core import index, queue
    from daedalus.core.registry import load_registry
    from daedalus.core.session import Session

    queue.STATE_ROOT = Path(state_root)
    index.CACHE_ROOT = Path(cache_root)
    return Session(load_registry(registry_path), active=active)


def propose_many(registry_path: str, state_root: str, cache_root: str, worker: int, count: int, results) -> None:
    from daedalus.core.tools import propose_note

    session = _setup(registry_path, state_root, cache_root)
    ids = []
    try:
        for i in range(count):
            p = propose_note(session, "alpha", f"inbox/w{worker}-{i:03d}.md", f"draft {worker}-{i}\n", "claude",
                             f"draft {worker}-{i}")
            ids.append(p.id)
        results.put(("ok", ids))
    except Exception as e:  # report, don't hang the parent
        results.put(("error", repr(e)))


def write_log_race(registry_path: str, state_root: str, cache_root: str, run_id: str, body: str, barrier,
                   results) -> None:
    """write_log, held at a barrier just after authorize() so both racers pass the policy check."""
    from daedalus.core import tools
    from daedalus.core.policy import PolicyError

    session = _setup(registry_path, state_root, cache_root)
    real_authorize = tools.authorize

    def gated(*args, **kwargs):
        auth = real_authorize(*args, **kwargs)
        barrier.wait(timeout=30)
        return auth

    tools.authorize = gated
    try:
        sha = tools.write_log(session, run_id, body, "deepseek")
        results.put(("ok", body, sha))
    except PolicyError as e:
        results.put(("refused", e.rule, str(e)))
    except Exception as e:
        results.put(("error", repr(e), ""))


def apply_and_crash(registry_path: str, state_root: str, cache_root: str, change_id: str, when: str) -> None:
    """apply(), killed with os._exit(1) at ``when``: "after_replace" or "after_commit"."""
    import os

    from daedalus.core import apply as apply_mod

    session = _setup(registry_path, state_root, cache_root)
    name = {"after_replace": "atomic_write_at", "after_commit": "commit_path"}[when]
    real = getattr(apply_mod, name)

    def then_crash(*args, **kwargs):
        real(*args, **kwargs)
        os._exit(1)

    setattr(apply_mod, name, then_crash)
    apply_mod.apply(session, change_id)
    os._exit(0)  # not reached


def hold_lock_and_crash(state_root: str) -> None:
    import os

    from daedalus.core import apply as apply_mod
    from daedalus.core import queue

    queue.STATE_ROOT = __import__("pathlib").Path(state_root)
    with apply_mod.write_lock():
        os._exit(1)


def hold_lock_until(state_root: str, held, release) -> None:
    from pathlib import Path

    from daedalus.core import apply as apply_mod
    from daedalus.core import queue

    queue.STATE_ROOT = Path(state_root)
    with apply_mod.write_lock():
        held.set()
        release.wait(timeout=30)


def answer_and_crash(registry_path: str, state_root: str, cache_root: str, question_id: str) -> None:
    """answer_question, killed with os._exit(1) after both files are written and before the commit."""
    import os

    from daedalus.core import accept, apply as apply_mod

    session = _setup(registry_path, state_root, cache_root)

    def crash(*args, **kwargs):
        os._exit(1)

    apply_mod.commit_paths = crash
    accept.answer_question(session, question_id, "1")
    os._exit(0)  # not reached
