"""Tests for the thin benchmark matrix command-line facade."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.perf.benchmark_matrix import __main__ as cli
from scripts.perf.benchmark_matrix.catalog import get_profile
from scripts.perf.benchmark_matrix.github_baseline import (
    BaselineError,
    BaselineNotFoundError,
)
from scripts.perf.benchmark_matrix.preparation import load_preparation_report
from scripts.perf.benchmark_matrix.results import (
    BenchmarkReport,
    ComparisonReport,
    ComparisonRow,
    ComparisonStatus,
    compare_reports,
    load_report,
    write_comparison,
    write_report,
)
from tests.utils.benchmark_matrix_fixture import DEFAULT_PLATFORM, build_benchmark_report

pytestmark = pytest.mark.component


def _report() -> BenchmarkReport:
    return build_benchmark_report("smoke", default_sample_ns=1)


def _live_report() -> BenchmarkReport:
    return build_benchmark_report("live", default_sample_ns=1)


def test_run_command_accepts_workflow_argument_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The workflow-facing run command delegates profile and output exactly once."""
    captured: dict[str, object] = {}

    def fake_run_profile(
        profile_id: str,
        *,
        repository_root: Path,
        output: Path,
        preparation_output: Path,
    ) -> BenchmarkReport:
        captured.update(
            profile_id=profile_id,
            repository_root=repository_root,
            output=output,
            preparation_output=preparation_output,
        )
        return _report()

    monkeypatch.setattr(cli, "_run_profile", fake_run_profile)
    output = tmp_path / "results.json"
    preparation_output = tmp_path / "preparation.json"
    assert (
        cli.main(
            [
                "run",
                "--profile",
                "smoke",
                "--output",
                str(output),
                "--preparation-output",
                str(preparation_output),
            ]
        )
        == 0
    )
    assert captured["profile_id"] == "smoke"
    assert captured["output"] == output
    assert captured["preparation_output"] == preparation_output


