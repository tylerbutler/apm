"""Run-scoped lockfile snapshot architecture guard."""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.install_deployment_shared import (
    _count_re,
    _facts_for,
    _present,
    _summary,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Violation

GUARD_LOCKFILE_SNAPSHOT = "install-deployment-run-scoped-lockfile-snapshot"

_OWNER_PATH = "src/apm_cli/install/lockfile_snapshot.py"
_PIPELINE_PATH = "src/apm_cli/install/pipeline.py"
_REQUEST_PATH = "src/apm_cli/install/request.py"
_LOCKFILE_PHASE_PATH = "src/apm_cli/install/phases/lockfile.py"
_SERVICE_PATH = "src/apm_cli/install/service_integration.py"
_FUNCTION_OWNERS = (
    ("src/apm_cli/install/phases/resolve.py", "_load_lockfile"),
    ("src/apm_cli/install/integrity.py", "enforce_installed_hash_policy"),
    ("src/apm_cli/install/phases/post_deps_local.py", "run"),
    ("src/apm_cli/install/manifest_reconcile.py", "reconcile_project_deployed_state"),
    ("src/apm_cli/install/mcp/integration.py", "run_mcp_integration"),
    ("src/apm_cli/install/lsp/integration.py", "run_lsp_integration"),
    ("src/apm_cli/integration/_shared.py", "resolve_locked_apm_yml_sources"),
    ("src/apm_cli/integration/mcp_config_view.py", "_collect_transitive_compat"),
    ("src/apm_cli/integration/mcp_integrator.py", "update_lockfile"),
    ("src/apm_cli/integration/lsp_integrator.py", "collect_transitive"),
    ("src/apm_cli/integration/lsp_integrator.py", "update_lockfile"),
)
_DIRECT_READ = re.compile(r"(?:LockFile|_LF)\.(?:read|load_or_create)\(")


def _function_lines(facts: object, name: str) -> tuple[str, ...]:
    """Return lexical lines for the one named function."""
    definitions = [
        definition
        for definition in getattr(facts, "definitions", ())
        if definition.name == name and definition.kind in ("function", "async_function")
    ]
    if len(definitions) != 1:
        return ()
    definition = definitions[0]
    return tuple(getattr(facts, "lines", ()))[definition.line - 1 : definition.end_line]


def check_run_scoped_lockfile_snapshot(provider: FactsProvider) -> tuple[Violation, ...]:
    """Install orchestration must reuse one parsed lockfile snapshot."""
    rule_id = GUARD_LOCKFILE_SNAPSHOT
    paths = {
        _OWNER_PATH,
        _PIPELINE_PATH,
        _REQUEST_PATH,
        _LOCKFILE_PHASE_PATH,
        _SERVICE_PATH,
        *(path for path, _function in _FUNCTION_OWNERS),
    }
    loaded = {}
    failures: list[Violation] = []
    for path in sorted(paths):
        facts, fact_failures = _facts_for(provider, path, rule_id)
        loaded[path] = facts
        failures.extend(fact_failures)
    if failures:
        return tuple(failures)

    findings: list[Violation] = []
    owner = loaded[_OWNER_PATH]
    required_owner_shapes = (
        "class LockfileSnapshot:",
        "def load(",
        "def supplied(",
        "def resolve(",
        "def require_path(",
        "def replace(",
    )
    if any(not _present(owner, shape) for shape in required_owner_shapes):
        findings.append(
            _summary(
                rule_id,
                _OWNER_PATH,
                "LockfileSnapshot must own loaded, supplied, absent, and replacement state",
            )
        )

    pipeline = loaded[_PIPELINE_PATH]
    if not _present(
        pipeline, "LockfileSnapshot.resolve(_lock_path, lockfile_snapshot)"
    ) or not _present(pipeline, "lockfile_snapshot=lockfile_snapshot"):
        findings.append(
            _summary(
                rule_id,
                _PIPELINE_PATH,
                "install pipeline must resolve once and publish the run-scoped snapshot",
            )
        )

    if _present(loaded[_REQUEST_PATH], "lockfile_snapshot"):
        findings.append(
            _summary(
                rule_id,
                _REQUEST_PATH,
                "execution-state lockfile snapshots must not enter InstallRequest user intent",
            )
        )

    lockfile_phase = loaded[_LOCKFILE_PHASE_PATH]
    if _count_re(lockfile_phase, re.compile(r"_LF\.read\(lockfile_path\)")) != 1 or not _present(
        lockfile_phase, "self._publish_snapshot(lockfile)"
    ):
        findings.append(
            _summary(
                rule_id,
                _LOCKFILE_PHASE_PATH,
                "lockfile assembly must retain exactly one fresh comparison read and publish it",
            )
        )

    service = loaded[_SERVICE_PATH]
    if _count_re(service, re.compile(r"lockfile_snapshot=lockfile_snapshot")) != 2:
        findings.append(
            _summary(
                rule_id,
                _SERVICE_PATH,
                "service reconciliation must pass one snapshot to both MCP and LSP",
            )
        )

    for path, function_name in _FUNCTION_OWNERS:
        lines = _function_lines(loaded[path], function_name)
        if not lines:
            findings.append(
                _summary(
                    rule_id,
                    path,
                    f"snapshot-guarded function {function_name} must exist exactly once",
                )
            )
            continue
        if any(_DIRECT_READ.search(line) for line in lines):
            findings.append(
                _summary(
                    rule_id,
                    path,
                    f"{function_name} must consume LockfileSnapshot instead of rereading disk",
                )
            )

    return tuple(findings)
