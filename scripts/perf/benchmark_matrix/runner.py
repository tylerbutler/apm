"""Timed source-CLI subprocess boundary for benchmark samples."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .catalog import BenchmarkScenario

_SCRUBBED_ENVIRONMENT_NAMES = frozenset(
    {
        "ADO_APM_PAT",
        "ALL_PROXY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AZURE_ACCESS_TOKEN",
        "COPILOT_GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GH_TOKEN",
        "GITHUB_APM_PAT",
        "GITHUB_COPILOT_PAT",
        "GITHUB_ENTERPRISE_TOKEN",
        "GITHUB_MODELS_KEY",
        "GITHUB_PERSONAL_ACCESS_TOKEN",
        "GITHUB_TOKEN",
        "GITLAB_APM_PAT",
        "GITLAB_TOKEN",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "SSH_AGENT_PID",
        "SSH_ASKPASS",
        "SSH_AUTH_SOCK",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)
_SCRUBBED_ENVIRONMENT_PREFIXES = (
    "APM_REGISTRY_PASS_",
    "APM_REGISTRY_TOKEN_",
    "APM_REGISTRY_USER_",
    "GITHUB_APM_PAT_",
)


class PreparedFixtureProtocol(Protocol):
    """Minimum fixture surface consumed by the subprocess runner."""

    cwd: Path
    environment: Mapping[str, str]

    def validate(self, *, stdout: str, stderr: str) -> None:
        """Validate command correctness after a successful process exit."""


class BenchmarkRunError(RuntimeError):
    """Base class for hard benchmark execution failures."""


class BenchmarkTimeoutError(BenchmarkRunError):
    """Raised when a measured command exceeds its catalog timeout."""


class BenchmarkCommandError(BenchmarkRunError):
    """Raised when a measured command exits unsuccessfully."""


class MalformedCommandResultError(BenchmarkRunError):
    """Raised when a subprocess adapter returns an invalid result shape."""


class BenchmarkCorrectnessError(BenchmarkRunError):
    """Raised when a successful command produces incorrect state."""


@dataclass(frozen=True)
class TimedCommandResult:
    """Validated evidence for one measured subprocess."""

    command: tuple[str, ...]
    cwd: Path
    returncode: int
    stdout: str
    stderr: str
    elapsed_ns: int


RunCallable = Callable[..., subprocess.CompletedProcess[str]]


def source_cli_command(repository_root: Path) -> tuple[str, ...]:
    """Return the canonical current-worktree APM subprocess command."""
    root = repository_root.resolve()
    return (
        "uv",
        "run",
        "--frozen",
        "--no-sync",
        "--project",
        str(root),
        "apm",
    )


def sanitize_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Remove inherited credentials and network routing from a child environment."""
    sanitized: dict[str, str] = {}
    for name, value in environment.items():
        normalized = name.upper()
        if name in _SCRUBBED_ENVIRONMENT_NAMES or normalized in _SCRUBBED_ENVIRONMENT_NAMES:
            continue
        if normalized.startswith(_SCRUBBED_ENVIRONMENT_PREFIXES):
            continue
        sanitized[name] = value
    sanitized["CI"] = "1"
    sanitized["GIT_TERMINAL_PROMPT"] = "0"
    sanitized["NO_COLOR"] = "1"
    sanitized["PYTHONHASHSEED"] = "0"
    sanitized["UV_NO_PROGRESS"] = "1"
    return sanitized


class BenchmarkRunner:
    """Measure one prepared scenario through the isolated source CLI."""

    def __init__(
        self,
        repository_root: Path,
        *,
        run_command: RunCallable = subprocess.run,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        """Create a runner with injectable process and clock boundaries."""
        self._base_command = source_cli_command(repository_root)
        self._run_command = run_command
        self._clock_ns = clock_ns

    def run_sample(
        self,
        scenario: BenchmarkScenario,
        fixture: PreparedFixtureProtocol,
    ) -> TimedCommandResult:
        """Execute, time, and validate one catalog scenario sample."""
        command = (*self._base_command, *scenario.command)
        environment = sanitize_environment(fixture.environment)
        started_ns = self._clock_ns()
        try:
            completed = self._run_command(
                command,
                cwd=fixture.cwd,
                env=environment,
                capture_output=True,
                text=True,
                timeout=scenario.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise BenchmarkTimeoutError(
                "Benchmark command timed out\n"
                f"scenario={scenario.id}\n"
                f"timeout_seconds={scenario.timeout_seconds}\n"
                f"command={command!r}\n"
                f"cwd={fixture.cwd}"
            ) from exc
        finished_ns = self._clock_ns()

        returncode = getattr(completed, "returncode", None)
        stdout = getattr(completed, "stdout", None)
        stderr = getattr(completed, "stderr", None)
        elapsed_ns = finished_ns - started_ns
        if (
            not isinstance(returncode, int)
            or isinstance(returncode, bool)
            or not isinstance(stdout, str)
            or not isinstance(stderr, str)
            or elapsed_ns <= 0
        ):
            raise MalformedCommandResultError(
                "Benchmark subprocess returned malformed evidence\n"
                f"scenario={scenario.id}\n"
                f"returncode={returncode!r}\n"
                f"stdout_type={type(stdout).__name__}\n"
                f"stderr_type={type(stderr).__name__}\n"
                f"elapsed_ns={elapsed_ns}"
            )
        if returncode != 0:
            raise BenchmarkCommandError(
                "Benchmark command failed\n"
                f"scenario={scenario.id}\n"
                f"command={command!r}\n"
                f"cwd={fixture.cwd}\n"
                f"returncode={returncode}\n"
                f"stdout={stdout!r}\n"
                f"stderr={stderr!r}"
            )
        try:
            fixture.validate(stdout=stdout, stderr=stderr)
        except (AssertionError, OSError, ValueError) as exc:
            raise BenchmarkCorrectnessError(
                "Benchmark correctness validation failed\n"
                f"scenario={scenario.id}\n"
                f"command={command!r}\n"
                f"cwd={fixture.cwd}\n"
                f"cause={exc}"
            ) from exc
        return TimedCommandResult(
            command=command,
            cwd=fixture.cwd,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            elapsed_ns=elapsed_ns,
        )
