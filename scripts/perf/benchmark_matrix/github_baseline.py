"""GitHub Actions baseline discovery and safe artifact extraction."""

from __future__ import annotations

import json
import stat
import subprocess
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any

from apm_cli.utils.atomic_io import atomic_write_text

from .catalog import (
    BASELINE_ARTIFACT,
    BASELINE_RESULT_FILE,
    BASELINE_WORKFLOW,
    RESULT_SCHEMA_VERSION,
)
from .results import (
    BenchmarkReport,
    ResultFormatError,
    is_current_profile_report,
    load_report,
)

_MAX_ARCHIVE_ENTRIES = 1_000
_MAX_ARCHIVE_BYTES = 100 * 1024 * 1024


class BaselineError(RuntimeError):
    """Raised when an exact safe GitHub baseline cannot be obtained."""


class BaselineNotFoundError(BaselineError):
    """Raised when no compatible trusted full baseline exists yet."""


@dataclass(frozen=True)
class WorkflowRun:
    """Successful main-branch workflow run eligible for baseline selection."""

    database_id: int
    created_at: str
    head_sha: str
    event: str


@dataclass(frozen=True)
class Artifact:
    """One exact artifact attached to a workflow run."""

    artifact_id: int
    name: str
    expired: bool


GhRunner = Callable[..., subprocess.CompletedProcess[Any]]


def select_latest_earlier_successful_run(
    raw_runs: object,
    *,
    current_run_id: int | None = None,
    before: datetime | None = None,
) -> WorkflowRun:
    """Select the newest successful main run strictly before the current run."""
    return eligible_successful_runs(
        raw_runs,
        current_run_id=current_run_id,
        before=before,
    )[0]


def eligible_successful_runs(
    raw_runs: object,
    *,
    current_run_id: int | None = None,
    before: datetime | None = None,
) -> tuple[WorkflowRun, ...]:
    """Return eligible successful main runs ordered newest first."""
    if not isinstance(raw_runs, list):
        raise BaselineError("GitHub workflow run response must be a JSON array")
    candidates: list[tuple[datetime, WorkflowRun]] = []
    for index, raw_run in enumerate(raw_runs):
        if not isinstance(raw_run, dict):
            raise BaselineError(f"Workflow run at index {index} must be an object")
        try:
            database_id = raw_run["databaseId"]
            created_at = raw_run["createdAt"]
            conclusion = raw_run["conclusion"]
            head_branch = raw_run["headBranch"]
            head_sha = raw_run["headSha"]
            event = raw_run["event"]
        except KeyError as exc:
            raise BaselineError(f"Workflow run is missing field: {exc.args[0]}") from exc
        if (
            isinstance(database_id, bool)
            or not isinstance(database_id, int)
            or not isinstance(created_at, str)
            or not isinstance(conclusion, str)
            or not isinstance(head_branch, str)
            or not isinstance(head_sha, str)
            or not isinstance(event, str)
        ):
            raise BaselineError(f"Workflow run at index {index} has invalid field types")
        if (
            conclusion != "success"
            or head_branch != "main"
            or event not in {"schedule", "workflow_dispatch"}
        ):
            continue
        if len(head_sha) != 40 or any(
            character not in "0123456789abcdefABCDEF" for character in head_sha
        ):
            raise BaselineError(f"Workflow run at index {index} has an invalid head SHA")
        if current_run_id is not None and database_id >= current_run_id:
            continue
        created = _parse_github_timestamp(created_at)
        if before is not None and created >= before:
            continue
        candidates.append(
            (
                created,
                WorkflowRun(
                    database_id=database_id,
                    created_at=created_at,
                    head_sha=head_sha,
                    event=event,
                ),
            )
        )
    if not candidates:
        raise BaselineNotFoundError("No earlier trusted successful main workflow run was found")
    candidates.sort(key=lambda item: item[0], reverse=True)
    return tuple(run for _, run in candidates)


def select_exact_artifact(raw_artifacts: object, artifact_name: str) -> Artifact:
    """Select one non-expired artifact whose name exactly matches the catalog."""
    artifact = _optional_exact_artifact(raw_artifacts, artifact_name)
    if artifact is None:
        raise BaselineError(
            f"Expected exactly one non-expired artifact named {artifact_name!r}, found 0"
        )
    return artifact


