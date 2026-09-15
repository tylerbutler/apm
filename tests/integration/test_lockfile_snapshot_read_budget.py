"""Large-style install/update regression for lockfile parse count."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.deps.lockfile import LockFile
from scripts.perf.benchmark_matrix.catalog import get_scenario
from scripts.perf.benchmark_matrix.fixtures import FixtureFactory

pytestmark = pytest.mark.component
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("scenario_id", "expected_reads"),
    (
        ("install.cold.large", 1),
        ("update.cold.large", 2),
    ),
)
def test_large_pipeline_stays_within_lockfile_read_budget(
    tmp_path,
    monkeypatch,
    scenario_id: str,
    expected_reads: int,
) -> None:
    """Normal large-style runs parse at the initial and fresh-write boundaries only."""
    from apm_cli.cli import cli

    scenario = get_scenario(scenario_id)
    fixture = FixtureFactory(REPOSITORY_ROOT).prepare_sample(scenario, tmp_path / scenario_id)
    original_read = LockFile.read.__func__
    read_count = 0

    def counted_read(cls, path):
        nonlocal read_count
        read_count += 1
        return original_read(cls, path)

    monkeypatch.chdir(fixture.cwd)
    with monkeypatch.context() as scoped:
        scoped.setattr(LockFile, "read", classmethod(counted_read))
        result = CliRunner().invoke(cli, list(scenario.command), env=dict(fixture.environment))

    assert result.exit_code == 0, result.output
    assert read_count == expected_reads
    fixture.validate(stdout=result.output, stderr="")
