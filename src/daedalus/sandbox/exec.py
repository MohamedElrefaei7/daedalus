"""The executor: the one place a process is spawned. Tests inject a fake, so they can see every spawn.

spawn() runs argv in exec form (no shell) in its own session, so a timeout
can kill the whole process group: SIGTERM, up to 2 s for it to exit, then
SIGKILL. Whatever the outcome, the group is SIGKILLed once the leader is done,
so a background child can't outlive its run, and the group is confirmed gone.
Output goes to unnamed temp files, of which the last 200 KB each is kept.
"""

from __future__ import annotations

import os
import resource
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol, Sequence

from .errors import RunnerError

TAIL_BYTES = 200 * 1024
TERM_GRACE_S = 2.0
GONE_WAIT_S = 3.0


@dataclass(frozen=True)
class Limits:
    """setrlimit values for the child. macOS doesn't reliably enforce memory limits, so there's none here."""

    cpu_s: int
    nofile: int = 1024
    fsize_bytes: int = 512 * 1024 * 1024

    def apply(self) -> None:  # runs in the child between fork and exec
        for which, value in ((resource.RLIMIT_CPU, self.cpu_s), (resource.RLIMIT_FSIZE, self.fsize_bytes),
                             (resource.RLIMIT_CORE, 0)):
            resource.setrlimit(which, (value, value))
        _, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        n = self.nofile if hard == resource.RLIM_INFINITY else min(self.nofile, hard)
        resource.setrlimit(resource.RLIMIT_NOFILE, (n, n))


@dataclass(frozen=True)
class Completed:
    exit_code: int
    stdout_tail: str
    stderr_tail: str
    duration_s: float
    timed_out: bool


class Executor(Protocol):
    def __call__(self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout_s: float | None,
                 limits: Limits | None) -> Completed: ...


def _tail(f) -> str:
    size = f.seek(0, os.SEEK_END)
    f.seek(max(0, size - TAIL_BYTES))
    return f.read().decode("utf-8", errors="replace")


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _wait_gone(proc: subprocess.Popen, pgid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        proc.poll()  # reap the leader, so a zombie doesn't keep the group alive
        if proc.returncode is not None and not _group_alive(pgid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def spawn(argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout_s: float | None,
          limits: Limits | None) -> Completed:
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        start = time.monotonic()
        proc = subprocess.Popen(list(argv), cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                start_new_session=True, close_fds=True,
                                preexec_fn=limits.apply if limits is not None else None)
        pgid, timed_out = proc.pid, False
        try:
            try:
                proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                _signal_group(pgid, signal.SIGTERM)
                if not _wait_gone(proc, pgid, TERM_GRACE_S):
                    _signal_group(pgid, signal.SIGKILL)
        finally:  # also on Ctrl-C: the child is in its own session, so it never saw the SIGINT
            _signal_group(pgid, signal.SIGKILL)
            gone = _wait_gone(proc, pgid, GONE_WAIT_S)
        duration = time.monotonic() - start
        if not gone:
            raise RunnerError(f"processes in group {pgid} survived SIGKILL; check with `pgrep -g {pgid}`")
        return Completed(proc.returncode, _tail(out), _tail(err), duration, timed_out)