def _optional_exact_artifact(raw_artifacts: object, artifact_name: str) -> Artifact | None:
    """Return one exact artifact, allowing a run to have no matching artifact."""
    if not isinstance(raw_artifacts, dict):
        raise BaselineError("GitHub artifact response must be a JSON object")
    artifacts = raw_artifacts.get("artifacts")
    if not isinstance(artifacts, list):
        raise BaselineError("GitHub artifact response must contain an artifacts array")
    matches: list[Artifact] = []
    for index, raw_artifact in enumerate(artifacts):
        if not isinstance(raw_artifact, dict):
            raise BaselineError(f"Artifact at index {index} must be an object")
        artifact_id = raw_artifact.get("id")
        name = raw_artifact.get("name")
        expired = raw_artifact.get("expired")
        if (
            isinstance(artifact_id, bool)
            or not isinstance(artifact_id, int)
            or not isinstance(name, str)
            or not isinstance(expired, bool)
        ):
            raise BaselineError(f"Artifact at index {index} has invalid field types")
        if name == artifact_name and not expired:
            matches.append(Artifact(artifact_id=artifact_id, name=name, expired=expired))
    if len(matches) > 1:
        raise BaselineError(
            f"Expected exactly one non-expired artifact named {artifact_name!r}, "
            f"found {len(matches)}"
        )
    return matches[0] if matches else None


def extract_zip_safely(archive_bytes: bytes, destination: Path) -> tuple[Path, ...]:
    """Extract regular ZIP members without traversal or symlink behavior."""
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise BaselineError(f"Refusing unsafe artifact destination: {destination}")
    try:
        archive = zipfile.ZipFile(BytesIO(archive_bytes))
    except zipfile.BadZipFile as exc:
        raise BaselineError("Downloaded artifact is not a valid ZIP archive") from exc
    with archive:
        members = archive.infolist()
        if len(members) > _MAX_ARCHIVE_ENTRIES:
            raise BaselineError("Artifact ZIP contains too many entries")
        total_size = sum(member.file_size for member in members)
        if total_size > _MAX_ARCHIVE_BYTES:
            raise BaselineError("Artifact ZIP exceeds the uncompressed size limit")
        validated = tuple(_validated_member(member, destination) for member in members)
        destination.mkdir(parents=True, exist_ok=True)
        extracted: list[Path] = []
        for member, target in zip(members, validated, strict=True):
            _assert_parent_chain_safe(destination, target.parent)
            if member.is_dir():
                _mkdir_parents_safe(destination, target)
                continue
            _mkdir_parents_safe(destination, target.parent)
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise BaselineError(f"Refusing unsafe existing artifact target: {target}")
            target.write_bytes(archive.read(member))
            extracted.append(target)
        return tuple(extracted)


