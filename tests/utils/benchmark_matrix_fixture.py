"""Builders for complete benchmark reports used by harness unit tests."""

from __future__ import annotations

from collections.abc import Mapping

from scripts.perf.benchmark_matrix.catalog import RESULT_SCHEMA_VERSION, get_profile
from scripts.perf.benchmark_matrix.results import (
    BenchmarkReport,
    PlatformIdentity,
    make_scenario_result,
)

DEFAULT_PLATFORM = PlatformIdentity(
    system="linux",
    machine="x86_64",
    python_implementation="CPython",
    python_version="3.12.7",
)


def build_benchmark_report(
    profile_id: str = "smoke",
    *,
    default_sample_ns: int = 100_000_000,
    scenario_samples: Mapping[str, tuple[int, ...]] | None = None,
    repository_revision: str = "a" * 40,
    platform: PlatformIdentity = DEFAULT_PLATFORM,
) -> BenchmarkReport:
    """Build a complete current report with optional per-scenario samples."""
    profile = get_profile(profile_id)
    overrides = dict(scenario_samples or {})
    rows = tuple(
        make_scenario_result(
            profile,
            scenario_id,
            overrides.get(
                scenario_id,
                (default_sample_ns,) * profile.repetitions,
            ),
        )
        for scenario_id in profile.scenario_ids
    )
    return BenchmarkReport(
        schema_version=RESULT_SCHEMA_VERSION,
        profile=profile.id,
        baseline_authority=profile.baseline_authority,
        generated_at="2026-09-13T00:00:00Z",
        repository_revision=repository_revision,
        platform=platform,
        scenarios=rows,
    )
