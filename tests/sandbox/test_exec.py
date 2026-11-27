"""The executor's process handling, with real (unsandboxed) processes: timeouts kill the whole group."""

from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

from daedalus.sandbox.exec import TAIL_BYTES, Limits, spawn

ENV = {"PATH": "/usr/bin:/bin"}


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def pids_from(text: str) -> list[int]:
    return [int(x) for x in text.split() if x.isdigit()]


def test_a_timeout_kills_the_leader_and_its_child(tmp_path) -> None:
    script = ("import subprocess, sys, time\n"
              "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
              "print(c.pid, flush=True); time.sleep(60)\n")
    start = time.monotonic()
    done = spawn([sys.executable, "-c", script], cwd=tmp_path, env=ENV, timeout_s=1, limits=None)
    assert done.timed_out and time.monotonic() - start < 6
    (child,) = pids_from(done.stdout_tail)
    assert not alive(child)


def test_a_child_that_ignores_sigterm_gets_sigkill(tmp_path) -> None:
    script = "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nprint('ready', flush=True)\ntime.sleep(60)\n"
    start = time.monotonic()
    done = spawn([sys.executable, "-c", script], cwd=tmp_path, env=ENV, timeout_s=1, limits=None)
    assert done.timed_out and 2.5 < time.monotonic() - start < 8  # 1 s, then the 2 s grace, then SIGKILL


def test_a_background_child_never_outlives_a_normal_exit(tmp_path) -> None:
    marker = f"daedalus-bg-{uuid.uuid4().hex}"
    script = (f"import subprocess, sys\nc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', '{marker}'])\n"
              "print(c.pid, flush=True)\n")
    done = spawn([sys.executable, "-c", script], cwd=tmp_path, env=ENV, timeout_s=30, limits=None)
    assert done.exit_code == 0 and not done.timed_out
    (child,) = pids_from(done.stdout_tail)
    assert not alive(child)


def test_output_keeps_the_last_200_kb(tmp_path) -> None:
    script = "import sys\nsys.stdout.write('a' * 300000 + 'END')\nsys.stderr.write('e' * 10)\n"
    done = spawn([sys.executable, "-c", script], cwd=tmp_path, env=ENV, timeout_s=30, limits=None)
    assert len(done.stdout_tail) == TAIL_BYTES and done.stdout_tail.endswith("END") and done.stderr_tail == "e" * 10


def test_limits_reach_the_child(tmp_path) -> None:
    script = ("import resource as r\n"
              "print(r.getrlimit(r.RLIMIT_CPU)[0], r.getrlimit(r.RLIMIT_FSIZE)[0], r.getrlimit(r.RLIMIT_NOFILE)[0],"
              " r.getrlimit(r.RLIMIT_CORE)[0])\n")
    done = spawn([sys.executable, "-c", script], cwd=tmp_path, env=ENV, timeout_s=30, limits=Limits(cpu_s=7))
    cpu, fsize, nofile, core = pids_from(done.stdout_tail)
    assert (cpu, fsize, core) == (7, 512 * 1024 * 1024, 0) and nofile <= 1024


def test_the_file_size_limit_stops_a_huge_write(tmp_path) -> None:
    target = tmp_path / "big"
    script = (f"import signal\nsignal.signal(signal.SIGXFSZ, signal.SIG_IGN)\n"
              f"f = open({str(target)!r}, 'wb', buffering=0)\n"
              "print('first', f.write(b'x' * 2048))  # cut short at the limit\n"
              "try:\n    f.write(b'x')\nexcept OSError as e:\n    print('EFBIG', e.errno)\n")
    done = spawn([sys.executable, "-c", script], cwd=tmp_path, env=ENV, timeout_s=30,
                 limits=Limits(cpu_s=10, fsize_bytes=1024))
    assert "first 1024" in done.stdout_tail and "EFBIG 27" in done.stdout_tail
    assert Path(target).stat().st_size == 1024
