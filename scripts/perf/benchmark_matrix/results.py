"""Typed benchmark result schema, statistics, serialization, and comparison."""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from apm_cli.utils.atomic_io import atomic_write_text

from .catalog import (
    CONTROL_SCENARIO_ID,
    RESULT_SCHEMA_VERSION,
    BenchmarkProfile,
    Thresholds,
    definition_hash,
    get_profile,
    get_scenario,
)

COMPARISON_SCHEMA_VERSION = 1


class ResultFormatError(ValueError):
    """Raised when a benchmark JSON document violates the versioned schema."""


@dataclass(frozen=True)
class PlatformIdentity:
    """Execution platform fields that must match for timing comparison."""

    system: str
    machine: str
    python_implementation: str
    python_version: str

    @property
    def compatibility_key(self) -> tuple[str, str, str, str]:
        """Return the exact platform comparison key."""
        return (
            self.system,
            self.machine,
            self.python_implementation,
            self.python_version,
        )


@dataclass(frozen=True)
class TimingStatistics:
    """Summary statistics derived only from retained raw nanosecond samples."""

    median_ns: float
    minimum_ns: int
    maximum_ns: int
    mean_ns: float
    stdev_ns: float
    p25_ns: float
    p75_ns: float
    p90_ns: float
    mad_ns: float
    robust_cv: float
    relative_range: float


@dataclass(frozen=True)
class ScenarioResult:
    """Measured data for one catalog scenario."""

    scenario_id: str
    definition_hash: str
    command: tuple[str, ...]
    fixture_size_id: str | None
    samples_ns: tuple[int, ...]
    statistics: TimingStatistics


@dataclass(frozen=True)
class BenchmarkReport:
    """Versioned benchmark result document."""

    schema_version: int
    profile: str
    baseline_authority: bool
    generated_at: str
    repository_revision: str
    platform: PlatformIdentity
    scenarios: tuple[ScenarioResult, ...]


class ComparisonStatus(str, Enum):
    """Advisory classification for one candidate scenario."""

    STABLE = "stable"
    IMPROVEMENT = "improvement"
    REGRESSION = "regression"
    NOISY = "noisy"
    MISSING = "missing"
    INCOMPATIBLE = "incompatible"


HARD_FAILURE_STATUSES = frozenset(
    {
        ComparisonStatus.MISSING,
    }
)


@dataclass(frozen=True)
class ComparisonRow:
    """Comparison output for one scenario ID."""

    scenario_id: str
    status: ComparisonStatus
    baseline_median_ns: float | None
    candidate_median_ns: float | None
    delta_ns: float | None
    delta_percent: float | None
    reason: str


@dataclass(frozen=True)
class ComparisonReport:
    """Versioned advisory comparison document."""

    schema_version: int
    baseline_profile: str
    candidate_profile: str
    rows: tuple[ComparisonRow, ...]


def calculate_statistics(samples_ns: tuple[int, ...]) -> TimingStatistics:
    """Calculate deterministic distribution statistics for positive samples."""
    if not samples_ns:
        raise ValueError("At least one timing sample is required")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in samples_ns
    ):
        raise ValueError("Timing samples must be positive integer nanoseconds")
    ordered = tuple(sorted(samples_ns))
    median = float(statistics.median(ordered))
    deviations = tuple(abs(value - median) for value in ordered)
    mad = float(statistics.median(deviations))
    robust_cv = 0.0 if median == 0 else (1.4826 * mad) / median
    relative_range = 0.0 if median == 0 else (ordered[-1] - ordered[0]) / median
    return TimingStatistics(
        median_ns=median,
        minimum_ns=ordered[0],
        maximum_ns=ordered[-1],
        mean_ns=float(statistics.fmean(ordered)),
        stdev_ns=float(statistics.pstdev(ordered)),
        p25_ns=_percentile(ordered, 0.25),
        p75_ns=_percentile(ordered, 0.75),
        p90_ns=_percentile(ordered, 0.90),
        mad_ns=mad,
        robust_cv=robust_cv,
        relative_range=relative_range,
    )


