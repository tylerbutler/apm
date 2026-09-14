"""Canonical benchmark scenario, profile, and threshold catalog.

This module is the single owner of durable benchmark decisions. Fixture
builders, runners, result comparison, and the command-line entry point consume
these definitions instead of recreating scenario IDs, commands, sizes, or
thresholds.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType

CATALOG_VERSION = 2
RESULT_SCHEMA_VERSION = 2

BASELINE_WORKFLOW = "benchmark.yml"
BASELINE_ARTIFACT = "benchmark-full-main"
BASELINE_RESULT_FILE = "results.json"
CONTROL_SCENARIO_ID = "startup.version"
LIVE_DEPENDENCY = "microsoft/apm-sample-package#v1.0.0"


@dataclass(frozen=True)
class FixtureSize:
    """Deterministic amount of fixture work for one scenario."""

    id: str
    package_count: int
    primitives_per_package: int
    payload_bytes: int


@dataclass(frozen=True)
class Thresholds:
    """Advisory timing and noise thresholds for one scenario family."""

    percent_floor: float
    absolute_floor_ns: int
    max_robust_cv: float
    max_relative_range: float
    control_noise_percent: float


@dataclass(frozen=True)
class BenchmarkScenario:
    """One stable benchmark row definition."""

    id: str
    operation: str
    temperature: str
    fixture_size_id: str | None
    command: tuple[str, ...]
    timeout_seconds: float
    thresholds: Thresholds
    control: bool = False
    live: bool = False


@dataclass(frozen=True)
class BenchmarkProfile:
    """Named collection of scenario rows with one repetition policy."""

    id: str
    scenario_ids: tuple[str, ...]
    repetitions: int
    baseline_authority: bool


FIXTURE_SIZES = MappingProxyType(
    {
        "small": FixtureSize(
            id="small",
            package_count=1,
            primitives_per_package=2,
            payload_bytes=512,
        ),
        "medium": FixtureSize(
            id="medium",
            package_count=4,
            primitives_per_package=8,
            payload_bytes=2_048,
        ),
        "large": FixtureSize(
            id="large",
            package_count=12,
            primitives_per_package=24,
            payload_bytes=8_192,
        ),
    }
)

_STARTUP_THRESHOLDS = Thresholds(
    percent_floor=10.0,
    absolute_floor_ns=5_000_000,
    max_robust_cv=0.15,
    max_relative_range=0.50,
    control_noise_percent=8.0,
)
_OPERATION_THRESHOLDS = Thresholds(
    percent_floor=12.0,
    absolute_floor_ns=25_000_000,
    max_robust_cv=0.12,
    max_relative_range=0.40,
    control_noise_percent=8.0,
)
_LIVE_THRESHOLDS = Thresholds(
    percent_floor=20.0,
    absolute_floor_ns=100_000_000,
    max_robust_cv=0.25,
    max_relative_range=1.00,
    control_noise_percent=15.0,
)

INSTALL_COMMAND = (
    "install",
    "--no-policy",
    "--no-audit",
    "--target",
    "copilot",
)
UPDATE_COMMAND = (
    "update",
    "--yes",
    "--target",
    "copilot",
)
COMPILE_COMMAND = (
    "compile",
    "--target",
    "copilot",
    "--force-instructions",
)


def _operation_scenarios() -> tuple[BenchmarkScenario, ...]:
    scenarios: list[BenchmarkScenario] = [
        BenchmarkScenario(
            id=CONTROL_SCENARIO_ID,
            operation="startup",
            temperature="process",
            fixture_size_id=None,
            command=("--version",),
            timeout_seconds=30.0,
            thresholds=_STARTUP_THRESHOLDS,
            control=True,
        )
    ]
    commands = {
        "install": INSTALL_COMMAND,
        "update": UPDATE_COMMAND,
        "compile": COMPILE_COMMAND,
    }
    timeouts = {"install": 240.0, "update": 240.0, "compile": 180.0}
    for operation in ("install", "update", "compile"):
        for temperature in ("cold", "warm"):
            for size_id in FIXTURE_SIZES:
                scenarios.append(
                    BenchmarkScenario(
                        id=f"{operation}.{temperature}.{size_id}",
                        operation=operation,
                        temperature=temperature,
                        fixture_size_id=size_id,
                        command=commands[operation],
                        timeout_seconds=timeouts[operation],
                        thresholds=_OPERATION_THRESHOLDS,
                    )
                )
    scenarios.append(
        BenchmarkScenario(
            id="install.live.small",
            operation="install",
            temperature="live",
            fixture_size_id="small",
            command=INSTALL_COMMAND,
            timeout_seconds=600.0,
            thresholds=_LIVE_THRESHOLDS,
            live=True,
        )
    )
    return tuple(scenarios)


_SCENARIO_SEQUENCE = _operation_scenarios()
SCENARIOS = MappingProxyType({scenario.id: scenario for scenario in _SCENARIO_SEQUENCE})
SCENARIO_IDS = tuple(SCENARIOS)

_SMOKE_SCENARIOS = (
    CONTROL_SCENARIO_ID,
    "install.cold.medium",
    "install.warm.medium",
    "update.cold.medium",
    "update.warm.medium",
    "compile.cold.medium",
    "compile.warm.medium",
)
_FULL_SCENARIOS = tuple(scenario.id for scenario in _SCENARIO_SEQUENCE if not scenario.live)

PROFILES = MappingProxyType(
    {
        "smoke": BenchmarkProfile(
            id="smoke",
            scenario_ids=_SMOKE_SCENARIOS,
            repetitions=5,
            baseline_authority=False,
        ),
        "full": BenchmarkProfile(
            id="full",
            scenario_ids=_FULL_SCENARIOS,
            repetitions=7,
            baseline_authority=True,
        ),
        "live": BenchmarkProfile(
            id="live",
            scenario_ids=("install.live.small",),
            repetitions=3,
            baseline_authority=False,
        ),
    }
)
PROFILE_IDS = tuple(PROFILES)


def get_scenario(scenario_id: str) -> BenchmarkScenario:
    """Return a scenario or fail with a bounded catalog error."""
    try:
        return SCENARIOS[scenario_id]
    except KeyError as exc:
        raise ValueError(f"Unknown benchmark scenario: {scenario_id}") from exc


def get_profile(profile_id: str) -> BenchmarkProfile:
    """Return a profile or fail with a bounded catalog error."""
    try:
        return PROFILES[profile_id]
    except KeyError as exc:
        raise ValueError(f"Unknown benchmark profile: {profile_id}") from exc


def get_fixture_size(size_id: str) -> FixtureSize:
    """Return a fixture size or fail with a bounded catalog error."""
    try:
        return FIXTURE_SIZES[size_id]
    except KeyError as exc:
        raise ValueError(f"Unknown benchmark fixture size: {size_id}") from exc


def definition_hash(scenario: BenchmarkScenario) -> str:
    """Hash every durable input that makes one measured row comparable."""
    fixture_size = (
        get_fixture_size(scenario.fixture_size_id) if scenario.fixture_size_id is not None else None
    )
    payload = {
        "catalog_version": CATALOG_VERSION,
        "scenario": {
            "id": scenario.id,
            "operation": scenario.operation,
            "temperature": scenario.temperature,
            "fixture_size_id": scenario.fixture_size_id,
            "command": list(scenario.command),
            "timeout_seconds": scenario.timeout_seconds,
            "control": scenario.control,
            "live": scenario.live,
            "thresholds": {
                "percent_floor": scenario.thresholds.percent_floor,
                "absolute_floor_ns": scenario.thresholds.absolute_floor_ns,
                "max_robust_cv": scenario.thresholds.max_robust_cv,
                "max_relative_range": scenario.thresholds.max_relative_range,
                "control_noise_percent": scenario.thresholds.control_noise_percent,
            },
        },
        "fixture_size": (
            {
                "id": fixture_size.id,
                "package_count": fixture_size.package_count,
                "primitives_per_package": fixture_size.primitives_per_package,
                "payload_bytes": fixture_size.payload_bytes,
            }
            if fixture_size is not None
            else None
        ),
        "live_dependency": LIVE_DEPENDENCY if scenario.live else None,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _validate_catalog() -> None:
    """Fail import when a catalog edit introduces an ambiguous definition."""
    if len(SCENARIOS) != len(_SCENARIO_SEQUENCE):
        raise RuntimeError("Benchmark scenario IDs must be unique")
    for profile in PROFILES.values():
        if profile.repetitions < 1:
            raise RuntimeError(f"Profile repetitions must be positive: {profile.id}")
        if len(profile.scenario_ids) != len(set(profile.scenario_ids)):
            raise RuntimeError(f"Profile scenario IDs must be unique: {profile.id}")
        for scenario_id in profile.scenario_ids:
            scenario = get_scenario(scenario_id)
            if profile.baseline_authority and profile.id != "full":
                raise RuntimeError("Only the full profile can be baseline authority")
    for scenario in SCENARIOS.values():
        if scenario.timeout_seconds <= 0:
            raise RuntimeError(f"Scenario timeout must be positive: {scenario.id}")
        if scenario.fixture_size_id is not None:
            get_fixture_size(scenario.fixture_size_id)
        if scenario.control and scenario.id != CONTROL_SCENARIO_ID:
            raise RuntimeError("The catalog supports exactly one named control row")


_validate_catalog()
