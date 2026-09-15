"""Tests for the independent benchmark preparation timing report."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.perf.benchmark_matrix.catalog import RESULT_SCHEMA_VERSION, get_profile
from scripts.perf.benchmark_matrix.preparation import (
    PREPARATION_SCHEMA_VERSION,
    PreparationFormatError,
    PreparationReport,
    PreparationSample,
    load_preparation_report,
    make_preparation_report,
    write_preparation_report,
)
from tests.utils.benchmark_matrix_fixture import DEFAULT_PLATFORM, build_benchmark_report

pytestmark = pytest.mark.component


def _samples(profile_id: str = "smoke") -> tuple[PreparationSample, ...]:
    profile = get_profile(profile_id)
    return tuple(
        PreparationSample(
            scenario_id=scenario_id,
            repetition=repetition,
            elapsed_ns=1_000 + index,
        )
        for index, (scenario_id, repetition) in enumerate(
            (scenario_id, repetition)
            for scenario_id in profile.scenario_ids
            for repetition in range(profile.repetitions)
        )
    )


def _report(profile_id: str = "smoke") -> PreparationReport:
    return make_preparation_report(
        profile_id=profile_id,
        generated_at="2026-09-14T00:00:00Z",
        repository_revision="a" * 40,
        platform=DEFAULT_PLATFORM,
        samples=_samples(profile_id),
    )


@pytest.mark.parametrize("profile_id", ["smoke", "full", "live"])
@pytest.mark.windows_compat
def test_preparation_report_is_ascii_atomic_and_round_trips(
    tmp_path: Path,
    profile_id: str,
) -> None:
    """Preparation artifacts are deterministic and independent of platform newlines."""
    report = _report(profile_id)
    path = tmp_path / "nested" / "preparation.json"
    write_preparation_report(path, report)

    payload = path.read_bytes()
    assert payload.endswith(b"\n")
    assert b"\r\n" not in payload
    assert payload.isascii()
    assert load_preparation_report(path) == report
    assert report.total_elapsed_ns == sum(sample.elapsed_ns for sample in report.samples)


def test_preparation_schema_is_independent_from_product_result_schema() -> None:
    """Harness throughput changes cannot alter product comparison compatibility."""
    result_report = build_benchmark_report("smoke")
    preparation_report = _report()

    assert result_report.schema_version == RESULT_SCHEMA_VERSION
    assert preparation_report.schema_version == PREPARATION_SCHEMA_VERSION
    assert "total_elapsed_ns" not in result_report.__dict__


def test_preparation_report_rejects_missing_or_reordered_samples(
    tmp_path: Path,
) -> None:
    """A preparation artifact must cover the exact ordered profile run."""
    report = _report()
    with pytest.raises(PreparationFormatError, match="complete ordered profile"):
        write_preparation_report(
            tmp_path / "missing.json",
            replace(report, samples=report.samples[:-1]),
        )

    reversed_samples = tuple(reversed(report.samples))
    with pytest.raises(PreparationFormatError, match="complete ordered profile"):
        write_preparation_report(
            tmp_path / "reordered.json",
            replace(report, samples=reversed_samples),
        )


def test_preparation_report_rejects_total_that_disagrees_with_samples(
    tmp_path: Path,
) -> None:
    """Consumers never trust a preparation total that contradicts raw evidence."""
    path = tmp_path / "bad-total.json"
    write_preparation_report(path, _report())
    raw = json.loads(path.read_text(encoding="ascii"))
    raw["total_elapsed_ns"] += 1
    path.write_text(json.dumps(raw), encoding="ascii")

    with pytest.raises(PreparationFormatError, match="sum"):
        load_preparation_report(path)
