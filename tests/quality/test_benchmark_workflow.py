"""Semantic contracts for the benchmark matrix GitHub Actions workflow."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from pathlib import Path

import pytest

from tests.workflow_contracts import (
    WorkflowNode,
    assert_exact_command,
    load_workflow,
    shell_commands,
    workflow_job,
    workflow_step,
    workflow_step_index,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github/workflows/benchmark.yml"
MERGE_GATE_PATH = REPO_ROOT / ".github/workflows/merge-gate.yml"

DAILY_CRON = "23 4 * * *"
WEEKLY_CRON = "47 5 * * 0"
PROFILES = ("smoke", "full", "live")
HERMITIC_PROFILES = ("smoke", "full")
HARNESS_PREFIX = [
    "uv",
    "run",
    "--frozen",
    "--no-sync",
    "python",
    "-m",
    "scripts.perf.benchmark_matrix",
]
ACTION_PINS = {
    "Checkout": "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
    "Set up Python": "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97",
    "Install uv": "astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d",
}
ARTIFACT_PIN = "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
ARTIFACT_NAMES = {
    "smoke": "benchmark-smoke",
    "full": (
        "${{ github.ref == 'refs/heads/main' && "
        "'benchmark-full-main' || 'benchmark-full-dispatch' }}"
    ),
    "live": "benchmark-live",
}
RETENTION_DAYS = {"smoke": 14, "full": 90, "live": 30}
COMPARED_OUTPUTS = {
    "smoke": "results.json\ncomparison.json\nsummary.md\n",
    "full": "results.json\ncomparison.json\nsummary.md\n",
}
DIRECT_OUTPUTS = "results.json\nsummary.md\n"
BASELINE_PRESENT_IF = "${{ hashFiles('baseline/results.json') != '' }}"
BASELINE_MISSING_IF = "${{ hashFiles('baseline/results.json') == '' }}"


def _steps(job: WorkflowNode) -> list[WorkflowNode]:
    steps = job.get("steps")
    assert isinstance(steps, list)
    assert all(isinstance(step, dict) for step in steps)
    return steps


def _string_values(value: object) -> tuple[str, ...]:
    """Return every nested string value from a parsed workflow node."""
    if isinstance(value, str):
        return (value,)
    if isinstance(value, dict):
        return tuple(text for child in value.values() for text in _string_values(child))
    if isinstance(value, list):
        return tuple(text for child in value for text in _string_values(child))
    return ()


def _assert_trigger_contract(workflow: WorkflowNode) -> None:
    triggers = workflow["on"]
    assert isinstance(triggers, dict)
    assert set(triggers) == {"pull_request", "schedule", "workflow_dispatch"}
    assert triggers["pull_request"] == {"branches": ["main"]}
    assert triggers["schedule"] == [{"cron": DAILY_CRON}, {"cron": WEEKLY_CRON}]

    dispatch = triggers["workflow_dispatch"]
    assert isinstance(dispatch, dict)
    assert dispatch == {
        "inputs": {
            "profile": {
                "description": "Benchmark profile to run",
                "required": True,
                "default": "smoke",
                "type": "choice",
                "options": ["smoke", "full", "live", "all"],
            }
        }
    }

    smoke_if = workflow_job(workflow, "smoke")["if"]
    full_if = workflow_job(workflow, "full")["if"]
    live_if = workflow_job(workflow, "live")["if"]
    assert smoke_if == (
        "github.event_name == 'pull_request' || "
        "(github.event_name == 'workflow_dispatch' && "
        "(inputs.profile == 'smoke' || inputs.profile == 'all'))"
    )
    assert full_if == (
        "(github.event_name == 'schedule' && "
        f"github.event.schedule == '{DAILY_CRON}') || "
        "(github.event_name == 'workflow_dispatch' && "
        "(inputs.profile == 'full' || inputs.profile == 'all'))"
    )
    assert live_if == (
        "(github.event_name == 'schedule' && "
        f"github.event.schedule == '{WEEKLY_CRON}') || "
        "(github.event_name == 'workflow_dispatch' && "
        "(inputs.profile == 'live' || inputs.profile == 'all'))"
    )


def _assert_runtime_and_action_contract(workflow: WorkflowNode) -> None:
    assert workflow["permissions"] == {"contents": "read", "actions": "read"}
    assert workflow["env"] == {"PYTHON_VERSION": "3.12"}
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    assert set(jobs) == set(PROFILES)

    for profile in PROFILES:
        job = workflow_job(workflow, profile)
        checkout = workflow_step(job, "Checkout")
        assert checkout["with"] == {"persist-credentials": False}
        assert job["runs-on"] == "ubuntu-24.04"
        assert "permissions" not in job
        for step_name, action in ACTION_PINS.items():
            step = workflow_step(job, step_name)
            assert step["uses"] == action
        action_steps = [step["uses"] for step in _steps(job) if isinstance(step.get("uses"), str)]
        expected_actions = [*ACTION_PINS.values(), ARTIFACT_PIN]
        if profile in HERMITIC_PROFILES:
            expected_actions.append(ARTIFACT_PIN)
        assert Counter(action_steps) == Counter(expected_actions)
        assert workflow_step(job, "Set up Python")["with"] == {
            "python-version": "${{ env.PYTHON_VERSION }}"
        }
        assert workflow_step(job, "Install uv")["with"] == {"enable-cache": True}
        assert_exact_command(
            shell_commands(workflow_step(job, "Install frozen development dependencies")),
            ["uv", "sync", "--frozen", "--extra", "dev"],
            label=f"{profile} dependency setup",
        )


def _assert_harness_delegation(workflow: WorkflowNode) -> None:
    forbidden_workflow_logic = ("--scenario", "--threshold", "--fail-on-regression")
    workflow_text = "\n".join(_string_values(workflow))
    assert all(token not in workflow_text for token in forbidden_workflow_logic)

    for profile in HERMITIC_PROFILES:
        job = workflow_job(workflow, profile)
        run_step = workflow_step(job, f"Run {profile} benchmark matrix")
        compare_step = workflow_step(job, "Compare with full-main baseline")
        comparison_summary_step = workflow_step(job, "Render comparison summary")
        direct_summary_step = workflow_step(job, "Render no-baseline summary")

        run_args = ["run", "--profile", profile, "--output", "results.json"]
        assert shell_commands(run_step) == [[*HARNESS_PREFIX, *run_args]]
        assert compare_step.get("if") == BASELINE_PRESENT_IF
        assert shell_commands(compare_step) == [
            [
                *HARNESS_PREFIX,
                "compare",
                "results.json",
                "baseline/results.json",
                ">",
                "comparison.json",
            ]
        ]
        assert comparison_summary_step.get("if") == BASELINE_PRESENT_IF
        assert shell_commands(comparison_summary_step) == [
            [*HARNESS_PREFIX, "summary", "comparison.json", ">", "summary.md"],
            ["cat", "summary.md", ">>", "$GITHUB_STEP_SUMMARY"],
        ]
        assert direct_summary_step.get("if") == BASELINE_MISSING_IF
        assert shell_commands(direct_summary_step) == [
            [*HARNESS_PREFIX, "summarize-results", "results.json", ">", "summary.md"],
            ["cat", "summary.md", ">>", "$GITHUB_STEP_SUMMARY"],
        ]

        harness_commands = [
            command
            for step in _steps(job)
            if "scripts.perf.benchmark_matrix" in str(step.get("run", ""))
            if isinstance(step.get("run"), str)
            for command in shell_commands(step)
            if command[: len(HARNESS_PREFIX)] == HARNESS_PREFIX
        ]
        assert len(harness_commands) == 5
        assert all("--frozen" in command for command in harness_commands)
        assert all("--no-sync" in command for command in harness_commands)

    live = workflow_job(workflow, "live")
    assert shell_commands(workflow_step(live, "Run live benchmark matrix")) == [
        [
            *HARNESS_PREFIX,
            "run",
            "--profile",
            "live",
            "--allow-network",
            "--output",
            "results.json",
        ]
    ]
    assert shell_commands(workflow_step(live, "Render live benchmark summary")) == [
        [*HARNESS_PREFIX, "summarize-results", "results.json", ">", "summary.md"],
        ["cat", "summary.md", ">>", "$GITHUB_STEP_SUMMARY"],
    ]
    live_harness_commands = [
        command
        for step in _steps(live)
        if "scripts.perf.benchmark_matrix" in str(step.get("run", ""))
        if isinstance(step.get("run"), str)
        for command in shell_commands(step)
        if command[: len(HARNESS_PREFIX)] == HARNESS_PREFIX
    ]
    assert len(live_harness_commands) == 2
    assert all("--frozen" in command for command in live_harness_commands)
    assert all("--no-sync" in command for command in live_harness_commands)


def _assert_baseline_and_artifact_contract(workflow: WorkflowNode) -> None:
    for profile in HERMITIC_PROFILES:
        job = workflow_job(workflow, profile)
        baseline_step = workflow_step(job, "Fetch previous full-main baseline")
        compared_upload = workflow_step(
            job,
            f"Upload {profile} benchmark artifacts (compared)",
        )
        bootstrap_upload = workflow_step(
            job,
            f"Upload {profile} benchmark artifacts (bootstrap)",
        )
        baseline_script = baseline_step["run"]
        assert isinstance(baseline_script, str)
        assert baseline_step["env"] == {"GH_TOKEN": "${{ github.token }}"}
        assert shell_commands(baseline_step) == [
            [
                *HARNESS_PREFIX,
                "download-baseline",
                "--repository",
                "${{ github.repository }}",
                "--current-run-id",
                "${{ github.run_id }}",
                "--destination",
                "baseline",
                "--allow-missing",
            ]
        ]
        assert workflow_step_index(job, "Fetch previous full-main baseline") < (
            workflow_step_index(job, f"Run {profile} benchmark matrix")
        )
        assert workflow_step_index(job, "Fetch previous full-main baseline") < (
            workflow_step_index(job, f"Upload {profile} benchmark artifacts (compared)")
        )
        assert workflow_step_index(job, f"Run {profile} benchmark matrix") < (
            workflow_step_index(job, "Compare with full-main baseline")
        )
        assert workflow_step_index(job, "Compare with full-main baseline") < (
            workflow_step_index(job, "Render comparison summary")
        )
        assert workflow_step_index(job, "Render comparison summary") < (
            workflow_step_index(job, f"Upload {profile} benchmark artifacts (compared)")
        )
        assert workflow_step_index(job, f"Run {profile} benchmark matrix") < (
            workflow_step_index(job, "Render no-baseline summary")
        )
        assert workflow_step_index(job, "Render no-baseline summary") < (
            workflow_step_index(job, f"Upload {profile} benchmark artifacts (bootstrap)")
        )

        assert compared_upload["if"] == BASELINE_PRESENT_IF
        assert compared_upload["uses"] == ARTIFACT_PIN
        assert compared_upload["with"] == {
            "name": ARTIFACT_NAMES[profile],
            "path": COMPARED_OUTPUTS[profile],
            "if-no-files-found": "error",
            "retention-days": RETENTION_DAYS[profile],
        }
        assert bootstrap_upload["if"] == BASELINE_MISSING_IF
        assert bootstrap_upload["uses"] == ARTIFACT_PIN
        assert bootstrap_upload["with"] == {
            "name": ARTIFACT_NAMES[profile],
            "path": DIRECT_OUTPUTS,
            "if-no-files-found": "error",
            "retention-days": RETENTION_DAYS[profile],
        }
        assert "continue-on-error" not in compared_upload
        assert "continue-on-error" not in bootstrap_upload

    live = workflow_job(workflow, "live")
    live_upload = workflow_step(live, "Upload live benchmark artifacts")
    assert live_upload["uses"] == ARTIFACT_PIN
    assert live_upload["with"] == {
        "name": ARTIFACT_NAMES["live"],
        "path": DIRECT_OUTPUTS,
        "if-no-files-found": "error",
        "retention-days": RETENTION_DAYS["live"],
    }
    assert workflow_step_index(live, "Run live benchmark matrix") < workflow_step_index(
        live,
        "Render live benchmark summary",
    )
    assert workflow_step_index(live, "Render live benchmark summary") < workflow_step_index(
        live,
        "Upload live benchmark artifacts",
    )

    assert RETENTION_DAYS["smoke"] < RETENTION_DAYS["full"]
    assert RETENTION_DAYS["live"] < RETENTION_DAYS["full"]
    assert len(set(ARTIFACT_NAMES.values())) == len(PROFILES)


def _assert_failure_and_live_isolation(workflow: WorkflowNode) -> None:
    for profile in ("smoke", "full"):
        job = workflow_job(workflow, profile)
        assert "continue-on-error" not in job
        for step in _steps(job):
            assert "continue-on-error" not in step
            run = step.get("run")
            assert not isinstance(run, str) or "|| true" not in run

    live = workflow_job(workflow, "live")
    assert live["continue-on-error"] is True
    assert ARTIFACT_NAMES["live"] != "benchmark-full-main"
    live_text = "\n".join(_string_values(live))
    for forbidden in (
        "download-baseline",
        "baseline/results.json",
        "comparison.json",
        "benchmark-full-main",
    ):
        assert forbidden not in live_text
    assert all("baseline" not in str(step.get("name", "")).lower() for step in _steps(live))
    assert all("compare" not in str(step.get("name", "")).lower() for step in _steps(live))


def _assert_merge_gate_isolation(merge_gate: WorkflowNode) -> None:
    wait_step = workflow_step(
        workflow_job(merge_gate, "gate"),
        "Wait for all required checks",
    )
    expected_checks = wait_step["env"]["EXPECTED_CHECKS"]
    assert isinstance(expected_checks, str)
    assert "benchmark" not in expected_checks.lower()


def _assert_contract(workflow: WorkflowNode, merge_gate: WorkflowNode) -> None:
    assert workflow["name"] == "Benchmark Matrix"
    _assert_trigger_contract(workflow)
    _assert_runtime_and_action_contract(workflow)
    _assert_harness_delegation(workflow)
    _assert_baseline_and_artifact_contract(workflow)
    _assert_failure_and_live_isolation(workflow)
    _assert_merge_gate_isolation(merge_gate)


@pytest.fixture
def benchmark_workflow() -> WorkflowNode:
    return load_workflow(WORKFLOW_PATH)


@pytest.fixture
def merge_gate() -> WorkflowNode:
    return load_workflow(MERGE_GATE_PATH)


def test_benchmark_workflow_contract(
    benchmark_workflow: WorkflowNode,
    merge_gate: WorkflowNode,
) -> None:
    _assert_contract(benchmark_workflow, merge_gate)


def test_profile_substitution_fails(
    benchmark_workflow: WorkflowNode,
) -> None:
    mutated = deepcopy(benchmark_workflow)
    step = workflow_step(workflow_job(mutated, "smoke"), "Run smoke benchmark matrix")
    step["run"] = step["run"].replace("--profile smoke", "--profile invented")

    with pytest.raises(AssertionError):
        _assert_harness_delegation(mutated)


def test_threshold_logic_added_to_workflow_fails(
    benchmark_workflow: WorkflowNode,
) -> None:
    mutated = deepcopy(benchmark_workflow)
    step = workflow_step(workflow_job(mutated, "full"), "Run full benchmark matrix")
    step["run"] += " --threshold-percent 5"

    with pytest.raises(AssertionError):
        _assert_harness_delegation(mutated)


def test_baseline_after_upload_fails(
    benchmark_workflow: WorkflowNode,
) -> None:
    mutated = deepcopy(benchmark_workflow)
    job = workflow_job(mutated, "full")
    steps = _steps(job)
    baseline_index = workflow_step_index(job, "Fetch previous full-main baseline")
    upload_index = workflow_step_index(job, "Upload full benchmark artifacts (compared)")
    steps[baseline_index], steps[upload_index] = steps[upload_index], steps[baseline_index]

    with pytest.raises(AssertionError):
        _assert_baseline_and_artifact_contract(mutated)


def test_allow_missing_baseline_flag_dropped_fails(
    benchmark_workflow: WorkflowNode,
) -> None:
    mutated = deepcopy(benchmark_workflow)
    step = workflow_step(
        workflow_job(mutated, "smoke"),
        "Fetch previous full-main baseline",
    )
    step["run"] = step["run"].replace(" --allow-missing", "")

    with pytest.raises(AssertionError):
        _assert_baseline_and_artifact_contract(mutated)


def test_bootstrap_compare_becomes_unconditional_fails(
    benchmark_workflow: WorkflowNode,
) -> None:
    mutated = deepcopy(benchmark_workflow)
    step = workflow_step(
        workflow_job(mutated, "smoke"),
        "Compare with full-main baseline",
    )
    step.pop("if")

    with pytest.raises(AssertionError):
        _assert_harness_delegation(mutated)


def test_bootstrap_upload_includes_comparison_fails(
    benchmark_workflow: WorkflowNode,
) -> None:
    mutated = deepcopy(benchmark_workflow)
    upload = workflow_step(
        workflow_job(mutated, "full"),
        "Upload full benchmark artifacts (bootstrap)",
    )
    upload["with"]["path"] = COMPARED_OUTPUTS["full"]

    with pytest.raises(AssertionError):
        _assert_baseline_and_artifact_contract(mutated)


def test_live_baseline_fetch_fails(
    benchmark_workflow: WorkflowNode,
) -> None:
    mutated = deepcopy(benchmark_workflow)
    live = workflow_job(mutated, "live")
    smoke_baseline = workflow_step(
        workflow_job(mutated, "smoke"),
        "Fetch previous full-main baseline",
    )
    _steps(live).insert(4, deepcopy(smoke_baseline))

    with pytest.raises(AssertionError):
        _assert_failure_and_live_isolation(mutated)


def test_live_comparison_artifact_fails(
    benchmark_workflow: WorkflowNode,
) -> None:
    mutated = deepcopy(benchmark_workflow)
    upload = workflow_step(
        workflow_job(mutated, "live"),
        "Upload live benchmark artifacts",
    )
    upload["with"]["path"] = COMPARED_OUTPUTS["smoke"]

    with pytest.raises(AssertionError):
        _assert_baseline_and_artifact_contract(mutated)


def test_live_becomes_blocking_fails(
    benchmark_workflow: WorkflowNode,
) -> None:
    mutated = deepcopy(benchmark_workflow)
    workflow_job(mutated, "live")["continue-on-error"] = False

    with pytest.raises(AssertionError):
        _assert_failure_and_live_isolation(mutated)


def test_full_retention_shortened_fails(
    benchmark_workflow: WorkflowNode,
) -> None:
    mutated = deepcopy(benchmark_workflow)
    upload = workflow_step(
        workflow_job(mutated, "full"),
        "Upload full benchmark artifacts (compared)",
    )
    upload["with"]["retention-days"] = 30

    with pytest.raises(AssertionError):
        _assert_baseline_and_artifact_contract(mutated)


def test_benchmark_added_to_merge_gate_fails(
    merge_gate: WorkflowNode,
) -> None:
    mutated = deepcopy(merge_gate)
    wait_step = workflow_step(
        workflow_job(mutated, "gate"),
        "Wait for all required checks",
    )
    wait_step["env"]["EXPECTED_CHECKS"] += ",Benchmark Smoke"

    with pytest.raises(AssertionError):
        _assert_merge_gate_isolation(mutated)
