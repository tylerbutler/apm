"""Unit tests for the canonical benchmark matrix catalog."""

from __future__ import annotations

from dataclasses import replace

import pytest

from scripts.perf.benchmark_matrix.catalog import (
    CONTROL_SCENARIO_ID,
    FIXTURE_SIZES,
    PROFILE_IDS,
    SCENARIO_IDS,
    definition_hash,
    get_profile,
    get_scenario,
)
from scripts.perf.benchmark_matrix.results import make_scenario_result

pytestmark = pytest.mark.unit


def test_profiles_reference_unique_catalog_scenarios() -> None:
    """Every profile must route through the canonical scenario registry."""
    assert PROFILE_IDS == ("smoke", "full", "live")
    for profile_id in PROFILE_IDS:
        profile = get_profile(profile_id)
        assert len(profile.scenario_ids) == len(set(profile.scenario_ids))
        assert all(scenario_id in SCENARIO_IDS for scenario_id in profile.scenario_ids)


def test_full_profile_covers_operation_temperature_and_size_matrix() -> None:
    """Full keeps cold and warm rows for every hermetic operation and size."""
    full = get_profile("full")
    expected = {
        f"{operation}.{temperature}.{size_id}"
        for operation in ("install", "update", "compile")
        for temperature in ("cold", "warm")
        for size_id in FIXTURE_SIZES
    }
    assert CONTROL_SCENARIO_ID in full.scenario_ids
    assert expected.issubset(full.scenario_ids)


def test_smoke_is_startup_plus_medium_operation_rows() -> None:
    """Smoke uses meaningful medium work while excluding large and live rows."""
    smoke = get_profile("smoke")
    assert smoke.scenario_ids[0] == CONTROL_SCENARIO_ID
    assert all(
        scenario_id == CONTROL_SCENARIO_ID or scenario_id.endswith(".medium")
        for scenario_id in smoke.scenario_ids
    )
    assert smoke.repetitions >= 5
    assert all(not get_scenario(scenario_id).live for scenario_id in smoke.scenario_ids)


def test_only_full_profile_is_baseline_authority() -> None:
    """Only scheduled full results can become the historical baseline."""
    smoke = get_profile("smoke")
    full = get_profile("full")
    live = get_profile("live")
    assert smoke.baseline_authority is False
    assert full.baseline_authority is True
    assert live.baseline_authority is False
    assert all(get_scenario(scenario_id).live for scenario_id in live.scenario_ids)


def test_definition_hash_is_stable_and_covers_durable_inputs() -> None:
    """Changing a durable scenario input changes row compatibility."""
    scenario = get_scenario("install.cold.medium")
    original = definition_hash(scenario)

    assert definition_hash(scenario) == original
    assert len(original) == 64
    assert definition_hash(replace(scenario, command=("install",))) != original


def test_shared_rows_are_compatible_across_smoke_and_full_profiles() -> None:
    """Profile repetition counts do not fork the scenario definition."""
    smoke = get_profile("smoke")
    full = get_profile("full")
    smoke_row = make_scenario_result(
        smoke,
        "install.cold.medium",
        (1, 1, 1, 1, 1),
    )
    full_row = make_scenario_result(
        full,
        "install.cold.medium",
        (1, 1, 1, 1, 1, 1, 1),
    )
    assert smoke_row.definition_hash == full_row.definition_hash


def test_install_and_update_exercise_default_parallelism() -> None:
    """The benchmark must not override the product's parallel-download default."""
    for scenario_id in ("install.cold.medium", "update.cold.medium"):
        assert "--parallel-downloads" not in get_scenario(scenario_id).command


@pytest.mark.parametrize("lookup", [get_profile, get_scenario])
def test_unknown_catalog_id_fails_closed(lookup: object) -> None:
    """Unknown durable IDs cannot silently acquire ad hoc defaults."""
    with pytest.raises(ValueError, match="Unknown benchmark"):
        lookup("missing")  # type: ignore[operator]
