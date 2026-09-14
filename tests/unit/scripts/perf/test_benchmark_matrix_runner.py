"""Unit tests for the benchmark subprocess boundary."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import ClassVar

import pytest

from scripts.perf.benchmark_matrix.catalog import get_scenario
from scripts.perf.benchmark_matrix.runner import (
    BenchmarkCommandError,
    BenchmarkCorrectnessError,
    BenchmarkRunner,
    BenchmarkTimeoutError,
    MalformedCommandResultError,
    sanitize_environment,
    source_cli_command,
)

pytestmark = pytest.mark.unit


class _Fixture:
    cwd = Path("/tmp/benchmark-project")
    environment: ClassVar[dict[str, str]] = {
        "PATH": "/usr/bin",
        "GITHUB_TOKEN": "secret",
        "https_proxy": "http://proxy.invalid",
        "BENCHMARK_VISIBLE": "yes",
    }

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.validated = False

    def validate(self, *, stdout: str, stderr: str) -> None:
        self.validated = True
        if self.error is not None:
            raise self.error


def test_source_cli_command_is_exact_and_rooted() -> None:
    """The harness always executes the current checkout through frozen uv."""
    command = source_cli_command(Path("/repo/../repo"))
    assert command == (
        "uv",
        "run",
        "--frozen",
        "--no-sync",
        "--project",
        "/repo",
        "apm",
    )


def test_sanitize_environment_removes_credentials_and_network_routing() -> None:
    """Child processes retain ordinary inputs but not ambient authority."""
    sanitized = sanitize_environment(_Fixture.environment)
    assert "GITHUB_TOKEN" not in sanitized
    assert "https_proxy" not in sanitized
    assert sanitized["BENCHMARK_VISIBLE"] == "yes"
    assert sanitized["GIT_TERMINAL_PROMPT"] == "0"
    assert sanitized["PYTHONHASHSEED"] == "0"


def test_run_sample_times_exact_command_and_validates() -> None:
    """Timing brackets only the subprocess and correctness validation follows it."""
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def fake_run(command: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="apm 1.0\n", stderr="")

    clock = iter((1_000, 4_500))
    fixture = _Fixture()
    runner = BenchmarkRunner(
        Path("/repo"),
        run_command=fake_run,
        clock_ns=lambda: next(clock),
    )
    result = runner.run_sample(get_scenario("startup.version"), fixture)

    assert result.elapsed_ns == 3_500
    assert result.command[-1] == "--version"
    assert fixture.validated is True
    assert calls[0][1]["timeout"] == 30.0
    child_env = calls[0][1]["env"]
    assert isinstance(child_env, dict)
    assert "GITHUB_TOKEN" not in child_env


def test_run_sample_hard_fails_timeout() -> None:
    """A subprocess timeout is never converted into a timing sample."""

    def fake_run(command: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    runner = BenchmarkRunner(Path("/repo"), run_command=fake_run)
    with pytest.raises(BenchmarkTimeoutError, match=r"startup\.version"):
        runner.run_sample(get_scenario("startup.version"), _Fixture())


def test_run_sample_hard_fails_command_error() -> None:
    """A nonzero command result retains captured diagnostic evidence."""

    def fake_run(command: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 2, stdout="out", stderr="bad")

    runner = BenchmarkRunner(Path("/repo"), run_command=fake_run, clock_ns=iter((1, 2)).__next__)
    with pytest.raises(BenchmarkCommandError, match="returncode=2"):
        runner.run_sample(get_scenario("startup.version"), _Fixture())


def test_run_sample_hard_fails_malformed_result() -> None:
    """Mocks and adapters cannot smuggle invalid evidence into reports."""

    def fake_run(command: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout=b"bytes", stderr="")  # type: ignore[arg-type]

    runner = BenchmarkRunner(Path("/repo"), run_command=fake_run, clock_ns=iter((1, 2)).__next__)
    with pytest.raises(MalformedCommandResultError, match="malformed evidence"):
        runner.run_sample(get_scenario("startup.version"), _Fixture())


def test_run_sample_hard_fails_correctness_error() -> None:
    """A successful exit without correct artifacts remains a failed run."""

    def fake_run(command: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    runner = BenchmarkRunner(Path("/repo"), run_command=fake_run, clock_ns=iter((1, 2)).__next__)
    with pytest.raises(BenchmarkCorrectnessError, match="missing output"):
        runner.run_sample(
            get_scenario("startup.version"),
            _Fixture(AssertionError("missing output")),
        )