def make_scenario_result(
    profile: BenchmarkProfile,
    scenario_id: str,
    samples_ns: tuple[int, ...],
) -> ScenarioResult:
    """Build one result row entirely from its canonical catalog definition."""
    scenario = get_scenario(scenario_id)
    if scenario_id not in profile.scenario_ids:
        raise ValueError(f"Scenario {scenario_id!r} is not part of profile {profile.id!r}")
    if len(samples_ns) != profile.repetitions:
        raise ValueError(
            f"Scenario {scenario_id!r} requires {profile.repetitions} samples, "
            f"received {len(samples_ns)}"
        )
    return ScenarioResult(
        scenario_id=scenario.id,
        definition_hash=definition_hash(scenario),
        command=scenario.command,
        fixture_size_id=scenario.fixture_size_id,
        samples_ns=samples_ns,
        statistics=calculate_statistics(samples_ns),
    )


def write_report(path: Path, report: BenchmarkReport) -> None:
    """Atomically write deterministic printable-ASCII JSON with one LF suffix."""
    _validate_report(report)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, _json_text(_report_to_dict(report)))


def load_report(path: Path, *, require_current: bool = True) -> BenchmarkReport:
    """Load and validate a benchmark report from JSON."""
    try:
        raw = json.loads(path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ResultFormatError(f"Cannot read benchmark report {path}: {exc}") from exc
    return report_from_dict(raw, require_current=require_current)


def report_from_dict(raw: object, *, require_current: bool = True) -> BenchmarkReport:
    """Validate an untrusted decoded JSON value as a benchmark report."""
    document = _mapping(raw, "report")
    _exact_keys(
        document,
        {
            "schema_version",
            "profile",
            "baseline_authority",
            "generated_at",
            "repository_revision",
            "platform",
            "scenarios",
        },
        "report",
    )
    schema_version = _integer(document["schema_version"], "schema_version")
    profile = _string(document["profile"], "profile")
    baseline_authority = _boolean(document["baseline_authority"], "baseline_authority")
    generated_at = _string(document["generated_at"], "generated_at")
    repository_revision = _string(document["repository_revision"], "repository_revision")
    platform = _platform_from_dict(document["platform"])
    raw_scenarios = _list(document["scenarios"], "scenarios")
    scenarios = tuple(
        _scenario_from_dict(value, f"scenarios[{index}]")
        for index, value in enumerate(raw_scenarios)
    )
    report = BenchmarkReport(
        schema_version=schema_version,
        profile=profile,
        baseline_authority=baseline_authority,
        generated_at=generated_at,
        repository_revision=repository_revision,
        platform=platform,
        scenarios=scenarios,
    )
    _validate_report(report, require_current=require_current)
    return report


def compare_reports(
    baseline: BenchmarkReport,
    candidate: BenchmarkReport,
) -> ComparisonReport:
    """Compare compatible rows while treating timing movement as advisory."""
    _validate_report(baseline, require_current=False)
    _validate_report(candidate)
    baseline_rows = _scenario_map(baseline)
    candidate_rows = _scenario_map(candidate)
    scenario_ids = tuple(row.scenario_id for row in candidate.scenarios)
    platform_compatible = (
        baseline.schema_version == candidate.schema_version == RESULT_SCHEMA_VERSION
        and baseline.platform.compatibility_key == candidate.platform.compatibility_key
    )
    control_delta = _compatible_control_delta(
        baseline_rows,
        candidate_rows,
        platform_compatible=platform_compatible,
    )
    rows = tuple(
        _compare_row(
            scenario_id,
            baseline_rows.get(scenario_id),
            candidate_rows.get(scenario_id),
            platform_compatible=platform_compatible,
            control_delta_percent=control_delta,
        )
        for scenario_id in scenario_ids
    )
    return ComparisonReport(
        schema_version=COMPARISON_SCHEMA_VERSION,
        baseline_profile=baseline.profile,
        candidate_profile=candidate.profile,
        rows=rows,
    )


def write_comparison(path: Path, comparison: ComparisonReport) -> None:
    """Atomically write one deterministic comparison JSON document."""
    _validate_comparison(comparison)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, _json_text(_comparison_to_dict(comparison)))


def comparison_text(comparison: ComparisonReport) -> str:
    """Return deterministic printable-ASCII JSON for stdout or files."""
    _validate_comparison(comparison)
    return _json_text(_comparison_to_dict(comparison))


def comparison_has_hard_failures(comparison: ComparisonReport) -> bool:
    """Return whether missing rows invalidate the requested comparison."""
    _validate_comparison(comparison)
    return any(row.status in HARD_FAILURE_STATUSES for row in comparison.rows)


