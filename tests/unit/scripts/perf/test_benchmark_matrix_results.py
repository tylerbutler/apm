"""Tests for benchmark statistics, result JSON, and advisory comparison."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.perf.benchmark_matrix.results import (
    ComparisonStatus,
    ResultFormatError,
    calculate_statistics,
    compare_reports,
    comparison_has_hard_failures,
    comparison_summary,
    load_comparison,
    load_report,
    report_summary,
    write_comparison,
    write_report,
)
from tests.utils.benchmark_matrix_fixture import DEFAULT_PLATFORM, build_benchmark_report

pytestmark = pytest.mark.component


def _report(
    *,
    control_samples: tuple[int, ...] = (100_000_000,) * 5,
    operation_samples: tuple[int, ...] = (100_000_000,) * 5,
):
    return build_benchmark_report(
        scenario_samples={
            "startup.version": control_samples,
            "install.cold.medium": operation_samples,
        }
    )


def test_statistics_include_distribution_and_robust_metrics() -> None:
    """Raw samples produce every required summary statistic."""
    stats = calculate_statistics((10, 20, 30, 40, 100))
    assert stats.median_ns == 30.0
    assert stats.minimum_ns == 10
    assert stats.maximum_ns == 100
    assert stats.mean_ns == 40.0
    assert stats.p25_ns == 20.0
    assert stats.p75_ns == 40.0
    assert stats.p90_ns == pytest.approx(76.0)
    assert stats.mad_ns == 10.0
    assert stats.robust_cv == pytest.approx(0.4942)
    assert stats.relative_range == 3.0
    assert stats.stdev_ns > 0


def test_range_guard_detects_single_extreme_outlier_with_zero_mad() -> None:
    """A repeated median cannot hide a severe tail outlier."""
    comparison = compare_reports(
        _report(),
        _report(operation_samples=(100_000_000,) * 4 + (1_000_000_000,)),
    )
    row = next(item for item in comparison.rows if item.scenario_id == "install.cold.medium")
    assert row.status is ComparisonStatus.NOISY


@pytest.mark.windows_compat
def test_report_serialization_is_ascii_lf_atomic_and_round_trips(tmp_path: Path) -> None:
    """Result bytes are deterministic across platform newline defaults."""
    report = _report()
    path = tmp_path / "nested" / "result.json"
    write_report(path, report)

    payload = path.read_bytes()
    assert payload.endswith(b"\n")
    assert b"\r\n" not in payload
    assert all(byte < 128 for byte in payload)
    assert load_report(path) == report


def test_load_report_rejects_statistics_that_do_not_match_samples(tmp_path: Path) -> None:
    """Consumers never trust summaries that contradict retained evidence."""
    path = tmp_path / "bad.json"
    report = _report()
    write_report(path, report)
    raw = json.loads(path.read_text(encoding="ascii"))
    raw["scenarios"][0]["statistics"]["median_ns"] = 1
    path.write_text(json.dumps(raw), encoding="ascii")

    with pytest.raises(ResultFormatError, match="statistics do not match"):
        load_report(path)


@pytest.mark.parametrize(
    ("candidate_ns", "expected"),
    [
        (110_000_000, ComparisonStatus.STABLE),
        (130_000_000, ComparisonStatus.REGRESSION),
        (70_000_000, ComparisonStatus.IMPROVEMENT),
    ],
)
def test_comparison_applies_percent_and_absolute_floors(
    candidate_ns: int,
    expected: ComparisonStatus,
) -> None:
    """Movement is actionable only when both catalog floors are crossed."""
    comparison = compare_reports(
        _report(),
        _report(operation_samples=(candidate_ns,) * 5),
    )
    row = next(item for item in comparison.rows if item.scenario_id == "install.cold.medium")
    assert row.status is expected


def test_comparison_marks_control_masked_movement_noisy() -> None:
    """Large process-control drift prevents a false timing verdict."""
    comparison = compare_reports(
        _report(),
        _report(
            control_samples=(120_000_000,) * 5,
            operation_samples=(130_000_000,) * 5,
        ),
    )
    rows = {row.scenario_id: row for row in comparison.rows}
    assert rows["startup.version"].status is ComparisonStatus.NOISY
    assert rows["install.cold.medium"].status is ComparisonStatus.NOISY


def test_comparison_marks_high_robust_cv_noisy() -> None:
    """Unstable distributions do not become regression claims."""
    comparison = compare_reports(
        _report(),
        _report(
            operation_samples=(
                80_000_000,
                100_000_000,
                130_000_000,
                160_000_000,
                180_000_000,
            )
        ),
    )
    row = next(item for item in comparison.rows if item.scenario_id == "install.cold.medium")
    assert row.status is ComparisonStatus.NOISY


def test_noisy_control_distribution_cannot_mask_operation_regression() -> None:
    """Control drift masks rows only when both control distributions are stable."""
    comparison = compare_reports(
        _report(),
        _report(
            control_samples=(
                120_000_000,
                120_000_000,
                120_000_000,
                120_000_000,
                1_200_000_000,
            ),
            operation_samples=(130_000_000,) * 5,
        ),
    )
    rows = {row.scenario_id: row for row in comparison.rows}
    assert rows["startup.version"].status is ComparisonStatus.NOISY
    assert rows["install.cold.medium"].status is ComparisonStatus.REGRESSION


def test_comparison_reports_missing_and_incompatible_rows() -> None:
    """Missing rows and platform/hash drift are explicit statuses."""
    baseline = _report()
    missing_baseline = replace(baseline, scenarios=baseline.scenarios[:1])
    missing = compare_reports(missing_baseline, baseline)
    missing_row = next(item for item in missing.rows if item.scenario_id == "install.cold.medium")
    assert missing_row.status is ComparisonStatus.MISSING
    assert comparison_has_hard_failures(missing) is True

    incompatible_candidate = replace(
        baseline,
        platform=replace(DEFAULT_PLATFORM, machine="aarch64"),
    )
    incompatible = compare_reports(baseline, incompatible_candidate)
    assert {row.status for row in incompatible.rows} == {ComparisonStatus.INCOMPATIBLE}
    assert comparison_has_hard_failures(incompatible) is False

    stale_row = replace(baseline.scenarios[1], definition_hash="0" * 64)
    stale = replace(baseline, scenarios=(baseline.scenarios[0], stale_row))
    stale_comparison = compare_reports(stale, baseline)
    stale_result = next(
        row for row in stale_comparison.rows if row.scenario_id == "install.cold.medium"
    )
    assert stale_result.status is ComparisonStatus.INCOMPATIBLE


def test_timing_statuses_remain_advisory() -> None:
    """Regression and noise classifications do not become harness failures."""
    regression = compare_reports(
        _report(),
        _report(operation_samples=(130_000_000,) * 5),
    )
    noisy = compare_reports(
        _report(),
        _report(
            operation_samples=(
                80_000_000,
                100_000_000,
                130_000_000,
                160_000_000,
                180_000_000,
            )
        ),
    )
    assert comparison_has_hard_failures(regression) is False
    assert comparison_has_hard_failures(noisy) is False


def test_smoke_rows_compare_with_full_baseline_rows() -> None:
    """Shared scenario definitions remain compatible across profile sample counts."""
    baseline = build_benchmark_report("full")
    candidate = build_benchmark_report("smoke")
    comparison = compare_reports(baseline, candidate)
    assert {row.status for row in comparison.rows} == {ComparisonStatus.STABLE}


def test_comparison_round_trip_and_markdown_summary(tmp_path: Path) -> None:
    """Comparison JSON is typed and the summary remains printable ASCII."""
    comparison = compare_reports(_report(), _report())
    path = tmp_path / "comparison.json"
    write_comparison(path, comparison)

    loaded = load_comparison(path)
    summary = comparison_summary(loaded)
    assert loaded == comparison
    assert "# Benchmark comparison" in summary
    assert "| startup.version | stable |" in summary
    assert summary.endswith("\n")
    assert summary.isascii()


@pytest.mark.parametrize("profile_id", ["smoke", "full", "live"])
def test_report_summary_supports_every_complete_current_profile(profile_id: str) -> None:
    """Direct summaries support bootstrap and observational profiles."""
    report = build_benchmark_report(profile_id)
    summary = report_summary(report)
    assert summary.startswith("# Benchmark results\n")
    assert f"Profile: `{profile_id}`." in summary
    assert summary.endswith("\n")
    assert summary.isascii()


def test_report_validation_rejects_truncated_or_mislabeled_profile(
    tmp_path: Path,
) -> None:
    """Current reports must contain the exact ordered catalog profile."""
    report = _report()
    with pytest.raises(ResultFormatError, match="complete ordered"):
        write_report(tmp_path / "truncated.json", replace(report, scenarios=report.scenarios[:-1]))
    with pytest.raises(ResultFormatError, match="baseline authority"):
        write_report(tmp_path / "authority.json", replace(report, baseline_authority=True))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("definition_hash", "0" * 64, "definition hash"),
        ("command", ("install",), "command"),
        ("fixture_size_id", "small", "fixture size"),
    ],
)
def test_report_validation_rejects_stale_scenario_definition(
    field: str,
    value: object,
    message: str,
    tmp_path: Path,
) -> None:
    """Commands, hashes, and fixture sizes are part of report validity."""
    report = _report()
    row = replace(report.scenarios[1], **{field: value})
    mutated = replace(
        report,
        scenarios=(report.scenarios[0], row, *report.scenarios[2:]),
    )
    with pytest.raises(ResultFormatError, match=message):
        write_report(tmp_path / f"{field}.json", mutated)


def test_report_validation_rejects_wrong_profile_sample_count(tmp_path: Path) -> None:
    """Every row must contain the catalog profile's exact repetition count."""
    report = _report()
    samples = report.scenarios[1].samples_ns[:-1]
    row = replace(
        report.scenarios[1],
        samples_ns=samples,
        statistics=calculate_statistics(samples),
    )
    mutated = replace(
        report,
        scenarios=(report.scenarios[0], row, *report.scenarios[2:]),
    )
    with pytest.raises(ResultFormatError, match="sample count"):
        write_report(tmp_path / "samples.json", mutated)


def test_structural_baseline_validation_rejects_false_current_definition() -> None:
    """A current hash cannot authenticate a different command or invalid digest."""
    baseline = build_benchmark_report("full")
    candidate = _report()
    target_index = next(
        index
        for index, row in enumerate(baseline.scenarios)
        if row.scenario_id == "install.cold.medium"
    )
    target = baseline.scenarios[target_index]
    wrong_command = replace(target, command=("install", "--parallel-downloads", "1"))
    scenarios = list(baseline.scenarios)
    scenarios[target_index] = wrong_command
    with pytest.raises(ResultFormatError, match="contradicts"):
        compare_reports(replace(baseline, scenarios=tuple(scenarios)), candidate)

    scenarios[target_index] = replace(target, definition_hash="z" * 64)
    with pytest.raises(ResultFormatError, match="SHA-256 hex"):
        compare_reports(replace(baseline, scenarios=tuple(scenarios)), candidate)