def test_run_profile_keeps_preparation_out_of_product_samples(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fixture preparation and product subprocess timings use separate reports."""
    profile = get_profile("smoke")
    clock_values = iter(
        value
        for index in range(len(profile.scenario_ids) * profile.repetitions)
        for value in (index * 1_000 + 10, index * 1_000 + 110)
    )

    class FakeFactory:
        def __init__(self, repository_root: Path, *, template_root: Path) -> None:
            del repository_root, template_root

        def __enter__(self) -> FakeFactory:
            return self

        def __exit__(self, *exc_info: object) -> None:
            del exc_info

        def prepare_sample(self, scenario: object, sample_root: Path) -> object:
            del scenario, sample_root
            return object()

    class FakeRunner:
        def __init__(self, repository_root: Path) -> None:
            del repository_root

        def run_sample(self, scenario: object, fixture: object) -> object:
            del scenario, fixture
            return SimpleNamespace(elapsed_ns=777)

    (tmp_path / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="ascii")
    monkeypatch.setattr(cli, "_require_supported_platform", lambda: None)
    monkeypatch.setattr(cli, "_repository_revision", lambda root: "a" * 40)
    monkeypatch.setattr(cli, "_platform_identity", lambda: DEFAULT_PLATFORM)
    monkeypatch.setattr(cli.time, "perf_counter_ns", lambda: next(clock_values))
    monkeypatch.setattr(cli, "FixtureFactory", FakeFactory)
    monkeypatch.setattr(cli, "BenchmarkRunner", FakeRunner)

    results_path = tmp_path / "results.json"
    preparation_path = tmp_path / "preparation.json"
    cli._run_profile(
        "smoke",
        repository_root=tmp_path,
        output=results_path,
        preparation_output=preparation_path,
    )

    result_report = load_report(results_path)
    preparation_report = load_preparation_report(preparation_path)
    assert {sample for row in result_report.scenarios for sample in row.samples_ns} == {777}
    assert {sample.elapsed_ns for sample in preparation_report.samples} == {100}


def test_run_rejects_same_result_and_preparation_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Separate report paths are required before any profile work begins."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="ascii")
    output = tmp_path / "benchmark.json"

    assert (
        cli.main(
            [
                "run",
                "--profile",
                "smoke",
                "--repo-root",
                str(tmp_path),
                "--output",
                str(output),
                "--preparation-output",
                str(output),
            ]
        )
        == 2
    )
    assert "must use different paths" in capsys.readouterr().err
    assert not output.exists()


def test_compare_uses_candidate_then_baseline_and_remains_advisory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The documented positional order still passes baseline first to comparison."""
    candidate_path = tmp_path / "candidate.json"
    baseline_path = tmp_path / "baseline.json"
    candidate = _report()
    baseline = _report()
    loaded = {candidate_path: candidate, baseline_path: baseline}
    compared: list[tuple[BenchmarkReport, BenchmarkReport]] = []

    monkeypatch.setattr(cli, "load_report", lambda path, **kwargs: loaded[path])

    def fake_compare(
        baseline_report: BenchmarkReport,
        candidate_report: BenchmarkReport,
    ) -> object:
        compared.append((baseline_report, candidate_report))
        return compare_reports(baseline_report, candidate_report)

    monkeypatch.setattr(cli, "compare_reports", fake_compare)
    assert cli.main(["compare", str(candidate_path), str(baseline_path)]) == 0
    assert compared == [(baseline, candidate)]


@pytest.mark.parametrize(
    ("status", "expected_exit"),
    [
        (ComparisonStatus.STABLE, 0),
        (ComparisonStatus.IMPROVEMENT, 0),
        (ComparisonStatus.REGRESSION, 0),
        (ComparisonStatus.NOISY, 0),
        (ComparisonStatus.MISSING, 2),
        (ComparisonStatus.INCOMPATIBLE, 0),
    ],
)
def test_compare_exit_code_distinguishes_timing_from_harness_failures(
    status: ComparisonStatus,
    expected_exit: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only missing and incompatible comparison evidence fail the command."""
    report = _report()
    comparison = ComparisonReport(
        schema_version=1,
        baseline_profile="full",
        candidate_profile="smoke",
        rows=(
            ComparisonRow(
                scenario_id="startup.version",
                status=status,
                baseline_median_ns=1.0,
                candidate_median_ns=1.0,
                delta_ns=0.0,
                delta_percent=0.0,
                reason="test status",
            ),
        ),
    )
    monkeypatch.setattr(cli, "load_report", lambda path, **kwargs: report)
    monkeypatch.setattr(cli, "compare_reports", lambda baseline, candidate: comparison)

    assert (
        cli.main(
            [
                "compare",
                str(tmp_path / "candidate.json"),
                str(tmp_path / "baseline.json"),
            ]
        )
        == expected_exit
    )


def test_compare_malformed_baseline_is_a_hard_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Malformed baseline JSON fails before any advisory comparison."""
    candidate = tmp_path / "candidate.json"
    baseline = tmp_path / "baseline.json"
    candidate.write_text("{}\n", encoding="ascii")
    baseline.write_text("{\n", encoding="ascii")

    assert cli.main(["compare", str(candidate), str(baseline)]) == 2
    assert "Benchmark command failed:" in capsys.readouterr().err


def test_summary_command_renders_comparison_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Summary reads typed comparison JSON and emits Markdown."""
    path = tmp_path / "comparison.json"
    write_comparison(path, compare_reports(_report(), _report()))
    assert cli.main(["summary", str(path)]) == 0
    assert capsys.readouterr().out.startswith("# Benchmark comparison\n")


def test_summarize_results_command_renders_live_report_without_baseline(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Live observations can render a summary without fetching a baseline."""
    report = _live_report()
    report_path = tmp_path / "results.json"
    write_report(report_path, report)

    assert cli.main(["summarize-results", str(report_path)]) == 0
    output = capsys.readouterr().out
    assert output.startswith("# Benchmark results\n")
    assert "Baseline authority: `no`." in output
    assert "| install.live.small |" in output


def test_summarize_results_accepts_complete_smoke_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Bootstrap summaries work for complete hermetic reports."""
    report_path = tmp_path / "results.json"
    write_report(report_path, _report())

    assert cli.main(["summarize-results", str(report_path)]) == 0
    assert "Profile: `smoke`." in capsys.readouterr().out


def test_download_baseline_allow_missing_is_only_for_absence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bootstrap mode succeeds only for the typed no-baseline outcome."""
    destination = tmp_path / "baseline"

    def not_found(*args: object, **kwargs: object) -> Path:
        del args, kwargs
        raise BaselineNotFoundError("none")

    monkeypatch.setattr(cli, "download_latest_baseline", not_found)
    assert (
        cli.main(
            [
                "download-baseline",
                "--destination",
                str(destination),
                "--allow-missing",
            ]
        )
        == 0
    )
    assert "No compatible benchmark baseline found" in capsys.readouterr().out
    assert not (destination / "results.json").exists()

    def malformed(*args: object, **kwargs: object) -> Path:
        del args, kwargs
        raise BaselineError("malformed response")

    monkeypatch.setattr(cli, "download_latest_baseline", malformed)
    assert (
        cli.main(
            [
                "download-baseline",
                "--destination",
                str(destination),
                "--allow-missing",
            ]
        )
        == 2
    )
    assert "malformed response" in capsys.readouterr().err


def test_live_run_requires_explicit_network_acknowledgement() -> None:
    """The observational live profile cannot access the network by accident."""
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["run", "--profile", "live"])
    assert exc_info.value.code == 2


def test_unexpected_programming_error_is_not_hidden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI formats expected failures without swallowing implementation bugs."""

    def fail_unexpectedly(
        profile_id: str,
        *,
        repository_root: Path,
        output: Path,
        preparation_output: Path,
    ) -> BenchmarkReport:
        del profile_id, repository_root, output, preparation_output
        raise TypeError("programming error")

    monkeypatch.setattr(cli, "_run_profile", fail_unexpectedly)
    with pytest.raises(TypeError, match="programming error"):
        cli.main(
            [
                "run",
                "--profile",
                "smoke",
                "--output",
                str(tmp_path / "results.json"),
            ]
        )