def load_comparison(path: Path) -> ComparisonReport:
    """Load and validate a comparison document from JSON."""
    try:
        raw = json.loads(path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ResultFormatError(f"Cannot read benchmark comparison {path}: {exc}") from exc
    value = _mapping(raw, "comparison")
    _exact_keys(
        value,
        {"schema_version", "baseline_profile", "candidate_profile", "rows"},
        "comparison",
    )
    raw_rows = _list(value["rows"], "comparison.rows")
    rows = tuple(
        _comparison_row_from_dict(item, f"comparison.rows[{index}]")
        for index, item in enumerate(raw_rows)
    )
    comparison = ComparisonReport(
        schema_version=_integer(value["schema_version"], "comparison.schema_version"),
        baseline_profile=_string(
            value["baseline_profile"],
            "comparison.baseline_profile",
        ),
        candidate_profile=_string(
            value["candidate_profile"],
            "comparison.candidate_profile",
        ),
        rows=rows,
    )
    _validate_comparison(comparison)
    return comparison


def comparison_summary(comparison: ComparisonReport) -> str:
    """Render an ASCII GitHub-flavored Markdown summary."""
    _validate_comparison(comparison)
    counts = {
        status: sum(row.status is status for row in comparison.rows) for status in ComparisonStatus
    }
    lines = [
        "# Benchmark comparison",
        "",
        (
            f"Baseline profile: `{comparison.baseline_profile}`. "
            f"Candidate profile: `{comparison.candidate_profile}`."
        ),
        "",
        (
            "Statuses: "
            + ", ".join(f"{status.value}={counts[status]}" for status in ComparisonStatus)
            + ". Regression, noisy, and incompatible statuses are advisory."
        ),
        "",
        "| Scenario | Status | Baseline ms | Candidate ms | Delta | Reason |",
        "| --- | --- | ---: | ---: | ---: | --- |",
    ]
    for row in comparison.rows:
        lines.append(
            "| "
            + " | ".join(
                (
                    row.scenario_id,
                    row.status.value,
                    _format_milliseconds(row.baseline_median_ns),
                    _format_milliseconds(row.candidate_median_ns),
                    _format_percent(row.delta_percent),
                    row.reason,
                )
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def report_summary(report: BenchmarkReport) -> str:
    """Render one benchmark result document as ASCII Markdown."""
    _validate_report(report)
    if (report.platform.system, report.platform.machine) != ("linux", "x86_64"):
        raise ResultFormatError("Result summary requires Linux x86_64 results")
    authority = "yes" if report.baseline_authority else "no"
    lines = [
        "# Benchmark results",
        "",
        (f"Profile: `{_escape_markdown_cell(report.profile)}`. Baseline authority: `{authority}`."),
        (
            f"Platform: `{_escape_markdown_cell(report.platform.system)} "
            f"{_escape_markdown_cell(report.platform.machine)}`. "
            f"Revision: `{_escape_markdown_cell(report.repository_revision)}`."
        ),
        "",
        "| Scenario | Median ms | Min ms | Max ms | P90 ms | Robust CV | Samples |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in report.scenarios:
        lines.append(
            "| "
            + " | ".join(
                (
                    _escape_markdown_cell(row.scenario_id),
                    _format_milliseconds(row.statistics.median_ns),
                    _format_milliseconds(float(row.statistics.minimum_ns)),
                    _format_milliseconds(float(row.statistics.maximum_ns)),
                    _format_milliseconds(row.statistics.p90_ns),
                    f"{row.statistics.robust_cv:.4f}",
                    str(len(row.samples_ns)),
                )
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def _compare_row(
    scenario_id: str,
    baseline: ScenarioResult | None,
    candidate: ScenarioResult | None,
    *,
    platform_compatible: bool,
    control_delta_percent: float | None,
) -> ComparisonRow:
    if baseline is None or candidate is None:
        return ComparisonRow(
            scenario_id=scenario_id,
            status=ComparisonStatus.MISSING,
            baseline_median_ns=baseline.statistics.median_ns if baseline else None,
            candidate_median_ns=candidate.statistics.median_ns if candidate else None,
            delta_ns=None,
            delta_percent=None,
            reason="scenario is absent from one report",
        )
    try:
        scenario = get_scenario(scenario_id)
    except ValueError:
        return _incompatible_row(scenario_id, baseline, candidate, "scenario is not in catalog")
    if not platform_compatible:
        return _incompatible_row(
            scenario_id,
            baseline,
            candidate,
            "schema version or platform identity differs",
        )
    current_definition_hash = definition_hash(scenario)
    if (
        baseline.definition_hash != candidate.definition_hash
        or baseline.definition_hash != current_definition_hash
    ):
        return _incompatible_row(
            scenario_id,
            baseline,
            candidate,
            "scenario definition hashes differ or are not current",
        )

    baseline_median = baseline.statistics.median_ns
    candidate_median = candidate.statistics.median_ns
    delta_ns = candidate_median - baseline_median
    delta_percent = 0.0 if baseline_median == 0 else (delta_ns / baseline_median) * 100.0
    thresholds = scenario.thresholds
    distribution_noisy = _distribution_is_noisy(baseline, thresholds) or (
        _distribution_is_noisy(candidate, thresholds)
    )
    movement_significant = (
        abs(delta_ns) >= thresholds.absolute_floor_ns
        and abs(delta_percent) >= thresholds.percent_floor
    )
    control_masks_movement = (
        not scenario.control
        and control_delta_percent is not None
        and (delta_percent * control_delta_percent) > 0
        and abs(control_delta_percent) >= thresholds.control_noise_percent
        and abs(delta_percent) <= abs(control_delta_percent) + thresholds.percent_floor
    )
    if scenario.control and movement_significant:
        status = ComparisonStatus.NOISY
        reason = "control row moved beyond its percent and absolute floors"
    elif distribution_noisy:
        status = ComparisonStatus.NOISY
        reason = "baseline or candidate distribution exceeds a scenario noise ceiling"
    elif control_masks_movement:
        status = ComparisonStatus.NOISY
        reason = "control drift is large enough to mask this timing movement"
    elif not movement_significant:
        status = ComparisonStatus.STABLE
        reason = "movement does not exceed both percent and absolute floors"
    elif delta_ns < 0:
        status = ComparisonStatus.IMPROVEMENT
        reason = "candidate median improved beyond both advisory floors"
    else:
        status = ComparisonStatus.REGRESSION
        reason = "candidate median regressed beyond both advisory floors"
    return ComparisonRow(
        scenario_id=scenario_id,
        status=status,
        baseline_median_ns=baseline_median,
        candidate_median_ns=candidate_median,
        delta_ns=delta_ns,
        delta_percent=delta_percent,
        reason=reason,
    )


def _compatible_control_delta(
    baseline_rows: dict[str, ScenarioResult],
    candidate_rows: dict[str, ScenarioResult],
    *,
    platform_compatible: bool,
) -> float | None:
    if not platform_compatible:
        return None
    baseline = baseline_rows.get(CONTROL_SCENARIO_ID)
    candidate = candidate_rows.get(CONTROL_SCENARIO_ID)
    if baseline is None or candidate is None:
        return None
    control = get_scenario(CONTROL_SCENARIO_ID)
    if (
        baseline.definition_hash != candidate.definition_hash
        or baseline.definition_hash != definition_hash(control)
    ):
        return None
    if _distribution_is_noisy(baseline, control.thresholds) or _distribution_is_noisy(
        candidate,
        control.thresholds,
    ):
        return None
    baseline_median = baseline.statistics.median_ns
    if baseline_median == 0:
        return 0.0
    return ((candidate.statistics.median_ns - baseline_median) / baseline_median) * 100.0


def _incompatible_row(
    scenario_id: str,
    baseline: ScenarioResult,
    candidate: ScenarioResult,
    reason: str,
) -> ComparisonRow:
    return ComparisonRow(
        scenario_id=scenario_id,
        status=ComparisonStatus.INCOMPATIBLE,
        baseline_median_ns=baseline.statistics.median_ns,
        candidate_median_ns=candidate.statistics.median_ns,
        delta_ns=None,
        delta_percent=None,
        reason=reason,
    )


def _distribution_is_noisy(
    result: ScenarioResult,
    thresholds: Thresholds,
) -> bool:
    return (
        result.statistics.robust_cv > thresholds.max_robust_cv
        or result.statistics.relative_range > thresholds.max_relative_range
    )


def _validate_report(
    report: BenchmarkReport,
    *,
    require_current: bool = True,
) -> None:
    if report.schema_version != RESULT_SCHEMA_VERSION:
        raise ResultFormatError(f"Unsupported benchmark schema version: {report.schema_version}")
    try:
        profile = get_profile(report.profile)
    except ValueError as exc:
        raise ResultFormatError(f"Unknown benchmark report profile: {report.profile!r}") from exc
    if report.baseline_authority is not profile.baseline_authority:
        raise ResultFormatError(
            f"Report baseline authority does not match profile {report.profile!r}"
        )
    if not report.generated_at:
        raise ResultFormatError("Report generated_at must not be empty")
    if len(report.repository_revision) != 40 or any(
        character not in "0123456789abcdefABCDEF" for character in report.repository_revision
    ):
        raise ResultFormatError("Report repository_revision must be a 40-character Git SHA")
    if not all(
        (
            report.platform.system,
            report.platform.machine,
            report.platform.python_implementation,
            report.platform.python_version,
        )
    ):
        raise ResultFormatError("Report platform identity fields must not be empty")
    scenario_ids = [row.scenario_id for row in report.scenarios]
    if len(scenario_ids) != len(set(scenario_ids)):
        raise ResultFormatError("Report scenario IDs must be unique")
    if require_current and tuple(scenario_ids) != profile.scenario_ids:
        raise ResultFormatError(
            f"Report scenarios do not match the complete ordered {profile.id!r} profile"
        )
    for row in report.scenarios:
        if len(row.definition_hash) != 64 or any(
            character not in "0123456789abcdef" for character in row.definition_hash
        ):
            raise ResultFormatError(
                f"Scenario definition hash must be SHA-256 hex: {row.scenario_id}"
            )
        if not row.command:
            raise ResultFormatError(f"Scenario command must not be empty: {row.scenario_id}")
        if len(row.samples_ns) < 1:
            raise ResultFormatError(f"Scenario samples must not be empty: {row.scenario_id}")
        expected_statistics = calculate_statistics(row.samples_ns)
        if expected_statistics != row.statistics:
            raise ResultFormatError(
                f"Scenario statistics do not match raw samples: {row.scenario_id}"
            )
        if require_current:
            try:
                scenario = get_scenario(row.scenario_id)
            except ValueError as exc:
                raise ResultFormatError(
                    f"Report contains an unknown current scenario: {row.scenario_id}"
                ) from exc
            if row.definition_hash != definition_hash(scenario):
                raise ResultFormatError(
                    f"Scenario definition hash is not current: {row.scenario_id}"
                )
            if row.command != scenario.command:
                raise ResultFormatError(f"Scenario command is not current: {row.scenario_id}")
            if row.fixture_size_id != scenario.fixture_size_id:
                raise ResultFormatError(f"Scenario fixture size is not current: {row.scenario_id}")
            if len(row.samples_ns) != profile.repetitions:
                raise ResultFormatError(
                    f"Scenario sample count does not match profile: {row.scenario_id}"
                )
        else:
            try:
                scenario = get_scenario(row.scenario_id)
            except ValueError:
                continue
            if row.definition_hash == definition_hash(scenario):
                if row.command != scenario.command:
                    raise ResultFormatError(
                        f"Scenario command contradicts its current definition hash: "
                        f"{row.scenario_id}"
                    )
                if row.fixture_size_id != scenario.fixture_size_id:
                    raise ResultFormatError(
                        f"Scenario fixture size contradicts its current definition hash: "
                        f"{row.scenario_id}"
                    )


def is_current_profile_report(report: BenchmarkReport, profile_id: str) -> bool:
    """Return whether a structurally valid report exactly matches a current profile."""
    if report.profile != profile_id:
        return False
    try:
        _validate_report(report)
    except ResultFormatError:
        return False
    return True


def _scenario_map(report: BenchmarkReport) -> dict[str, ScenarioResult]:
    return {row.scenario_id: row for row in report.scenarios}


def _report_to_dict(report: BenchmarkReport) -> dict[str, object]:
    return {
        "schema_version": report.schema_version,
        "profile": report.profile,
        "baseline_authority": report.baseline_authority,
        "generated_at": report.generated_at,
        "repository_revision": report.repository_revision,
        "platform": {
            "system": report.platform.system,
            "machine": report.platform.machine,
            "python_implementation": report.platform.python_implementation,
            "python_version": report.platform.python_version,
        },
        "scenarios": [
            {
                "scenario_id": row.scenario_id,
                "definition_hash": row.definition_hash,
                "command": list(row.command),
                "fixture_size_id": row.fixture_size_id,
                "samples_ns": list(row.samples_ns),
                "statistics": _statistics_to_dict(row.statistics),
            }
            for row in report.scenarios
        ],
    }


def _comparison_to_dict(comparison: ComparisonReport) -> dict[str, object]:
    return {
        "schema_version": comparison.schema_version,
        "baseline_profile": comparison.baseline_profile,
        "candidate_profile": comparison.candidate_profile,
        "rows": [
            {
                "scenario_id": row.scenario_id,
                "status": row.status.value,
                "baseline_median_ns": row.baseline_median_ns,
                "candidate_median_ns": row.candidate_median_ns,
                "delta_ns": row.delta_ns,
                "delta_percent": row.delta_percent,
                "reason": row.reason,
            }
            for row in comparison.rows
        ],
    }


def _comparison_row_from_dict(raw: object, context: str) -> ComparisonRow:
    value = _mapping(raw, context)
    _exact_keys(
        value,
        {
            "scenario_id",
            "status",
            "baseline_median_ns",
            "candidate_median_ns",
            "delta_ns",
            "delta_percent",
            "reason",
        },
        context,
    )
    status_raw = _string(value["status"], f"{context}.status")
    try:
        status = ComparisonStatus(status_raw)
    except ValueError as exc:
        raise ResultFormatError(f"{context}.status is unknown: {status_raw!r}") from exc
    return ComparisonRow(
        scenario_id=_string(value["scenario_id"], f"{context}.scenario_id"),
        status=status,
        baseline_median_ns=_optional_number(
            value["baseline_median_ns"],
            f"{context}.baseline_median_ns",
        ),
        candidate_median_ns=_optional_number(
            value["candidate_median_ns"],
            f"{context}.candidate_median_ns",
        ),
        delta_ns=_optional_number(value["delta_ns"], f"{context}.delta_ns"),
        delta_percent=_optional_number(
            value["delta_percent"],
            f"{context}.delta_percent",
        ),
        reason=_string(value["reason"], f"{context}.reason"),
    )


def _validate_comparison(comparison: ComparisonReport) -> None:
    if comparison.schema_version != COMPARISON_SCHEMA_VERSION:
        raise ResultFormatError(
            f"Unsupported comparison schema version: {comparison.schema_version}"
        )
    if not comparison.baseline_profile or not comparison.candidate_profile:
        raise ResultFormatError("Comparison profile IDs must not be empty")
    scenario_ids = [row.scenario_id for row in comparison.rows]
    if len(scenario_ids) != len(set(scenario_ids)):
        raise ResultFormatError("Comparison scenario IDs must be unique")
    for row in comparison.rows:
        if not row.scenario_id or not row.reason:
            raise ResultFormatError("Comparison rows require scenario IDs and reasons")
        numeric_values = (
            row.baseline_median_ns,
            row.candidate_median_ns,
            row.delta_ns,
            row.delta_percent,
        )
        if any(value is not None and not math.isfinite(value) for value in numeric_values):
            raise ResultFormatError(
                f"Comparison row contains a non-finite number: {row.scenario_id}"
            )


def _statistics_to_dict(value: TimingStatistics) -> dict[str, float | int]:
    return {
        "median_ns": value.median_ns,
        "minimum_ns": value.minimum_ns,
        "maximum_ns": value.maximum_ns,
        "mean_ns": value.mean_ns,
        "stdev_ns": value.stdev_ns,
        "p25_ns": value.p25_ns,
        "p75_ns": value.p75_ns,
        "p90_ns": value.p90_ns,
        "mad_ns": value.mad_ns,
        "robust_cv": value.robust_cv,
        "relative_range": value.relative_range,
    }


def _platform_from_dict(raw: object) -> PlatformIdentity:
    value = _mapping(raw, "platform")
    _exact_keys(
        value,
        {"system", "machine", "python_implementation", "python_version"},
        "platform",
    )
    return PlatformIdentity(
        system=_string(value["system"], "platform.system"),
        machine=_string(value["machine"], "platform.machine"),
        python_implementation=_string(
            value["python_implementation"],
            "platform.python_implementation",
        ),
        python_version=_string(value["python_version"], "platform.python_version"),
    )


def _scenario_from_dict(raw: object, context: str) -> ScenarioResult:
    value = _mapping(raw, context)
    _exact_keys(
        value,
        {
            "scenario_id",
            "definition_hash",
            "command",
            "fixture_size_id",
            "samples_ns",
            "statistics",
        },
        context,
    )
    raw_command = _list(value["command"], f"{context}.command")
    command = tuple(
        _string(item, f"{context}.command[{index}]") for index, item in enumerate(raw_command)
    )
    raw_samples = _list(value["samples_ns"], f"{context}.samples_ns")
    samples = tuple(
        _integer(item, f"{context}.samples_ns[{index}]") for index, item in enumerate(raw_samples)
    )
    fixture_size_raw = value["fixture_size_id"]
    fixture_size_id = (
        None
        if fixture_size_raw is None
        else _string(fixture_size_raw, f"{context}.fixture_size_id")
    )
    return ScenarioResult(
        scenario_id=_string(value["scenario_id"], f"{context}.scenario_id"),
        definition_hash=_string(
            value["definition_hash"],
            f"{context}.definition_hash",
        ),
        command=command,
        fixture_size_id=fixture_size_id,
        samples_ns=samples,
        statistics=_statistics_from_dict(value["statistics"], f"{context}.statistics"),
    )


def _statistics_from_dict(raw: object, context: str) -> TimingStatistics:
    value = _mapping(raw, context)
    keys = {
        "median_ns",
        "minimum_ns",
        "maximum_ns",
        "mean_ns",
        "stdev_ns",
        "p25_ns",
        "p75_ns",
        "p90_ns",
        "mad_ns",
        "robust_cv",
        "relative_range",
    }
    _exact_keys(value, keys, context)
    return TimingStatistics(
        median_ns=_number(value["median_ns"], f"{context}.median_ns"),
        minimum_ns=_integer(value["minimum_ns"], f"{context}.minimum_ns"),
        maximum_ns=_integer(value["maximum_ns"], f"{context}.maximum_ns"),
        mean_ns=_number(value["mean_ns"], f"{context}.mean_ns"),
        stdev_ns=_number(value["stdev_ns"], f"{context}.stdev_ns"),
        p25_ns=_number(value["p25_ns"], f"{context}.p25_ns"),
        p75_ns=_number(value["p75_ns"], f"{context}.p75_ns"),
        p90_ns=_number(value["p90_ns"], f"{context}.p90_ns"),
        mad_ns=_number(value["mad_ns"], f"{context}.mad_ns"),
        robust_cv=_number(value["robust_cv"], f"{context}.robust_cv"),
        relative_range=_number(value["relative_range"], f"{context}.relative_range"),
    )


def _percentile(ordered: tuple[int, ...], quantile: float) -> float:
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    fraction = position - lower
    return float(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction)


def _json_text(value: object) -> str:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _mapping(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ResultFormatError(f"{context} must be an object with string keys")
    return value


def _list(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise ResultFormatError(f"{context} must be an array")
    return value


def _string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ResultFormatError(f"{context} must be a non-empty string")
    return value


def _integer(value: object, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ResultFormatError(f"{context} must be an integer")
    return value


def _number(value: object, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResultFormatError(f"{context} must be a finite number")
    converted = float(value)
    if not math.isfinite(converted):
        raise ResultFormatError(f"{context} must be a finite number")
    return converted


def _optional_number(value: object, context: str) -> float | None:
    if value is None:
        return None
    return _number(value, context)


def _boolean(value: object, context: str) -> bool:
    if not isinstance(value, bool):
        raise ResultFormatError(f"{context} must be a boolean")
    return value


def _exact_keys(value: dict[str, object], keys: set[str], context: str) -> None:
    actual = set(value)
    if actual != keys:
        missing = sorted(keys - actual)
        extra = sorted(actual - keys)
        raise ResultFormatError(f"{context} has unexpected keys; missing={missing}, extra={extra}")


def _format_milliseconds(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value / 1_000_000:.3f}"


def _format_percent(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:+.2f}%"


def _escape_markdown_cell(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")
