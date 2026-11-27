"""Sandbox refusals. None of them ever leads to running a command unsandboxed."""


class SandboxError(Exception):
    """The command wasn't run."""


class SandboxConfigError(SandboxError):
    """daedalus.toml is missing or invalid."""


class SandboxUnavailable(SandboxError):
    """sandbox-exec is missing or a profile didn't load: fail closed."""


class VenvError(SandboxError):
    """The project venv couldn't be built from its frozen uv.lock."""


class RunnerError(SandboxError):
    """The run's own machinery failed (for example, a process outlived the kill)."""
