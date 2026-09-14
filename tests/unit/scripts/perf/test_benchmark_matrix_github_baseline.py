"""Component tests for mocked GitHub baseline selection and extraction."""

from __future__ import annotations

import io
import json
import stat
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.perf.benchmark_matrix.github_baseline import (
    BaselineError,
    BaselineNotFoundError,
    download_latest_baseline,
    eligible_successful_runs,
    extract_zip_safely,
    select_exact_artifact,
    select_latest_earlier_successful_run,
)
from scripts.perf.benchmark_matrix.results import write_report
from tests.utils.benchmark_matrix_fixture import build_benchmark_report

pytestmark = pytest.mark.component


def _zip_bytes(name: str, content: bytes, *, mode: int | None = None) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        info = zipfile.ZipInfo(name)
        if mode is not None:
            info.external_attr = mode << 16
        archive.writestr(info, content)
    return output.getvalue()


def _full_report_bytes(tmp_path: Path, *, revision: str = "a" * 40) -> bytes:
    report = build_benchmark_report(
        "full",
        default_sample_ns=1,
        repository_revision=revision,
    )
    path = tmp_path / "full-results.json"
    write_report(path, report)
    return path.read_bytes()


def test_selects_latest_earlier_successful_main_run() -> None:
    """Selection ignores current, failed, and non-main runs."""
    runs = [
        {
            "databaseId": 5,
            "createdAt": "2026-09-14T12:00:00Z",
            "conclusion": "success",
            "headBranch": "main",
            "headSha": "e" * 40,
            "event": "schedule",
        },
        {
            "databaseId": 4,
            "createdAt": "2026-09-13T12:00:00Z",
            "conclusion": "success",
            "headBranch": "main",
            "headSha": "d" * 40,
            "event": "workflow_dispatch",
        },
        {
            "databaseId": 3,
            "createdAt": "2026-09-12T12:00:00Z",
            "conclusion": "success",
            "headBranch": "main",
            "headSha": "c" * 40,
            "event": "schedule",
        },
        {
            "databaseId": 2,
            "createdAt": "2026-09-11T12:00:00Z",
            "conclusion": "failure",
            "headBranch": "main",
            "headSha": "b" * 40,
            "event": "schedule",
        },
    ]
    selected = select_latest_earlier_successful_run(
        runs,
        current_run_id=4,
        before=datetime(2026, 9, 13, tzinfo=timezone.utc),
    )
    assert selected.database_id == 3
    assert [run.database_id for run in eligible_successful_runs(runs)] == [5, 4, 3]


def test_exact_artifact_rejects_missing_duplicate_and_expired() -> None:
    """Artifact selection never falls back to a similarly named payload."""
    with pytest.raises(BaselineError, match="exactly one"):
        select_exact_artifact(
            {
                "artifacts": [
                    {"id": 1, "name": "benchmark-matrix-linux", "expired": False},
                    {
                        "id": 2,
                        "name": "benchmark-full-main",
                        "expired": True,
                    },
                ]
            },
            "benchmark-full-main",
        )


def test_missing_default_branch_workflow_is_bootstrap_not_found(tmp_path: Path) -> None:
    """The first PR can bootstrap before benchmark.yml exists on main."""

    def missing_workflow(
        command: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr=(
                "HTTP 404: workflow benchmark.yml not found on the default branch "
                "(https://api.github.com/repos/microsoft/apm/actions/workflows/benchmark.yml)"
            ),
        )

    with pytest.raises(BaselineNotFoundError, match="no default-branch history"):
        download_latest_baseline(
            tmp_path / "baseline",
            repository="microsoft/apm",
            gh_runner=missing_workflow,
        )


@pytest.mark.parametrize("member", ["../escape.json", "/absolute.json", "C:/drive.json"])
def test_safe_extract_rejects_path_traversal(tmp_path: Path, member: str) -> None:
    """Untrusted artifact names cannot escape the destination."""
    with pytest.raises(BaselineError):
        extract_zip_safely(_zip_bytes(member, b"{}"), tmp_path / "out")


