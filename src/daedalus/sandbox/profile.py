"""The Seatbelt (SBPL) profile for one run, and the -D parameters that carry its paths.

The profile text never contains a path: every path is a `(param "NAME")`,
passed as `sandbox-exec -D NAME=<realpath>`. Each one is resolved with
realpath first, because Seatbelt matches real paths and on macOS /tmp and
/var are symlinks into /private: a profile allowing /tmp/run123 allows nothing.

Shape: start from `(allow default)`, then deny the four things that matter,
re-allowing only what the run needs. Later rules win in SBPL, so each deny
comes before its exceptions.
  * Network: all of it, including Unix sockets; DNS also goes through
    mDNSResponder's mach services, so those are denied too. allow_localhost
    re-allows loopback only.
  * Writes: everywhere, except the run's temp folder and a few /dev nodes.
    The worktree is read-only to the run, so tests can't change the code they test.
  * Reads under the real home folder, except the worktree, the project venv,
    the Python installs it links to, and the run's temp folder. The worktree's
    vault/ and .git stay unreadable (rule 4).
  * Metadata (stat) only where pytest needs it. Seatbelt answers a denied stat
    with EPERM, which Python's Path.is_file() raises on, so pytest's upward
    search for its config breaks unless these answer: each ancestor folder of
    the worktree up to home, and in those folders only pytest's config
    filenames (PYTEST_CONFIG_FILES). Inside the worktree, vault/ and .git keep
    metadata too, since pytest stats every entry it collects. Metadata gives
    existence, size, mode, and times, never contents, a listing, or xattrs.
  * The Keychain: securityd's mach services, as well as the files (under home).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DNS_SERVICES = ("com.apple.dnssd.service", "com.apple.mDNSResponder")
KEYCHAIN_SERVICES = ("com.apple.SecurityServer", "com.apple.securityd")
PYTEST_CONFIG_FILES = ("pytest.ini", ".pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg", "conftest.py")
WRITABLE_DEVICES = ('(literal "/dev/null")', '(literal "/dev/zero")', '(regex #"^/dev/tty")', '(subpath "/dev/fd")')


def real(path: str | os.PathLike[str]) -> str:
    return os.path.realpath(os.path.expanduser(os.fspath(path)))


@dataclass(frozen=True)
class ProfileInputs:
    home: str
    worktree: str
    venv: str
    run_tmp: str
    python_dirs: tuple[str, ...]  # uv's Python folder, and the venv's base Python if it's under home
    allow_localhost: bool = False


def ancestors_under_home(home: str, worktree: str) -> list[str]:
    """The worktree's ancestor folders from home down to its parent; none if it isn't under home."""
    h, wt = Path(home), Path(worktree)
    if h not in wt.parents:
        return []
    return [str(p) for p in reversed(wt.parents) if p == h or h in p.parents]


def _key(filename: str) -> str:
    return ("DOT_" if filename.startswith(".") else "") + filename.lstrip(".").replace(".", "_").upper()


def params(inputs: ProfileInputs) -> dict[str, str]:
    """The -D parameters, every path a realpath."""
    home, wt = real(inputs.home), real(inputs.worktree)
    out = {"HOME": home, "WORKTREE": wt, "WORKTREE_VAULT": os.path.join(wt, "vault"),
           "WORKTREE_GIT": os.path.join(wt, ".git"), "VENV": real(inputs.venv), "RUN_TMP": real(inputs.run_tmp)}
    for i, d in enumerate(dict.fromkeys(real(p) for p in inputs.python_dirs)):
        out[f"PYTHON_{i}"] = d
    for i, folder in enumerate(ancestors_under_home(home, wt)):
        out[f"UP_{i}"] = folder
        for name in PYTEST_CONFIG_FILES:
            out[f"UP_{i}_{_key(name)}"] = os.path.join(folder, name)
    return out


def _services(names: tuple[str, ...]) -> str:
    return " ".join(f'(global-name "{n}")' for n in names)


def render(inputs: ProfileInputs) -> str:
    """The profile text. It names parameters only, never paths."""
    keys = params(inputs)
    pythons = " ".join(f'(subpath (param "{k}"))' for k in keys if k.startswith("PYTHON_"))
    upward = " ".join(f'(literal (param "{k}"))' for k in keys if k.startswith("UP_"))
    upward_rule = f"(allow file-read-metadata {upward})\n" if upward else ""
    localhost = ('(allow network-bind (local ip "localhost:*"))\n'
                 '(allow network-inbound (local ip "localhost:*"))\n'
                 '(allow network-outbound (remote ip "localhost:*"))\n') if inputs.allow_localhost else ""
    return f"""(version 1)
(allow default)

; Network: nothing, DNS included (mDNSResponder is reached over mach as well as a socket).
(deny network*)
{localhost}(deny mach-lookup {_services(DNS_SERVICES)})

; Writes: only the run's temp folder and a few devices. The worktree is read-only.
(deny file-write*)
(allow file-write* (subpath (param "RUN_TMP")) {" ".join(WRITABLE_DEVICES)})

; Reads under home: only what the run needs. Metadata (stat) only on the worktree's ancestors up to
; home and on pytest's config filenames in them, so pytest's upward search gets ENOENT, not EPERM.
(deny file-read* (subpath (param "HOME")))
{upward_rule}(allow file-read* (subpath (param "WORKTREE")) (subpath (param "VENV")) (subpath (param "RUN_TMP")){" " + pythons if pythons else ""})
(deny file-read* (subpath (param "WORKTREE_VAULT")) (subpath (param "WORKTREE_GIT")))
(allow file-read-metadata (subpath (param "WORKTREE_VAULT")) (subpath (param "WORKTREE_GIT")))

; The Keychain: securityd is reached over mach, not through files.
(deny mach-lookup {_services(KEYCHAIN_SERVICES)})
"""


def sandbox_argv(profile_file: str | os.PathLike[str], inputs: ProfileInputs, program: str | os.PathLike[str],
                 args: list[str]) -> list[str]:
    argv = ["/usr/bin/sandbox-exec", "-f", os.fspath(profile_file)]
    for key, value in params(inputs).items():
        argv += ["-D", f"{key}={value}"]
    return argv + ["--", os.fspath(program), *args]


def home() -> Path:
    return Path(real("~"))
