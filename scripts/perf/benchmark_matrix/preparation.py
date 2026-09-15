"""Separate benchmark harness preparation timing report."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from apm_cli.utils.atomic_io import atomic_write_text

from .catalog import get_profile
from .results import PlatformIdentity

PREPARATION_SCHEMA_VERSION = 1


class PreparationFormatError(ValueError):
    """Raised when a preparation report violates its independent schema."""


@dataclass(frozen=True)
class PreparationSample:
    """Untimed fixture preparation duration for one benchmark sample."""

    scenario_id: str
    repetition: int
    elapsed_ns: int


@dataclass(frozen=True)
class PreparationReport:
    """Harness preparation throughput, separate from product timing results."""

    schema_version: int
    profile: str
    generated_at: str
    repository_revision: str
    platform: PlatformIdentity
    samples: tuple[PreparationSample, ...]
    total_elapsed_ns: int


def make_preparation_report(
    *,
    profile_id: str,
    generated_at: str,
    repository_revision: str,
    platform: PlatformIdentity,
    samples: tuple[PreparationSample, ...],
) -> PreparationReport:
    """Build and validate one complete profile preparation report."""
    report = PreparationReport(
        schema_version=PREPARATION_SCHEMA_VERSION,
        profile=profile_id,
        generated_at=generated_at,
        repository_revision=repository_revision,
        platform=platform,
        samples=samples,
        total_elapsed_ns=sum(sample.elapsed_ns for sample in samples),
    )
    _validate_report(report)
    return report


def write_preparation_report(path: Path, report: PreparationReport) -> None:
    """Atomically write deterministic printable-ASCII preparation JSON."""
    _validate_report(report)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path,
        json.dumps(
            _report_to_dict(report),
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )


def load_preparation_report(path: Path) -> PreparationReport:
    """Load and validate one preparation report."""
    try:
        raw = json.loads(path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PreparationFormatError(f"Cannot read preparation report {path}: {exc}") from exc
    document = _mapping(raw, "preparation report")
    _exact_keys(
        document,
        {
            "schema_version",
            "profile",
            "generated_at",
            "repository_revision",
            "platform",
            "samples",
            "total_elapsed_ns",
        },
        "preparation report",
    )
    platform_raw = _mapping(document["platform"], "platform")
    _exact_keys(
        platform_raw,
        {
            "system",
            "machine",
            "python_implementation",
            "python_version",
        },
        "platform",
    )
    samples_raw = document["samples"]
    if not isinstance(samples_raw, list):
        raise PreparationFormatError("samples must be a list")
    samples = tuple(_sample_from_dict(value, index) for index, value in enumerate(samples_raw))
    report = PreparationReport(
        schema_version=_integer(document["schema_version"], "schema_version"),
        profile=_string(document["profile"], "profile"),
        generated_at=_string(document["generated_at"], "generated_at"),
        repository_revision=_string(
            document["repository_revision"],
            "repository_revision",
        ),
        platform=PlatformIdentity(
            system=_string(platform_raw["system"], "platform.system"),
            machine=_string(platform_raw["machine"], "platform.machine"),
            python_implementation=_string(
                platform_raw["python_implementation"],
                "platform.python_implementation",
            ),
            python_version=_string(
                platform_raw["python_version"],
                "platform.python_version",
            ),
        ),
        samples=samples,
        total_elapsed_ns=_integer(
            document["total_elapsed_ns"],
            "total_elapsed_ns",
        ),
    )
    _validate_report(report)
    return report


def _validate_report(report: PreparationReport) -> None:
    if report.schema_version != PREPARATION_SCHEMA_VERSION:
        raise PreparationFormatError(
            f"Unsupported preparation schema version: {report.schema_version}"
        )
    profile = get_profile(report.profile)
    if not report.generated_at:
        raise PreparationFormatError("generated_at must not be empty")
    if len(report.repository_revision) != 40:
        raise PreparationFormatError("repository_revision must be a 40-character Git SHA")
    if not all(
        (
            report.platform.system,
            report.platform.machine,
            report.platform.python_implementation,
            report.platform.python_version,
        )
    ):
        raise PreparationFormatError("platform fields must not be empty")
    expected_samples = tuple(
        (scenario_id, repetition)
        for scenario_id in profile.scenario_ids
        for repetition in range(profile.repetitions)
    )
    actual_samples = tuple((sample.scenario_id, sample.repetition) for sample in report.samples)
    if actual_samples != expected_samples:
        raise PreparationFormatError("preparation samples must match the complete ordered profile")
    if any(
        isinstance(sample.elapsed_ns, bool)
        or not isinstance(sample.elapsed_ns, int)
        or sample.elapsed_ns <= 0
        for sample in report.samples
    ):
        raise PreparationFormatError("preparation elapsed_ns values must be positive integers")
    expected_total = sum(sample.elapsed_ns for sample in report.samples)
    if report.total_elapsed_ns != expected_total:
        raise PreparationFormatError("total_elapsed_ns must equal the sum of preparation samples")


def _report_to_dict(report: PreparationReport) -> dict[str, object]:
    return {
        "schema_version": report.schema_version,
        "profile": report.profile,
        "generated_at": report.generated_at,
        "repository_revision": report.repository_revision,
        "platform": {
            "system": report.platform.system,
            "machine": report.platform.machine,
            "python_implementation": report.platform.python_implementation,
            "python_version": report.platform.python_version,
        },
        "samples": [
            {
                "scenario_id": sample.scenario_id,
                "repetition": sample.repetition,
                "elapsed_ns": sample.elapsed_ns,
            }
            for sample in report.samples
        ],
        "total_elapsed_ns": report.total_elapsed_ns,
    }


def _sample_from_dict(value: object, index: int) -> PreparationSample:
    sample = _mapping(value, f"samples[{index}]")
    _exact_keys(
        sample,
        {"scenario_id", "repetition", "elapsed_ns"},
        f"samples[{index}]",
    )
    return PreparationSample(
        scenario_id=_string(sample["scenario_id"], f"samples[{index}].scenario_id"),
        repetition=_integer(sample["repetition"], f"samples[{index}].repetition"),
        elapsed_ns=_integer(sample["elapsed_ns"], f"samples[{index}].elapsed_ns"),
    )


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise PreparationFormatError(f"{label} must be an object with string keys")
    return value


def _exact_keys(value: dict[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise PreparationFormatError(f"{label} has unexpected or missing fields")


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise PreparationFormatError(f"{label} must be a non-empty string")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PreparationFormatError(f"{label} must be an integer")
    return value