def test_safe_extract_rejects_symlink_members(tmp_path: Path) -> None:
    """ZIP metadata cannot create or follow an artifact-controlled symlink."""
    symlink_mode = stat.S_IFLNK | 0o777
    with pytest.raises(BaselineError, match="symbolic link"):
        extract_zip_safely(
            _zip_bytes("result-link", b"target", mode=symlink_mode),
            tmp_path / "out",
        )


def test_safe_extract_rejects_preexisting_symlink_target(tmp_path: Path) -> None:
    """Extraction never follows an existing final-component symlink."""
    destination = tmp_path / "out"
    destination.mkdir()
    target = tmp_path / "outside.json"
    (destination / "results.json").symlink_to(target)

    with pytest.raises(BaselineError, match=r"escapes destination|unsafe existing"):
        extract_zip_safely(
            _zip_bytes("results.json", b"{}"),
            destination,
        )
    assert not target.exists()


def test_download_latest_baseline_uses_mocked_gh_and_exact_file(tmp_path: Path) -> None:
    """The network boundary is fully mockable and uses exact run/artifact IDs."""
    report_bytes = _full_report_bytes(tmp_path)
    archive = _zip_bytes("results.json", report_bytes)
    responses: list[subprocess.CompletedProcess[object]] = [
        subprocess.CompletedProcess(
            (),
            0,
            stdout=json.dumps(
                [
                    {
                        "databaseId": 42,
                        "createdAt": "2026-09-12T12:00:00Z",
                        "conclusion": "success",
                        "headBranch": "main",
                        "headSha": "a" * 40,
                        "event": "schedule",
                    }
                ]
            ),
            stderr="",
        ),
        subprocess.CompletedProcess(
            (),
            0,
            stdout=json.dumps(
                {
                    "artifacts": [
                        {
                            "id": 99,
                            "name": "benchmark-full-main",
                            "expired": False,
                        }
                    ]
                }
            ),
            stderr="",
        ),
        subprocess.CompletedProcess((), 0, stdout=archive, stderr=b""),
    ]
    commands: list[tuple[str, ...]] = []

    def fake_gh(command: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[object]:
        commands.append(command)
        return responses.pop(0)

    result = download_latest_baseline(
        tmp_path / "baseline",
        repository="microsoft/apm",
        gh_runner=fake_gh,
    )
    assert result.name == "results.json"
    assert result.read_bytes() == report_bytes
    assert commands[0][-1].endswith(",event")
    assert "actions/runs/42/artifacts?per_page=100" in commands[1][-1]
    assert "actions/artifacts/99/zip" in commands[2][-1]


def test_download_scans_past_successful_run_without_full_artifact(tmp_path: Path) -> None:
    """A newer live-only run cannot hide the latest usable full baseline."""
    report_bytes = _full_report_bytes(tmp_path)
    archive = _zip_bytes("results.json", report_bytes)
    responses: list[subprocess.CompletedProcess[object]] = [
        subprocess.CompletedProcess(
            (),
            0,
            stdout=json.dumps(
                [
                    {
                        "databaseId": 50,
                        "createdAt": "2026-09-13T12:00:00Z",
                        "conclusion": "success",
                        "headBranch": "main",
                        "headSha": "b" * 40,
                        "event": "workflow_dispatch",
                    },
                    {
                        "databaseId": 40,
                        "createdAt": "2026-09-12T12:00:00Z",
                        "conclusion": "success",
                        "headBranch": "main",
                        "headSha": "a" * 40,
                        "event": "schedule",
                    },
                ]
            ),
            stderr="",
        ),
        subprocess.CompletedProcess(
            (),
            0,
            stdout=json.dumps(
                {"artifacts": [{"id": 501, "name": "benchmark-live", "expired": False}]}
            ),
            stderr="",
        ),
        subprocess.CompletedProcess(
            (),
            0,
            stdout=json.dumps(
                {"artifacts": [{"id": 401, "name": "benchmark-full-main", "expired": False}]}
            ),
            stderr="",
        ),
        subprocess.CompletedProcess((), 0, stdout=archive, stderr=b""),
    ]
    commands: list[tuple[str, ...]] = []

    def fake_gh(command: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[object]:
        del kwargs
        commands.append(command)
        return responses.pop(0)

    result = download_latest_baseline(
        tmp_path / "baseline",
        repository="microsoft/apm",
        current_run_id=60,
        gh_runner=fake_gh,
    )

    assert result.read_bytes() == report_bytes
    assert "actions/runs/50/artifacts?per_page=100" in commands[1][-1]
    assert "actions/runs/40/artifacts?per_page=100" in commands[2][-1]
    assert "actions/artifacts/401/zip" in commands[3][-1]


def test_baseline_ignores_untrusted_events_and_revision_mismatch(tmp_path: Path) -> None:
    """Only trusted events whose report SHA matches the run can supply a baseline."""
    mismatched_archive = _zip_bytes(
        "results.json",
        _full_report_bytes(tmp_path, revision="c" * 40),
    )
    trusted_archive = _zip_bytes(
        "results.json",
        _full_report_bytes(tmp_path, revision="a" * 40),
    )
    responses: list[subprocess.CompletedProcess[object]] = [
        subprocess.CompletedProcess(
            (),
            0,
            stdout=json.dumps(
                [
                    {
                        "databaseId": 60,
                        "createdAt": "2026-09-14T12:00:00Z",
                        "conclusion": "success",
                        "headBranch": "main",
                        "headSha": "d" * 40,
                        "event": "push",
                    },
                    {
                        "databaseId": 50,
                        "createdAt": "2026-09-13T12:00:00Z",
                        "conclusion": "success",
                        "headBranch": "main",
                        "headSha": "b" * 40,
                        "event": "schedule",
                    },
                    {
                        "databaseId": 40,
                        "createdAt": "2026-09-12T12:00:00Z",
                        "conclusion": "success",
                        "headBranch": "main",
                        "headSha": "a" * 40,
                        "event": "workflow_dispatch",
                    },
                ]
            ),
            stderr="",
        ),
        subprocess.CompletedProcess(
            (),
            0,
            stdout=json.dumps(
                {"artifacts": [{"id": 501, "name": "benchmark-full-main", "expired": False}]}
            ),
            stderr="",
        ),
        subprocess.CompletedProcess((), 0, stdout=mismatched_archive, stderr=b""),
        subprocess.CompletedProcess(
            (),
            0,
            stdout=json.dumps(
                {"artifacts": [{"id": 401, "name": "benchmark-full-main", "expired": False}]}
            ),
            stderr="",
        ),
        subprocess.CompletedProcess((), 0, stdout=trusted_archive, stderr=b""),
    ]

    def fake_gh(
        command: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[object]:
        del command, kwargs
        return responses.pop(0)

    result = download_latest_baseline(
        tmp_path / "baseline",
        repository="microsoft/apm",
        gh_runner=fake_gh,
    )
    assert result.read_bytes() == _full_report_bytes(tmp_path, revision="a" * 40)


def test_no_exact_compatible_artifact_is_typed_absence(tmp_path: Path) -> None:
    """Missing baseline artifacts are distinct from malformed GitHub responses."""
    responses: list[subprocess.CompletedProcess[object]] = [
        subprocess.CompletedProcess(
            (),
            0,
            stdout=json.dumps(
                [
                    {
                        "databaseId": 40,
                        "createdAt": "2026-09-12T12:00:00Z",
                        "conclusion": "success",
                        "headBranch": "main",
                        "headSha": "a" * 40,
                        "event": "schedule",
                    }
                ]
            ),
            stderr="",
        ),
        subprocess.CompletedProcess(
            (),
            0,
            stdout=json.dumps(
                {"artifacts": [{"id": 1, "name": "benchmark-live", "expired": False}]}
            ),
            stderr="",
        ),
    ]

    def fake_gh(
        command: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[object]:
        del command, kwargs
        return responses.pop(0)

    destination = tmp_path / "baseline"
    destination.mkdir()
    stale = destination / "results.json"
    stale.write_text("stale\n", encoding="ascii")
    with pytest.raises(BaselineNotFoundError):
        download_latest_baseline(
            destination,
            repository="microsoft/apm",
            gh_runner=fake_gh,
        )
    assert not stale.exists()


def test_malformed_run_response_remains_hard_failure(tmp_path: Path) -> None:
    """Allow-missing callers cannot reinterpret malformed provenance as absence."""

    def fake_gh(
        command: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[object]:
        del command, kwargs
        return subprocess.CompletedProcess((), 0, stdout='{"bad": true}', stderr="")

    with pytest.raises(BaselineError, match="JSON array"):
        download_latest_baseline(
            tmp_path / "baseline",
            repository="microsoft/apm",
            gh_runner=fake_gh,
        )


@pytest.mark.parametrize("mutation", ["truncated", "old-schema"])
def test_incompatible_full_artifact_is_typed_absence(
    tmp_path: Path,
    mutation: str,
) -> None:
    """Catalog drift cannot be promoted, but it remains a bootstrap condition."""
    raw = json.loads(_full_report_bytes(tmp_path).decode("ascii"))
    if mutation == "truncated":
        raw["scenarios"] = raw["scenarios"][:1]
    else:
        raw["schema_version"] = 1
    archive = _zip_bytes(
        "results.json",
        (json.dumps(raw, ensure_ascii=True) + "\n").encode("ascii"),
    )
    responses: list[subprocess.CompletedProcess[object]] = [
        subprocess.CompletedProcess(
            (),
            0,
            stdout=json.dumps(
                [
                    {
                        "databaseId": 40,
                        "createdAt": "2026-09-12T12:00:00Z",
                        "conclusion": "success",
                        "headBranch": "main",
                        "headSha": "a" * 40,
                        "event": "schedule",
                    }
                ]
            ),
            stderr="",
        ),
        subprocess.CompletedProcess(
            (),
            0,
            stdout=json.dumps(
                {"artifacts": [{"id": 401, "name": "benchmark-full-main", "expired": False}]}
            ),
            stderr="",
        ),
        subprocess.CompletedProcess((), 0, stdout=archive, stderr=b""),
    ]

    def fake_gh(
        command: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[object]:
        del command, kwargs
        return responses.pop(0)

    with pytest.raises(BaselineNotFoundError):
        download_latest_baseline(
            tmp_path / "baseline",
            repository="microsoft/apm",
            gh_runner=fake_gh,
        )


def test_corrupt_exact_artifact_remains_hard_failure(tmp_path: Path) -> None:
    """An exact artifact with invalid result JSON is not safe bootstrap absence."""
    archive = _zip_bytes("results.json", b"{\n")
    responses: list[subprocess.CompletedProcess[object]] = [
        subprocess.CompletedProcess(
            (),
            0,
            stdout=json.dumps(
                [
                    {
                        "databaseId": 40,
                        "createdAt": "2026-09-12T12:00:00Z",
                        "conclusion": "success",
                        "headBranch": "main",
                        "headSha": "a" * 40,
                        "event": "schedule",
                    }
                ]
            ),
            stderr="",
        ),
        subprocess.CompletedProcess(
            (),
            0,
            stdout=json.dumps(
                {"artifacts": [{"id": 401, "name": "benchmark-full-main", "expired": False}]}
            ),
            stderr="",
        ),
        subprocess.CompletedProcess((), 0, stdout=archive, stderr=b""),
    ]

    def fake_gh(
        command: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[object]:
        del command, kwargs
        return responses.pop(0)

    with pytest.raises(BaselineError, match="malformed"):
        download_latest_baseline(
            tmp_path / "baseline",
            repository="microsoft/apm",
            gh_runner=fake_gh,
        )