def download_latest_baseline(
    destination: Path,
    *,
    repository: str,
    current_run_id: int | None = None,
    before: datetime | None = None,
    workflow: str = BASELINE_WORKFLOW,
    artifact_name: str = BASELINE_ARTIFACT,
    result_file: str = BASELINE_RESULT_FILE,
    gh_runner: GhRunner = subprocess.run,
) -> Path:
    """Download the exact artifact from the latest earlier successful main run."""
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise BaselineError(f"Refusing unsafe artifact destination: {destination}")
    output = destination / PurePosixPath(result_file)
    if output.is_symlink() or (output.exists() and not output.is_file()):
        raise BaselineError(f"Refusing unsafe existing baseline output: {output}")
    if output.exists():
        output.unlink()
    try:
        raw_runs = _run_gh_json(
            (
                "gh",
                "run",
                "list",
                "--repo",
                repository,
                "--workflow",
                workflow,
                "--branch",
                "main",
                "--status",
                "success",
                "--limit",
                "100",
                "--json",
                "databaseId,createdAt,conclusion,headBranch,headSha,event",
            ),
            gh_runner=gh_runner,
        )
    except BaselineError as exc:
        message = str(exc)
        if (
            "HTTP 404" in message
            and f"workflow {workflow} not found on the default branch" in message
        ):
            raise BaselineNotFoundError(
                f"Benchmark workflow {workflow!r} has no default-branch history yet"
            ) from exc
        raise
    runs = eligible_successful_runs(
        raw_runs,
        current_run_id=current_run_id,
        before=before,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    for run in runs:
        raw_artifacts = _run_gh_json(
            (
                "gh",
                "api",
                f"repos/{repository}/actions/runs/{run.database_id}/artifacts?per_page=100",
            ),
            gh_runner=gh_runner,
        )
        artifact = _optional_exact_artifact(raw_artifacts, artifact_name)
        if artifact is None:
            continue
        archive_bytes = _run_gh_bytes(
            (
                "gh",
                "api",
                f"repos/{repository}/actions/artifacts/{artifact.artifact_id}/zip",
            ),
            gh_runner=gh_runner,
        )
        with tempfile.TemporaryDirectory(
            prefix="apm-benchmark-baseline-",
            dir=destination.parent,
        ) as temp_dir:
            extraction_root = Path(temp_dir)
            extracted = extract_zip_safely(archive_bytes, extraction_root)
            expected = extraction_root / PurePosixPath(result_file)
            if expected not in extracted or not expected.is_file():
                raise BaselineError(
                    f"Artifact {artifact_name!r} does not contain exact result file {result_file!r}"
                )
            if _decoded_schema_version(expected) != RESULT_SCHEMA_VERSION:
                continue
            try:
                report = load_report(expected, require_current=False)
            except ResultFormatError as exc:
                raise BaselineError(
                    f"Artifact {artifact_name!r} contains malformed {result_file!r}"
                ) from exc
            if not _is_authoritative_full_baseline(report, run):
                continue
            output.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(output, expected.read_text(encoding="ascii"))
            return output
    raise BaselineNotFoundError(
        "No earlier successful main workflow run contained a compatible "
        f"non-expired {artifact_name!r} artifact with {result_file!r}"
    )


def _is_authoritative_full_baseline(
    report: BenchmarkReport,
    run: WorkflowRun,
) -> bool:
    """Return whether a validated report can serve as the full Linux baseline."""
    return (
        is_current_profile_report(report, "full")
        and report.platform.system == "linux"
        and report.platform.machine == "x86_64"
        and report.repository_revision.lower() == run.head_sha.lower()
    )


def _decoded_schema_version(path: Path) -> int:
    """Read the schema discriminator without treating old versions as corrupt."""
    try:
        raw = json.loads(path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BaselineError(f"Artifact result file is malformed: {path}") from exc
    if not isinstance(raw, dict):
        raise BaselineError("Artifact result document must be a JSON object")
    schema_version = raw.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise BaselineError("Artifact result schema_version must be an integer")
    return schema_version


def _run_gh_json(command: tuple[str, ...], *, gh_runner: GhRunner) -> object:
    completed = gh_runner(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if completed.returncode != 0 or not isinstance(completed.stdout, str):
        raise BaselineError(
            f"gh command failed: command={command!r}, "
            f"returncode={completed.returncode!r}, stderr={completed.stderr!r}"
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise BaselineError(f"gh command returned malformed JSON: {command!r}") from exc


def _run_gh_bytes(command: tuple[str, ...], *, gh_runner: GhRunner) -> bytes:
    completed = gh_runner(
        command,
        capture_output=True,
        text=False,
        check=False,
        timeout=120,
    )
    if completed.returncode != 0 or not isinstance(completed.stdout, bytes):
        raise BaselineError(
            f"gh artifact download failed: command={command!r}, "
            f"returncode={completed.returncode!r}, stderr={completed.stderr!r}"
        )
    return completed.stdout


def _validated_member(member: zipfile.ZipInfo, destination: Path) -> Path:
    name = member.filename
    if not name or "\x00" in name or "\\" in name:
        raise BaselineError(f"Artifact ZIP contains an unsafe member name: {name!r}")
    relative = PurePosixPath(name)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise BaselineError(f"Artifact ZIP contains path traversal: {name!r}")
    if relative.parts and ":" in relative.parts[0]:
        raise BaselineError(f"Artifact ZIP contains a drive-qualified path: {name!r}")
    mode = member.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise BaselineError(f"Artifact ZIP contains a symbolic link: {name!r}")
    target = destination.joinpath(*relative.parts)
    destination_resolved = destination.resolve()
    target_resolved = target.resolve()
    if not target_resolved.is_relative_to(destination_resolved):
        raise BaselineError(f"Artifact ZIP member escapes destination: {name!r}")
    return target


def _mkdir_parents_safe(destination: Path, target: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    relative = target.relative_to(destination)
    current = destination
    for part in relative.parts:
        current /= part
        if current.exists() or current.is_symlink():
            if current.is_symlink() or not current.is_dir():
                raise BaselineError(f"Refusing unsafe artifact directory: {current}")
            continue
        current.mkdir()


def _assert_parent_chain_safe(destination: Path, parent: Path) -> None:
    relative = parent.relative_to(destination)
    current = destination
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise BaselineError(f"Refusing symlinked artifact parent: {current}")
        if current.exists() and not current.is_dir():
            raise BaselineError(f"Refusing non-directory artifact parent: {current}")


def _parse_github_timestamp(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BaselineError(f"Invalid GitHub timestamp: {value!r}") from exc
