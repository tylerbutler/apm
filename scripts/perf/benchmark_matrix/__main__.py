"""Command-line entry point for the APM benchmark matrix."""

from __future__ import annotations

import argparse
import platform
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .catalog import PROFILE_IDS, RESULT_SCHEMA_VERSION, get_profile, get_scenario
from .fixtures import FixtureFactory, FixturePreparationError
from .github_baseline import (
    BaselineError,
    BaselineNotFoundError,
    download_latest_baseline,
)
from .results import (
    BenchmarkReport,
    PlatformIdentity,
    ResultFormatError,
    compare_reports,
    comparison_has_hard_failures,
    comparison_summary,
    comparison_text,
    load_comparison,
    load_report,
    make_scenario_result,
    report_summary,
    write_comparison,
    write_report,
)
from .runner import BenchmarkRunError, BenchmarkRunner


class BenchmarkConfigurationError(RuntimeError):
    """Raised when the host checkout cannot run an authoritative benchmark."""


def main(argv: list[str] | None = None) -> int:
    """Run one benchmark profile, compare reports, or download a baseline."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            if args.profile == "live" and not args.allow_network:
                parser.error("live benchmarks require --allow-network")
            report = _run_profile(
                args.profile,
                repository_root=args.repo_root,
                output=args.output,
            )
            sys.stdout.write(f"Wrote {len(report.scenarios)} benchmark rows to {args.output}\n")
            return 0
        if args.command == "compare":
            comparison = compare_reports(
                load_report(args.baseline, require_current=False),
                load_report(args.candidate),
            )
            if args.output is None:
                sys.stdout.write(comparison_text(comparison))
            else:
                write_comparison(args.output, comparison)
                sys.stdout.write(f"Wrote benchmark comparison to {args.output}\n")
            # Timing movement is advisory; unusable comparison rows are not.
            return 2 if comparison_has_hard_failures(comparison) else 0
        if args.command == "summary":
            sys.stdout.write(comparison_summary(load_comparison(args.comparison)))
            return 0
        if args.command == "summarize-results":
            sys.stdout.write(report_summary(load_report(args.results)))
            return 0
        if args.command == "download-baseline":
            try:
                result = download_latest_baseline(
                    args.destination,
                    repository=args.repository,
                    current_run_id=args.current_run_id,
                )
            except BaselineNotFoundError:
                if not args.allow_missing:
                    raise
                sys.stdout.write(
                    "No compatible benchmark baseline found; continuing without comparison.\n"
                )
                return 0
            sys.stdout.write(f"Downloaded benchmark baseline to {result}\n")
            return 0
    except (
        BaselineError,
        BenchmarkConfigurationError,
        BenchmarkRunError,
        FixturePreparationError,
        OSError,
        ResultFormatError,
        ValueError,
        subprocess.SubprocessError,
    ) as exc:
        sys.stderr.write(
            f"Benchmark command failed: {exc}\n"
            "Fix the reported fixture, command, or data error and rerun the same command.\n"
        )
        return 2
    parser.error("a benchmark command is required")
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.perf.benchmark_matrix",
        description="Run and compare the Linux x86_64 APM source-CLI benchmark matrix.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    repository_root = Path(__file__).resolve().parents[3]
    run_parser = subparsers.add_parser(
        "run",
        help="Run one catalog-owned smoke, full, or live profile.",
    )
    run_parser.add_argument("--profile", choices=PROFILE_IDS, required=True)
    run_parser.add_argument(
        "--repo-root",
        type=Path,
        default=repository_root,
        help="APM repository root containing pyproject.toml.",
    )
    run_parser.add_argument(
        "--output",
        type=Path,
        default=Path("results.json"),
        help="Result JSON path.",
    )
    run_parser.add_argument(
        "--allow-network",
        action="store_true",
        help="Acknowledge public network access for the non-authoritative live profile.",
    )

    compare_parser = subparsers.add_parser(
        "compare",
        help="Compare two result files; timing regressions remain advisory.",
    )
    compare_parser.add_argument("candidate", type=Path)
    compare_parser.add_argument("baseline", type=Path)
    compare_parser.add_argument("--output", type=Path)

    summary_parser = subparsers.add_parser(
        "summary",
        help="Render a comparison JSON document as GitHub-flavored Markdown.",
    )
    summary_parser.add_argument("comparison", type=Path)

    results_summary_parser = subparsers.add_parser(
        "summarize-results",
        help="Render a result JSON document as GitHub-flavored Markdown.",
    )
    results_summary_parser.add_argument("results", type=Path)

    baseline_parser = subparsers.add_parser(
        "download-baseline",
        help="Download the exact prior successful main benchmark artifact with gh.",
    )
    baseline_parser.add_argument("--repository", default="microsoft/apm")
    baseline_parser.add_argument("--current-run-id", type=int)
    baseline_parser.add_argument("--destination", type=Path, required=True)
    baseline_parser.add_argument(
        "--allow-missing",
        action="store_true",
        help="Exit successfully only when no compatible trusted baseline exists.",
    )
    return parser


def _run_profile(
    profile_id: str,
    *,
    repository_root: Path,
    output: Path,
) -> BenchmarkReport:
    _require_supported_platform()
    repository_root = repository_root.resolve()
    if not (repository_root / "pyproject.toml").is_file():
        raise BenchmarkConfigurationError(
            f"Repository root does not contain pyproject.toml: {repository_root}"
        )
    profile_definition = get_profile(profile_id)
    runner = BenchmarkRunner(repository_root)
    fixture_factory = FixtureFactory(repository_root)
    rows = []
    with tempfile.TemporaryDirectory(prefix=f"apm-benchmark-{profile_id}-") as temp_dir:
        matrix_root = Path(temp_dir)
        for scenario_id in profile_definition.scenario_ids:
            scenario = get_scenario(scenario_id)
            samples: list[int] = []
            for repetition in range(profile_definition.repetitions):
                sample_root = matrix_root / scenario.id / f"sample-{repetition:02d}"
                sample_root.parent.mkdir(parents=True, exist_ok=True)
                fixture = fixture_factory.prepare_sample(scenario, sample_root)
                result = runner.run_sample(scenario, fixture)
                samples.append(result.elapsed_ns)
            rows.append(
                make_scenario_result(
                    profile_definition,
                    scenario.id,
                    tuple(samples),
                )
            )
    report = BenchmarkReport(
        schema_version=RESULT_SCHEMA_VERSION,
        profile=profile_definition.id,
        baseline_authority=profile_definition.baseline_authority,
        generated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        repository_revision=_repository_revision(repository_root),
        platform=_platform_identity(),
        scenarios=tuple(rows),
    )
    write_report(output, report)
    return report


def _platform_identity() -> PlatformIdentity:
    machine = platform.machine().lower()
    if machine == "amd64":
        machine = "x86_64"
    return PlatformIdentity(
        system=platform.system().lower(),
        machine=machine,
        python_implementation=platform.python_implementation(),
        python_version=platform.python_version(),
    )


def _require_supported_platform() -> None:
    identity = _platform_identity()
    if (identity.system, identity.machine) != ("linux", "x86_64"):
        raise BenchmarkConfigurationError(
            "Benchmark baselines require Linux x86_64; "
            f"detected {identity.system} {identity.machine}"
        )


def _repository_revision(repository_root: Path) -> str:
    result = subprocess.run(  # noqa: S603
        ("git", "-C", str(repository_root), "rev-parse", "HEAD"),  # noqa: S607
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    revision = result.stdout.strip()
    if result.returncode != 0 or len(revision) != 40:
        raise BenchmarkConfigurationError(
            "Cannot determine repository revision; "
            f"returncode={result.returncode}, stderr={result.stderr!r}"
        )
    return revision


if __name__ == "__main__":
    raise SystemExit(main())
