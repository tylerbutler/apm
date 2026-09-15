"""Skill ownership index architecture guard."""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.install_deployment_shared import (
    _SRC_PREFIX,
    _count_re,
    _facts_for,
    _present,
    _python_paths,
    _summary,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Violation

GUARD_SKILL_OWNERSHIP = "install-deployment-skill-ownership-index"

_OWNER_PATH = "src/apm_cli/integration/skill_ownership.py"
_INTEGRATOR_PATH = "src/apm_cli/integration/skill_integrator.py"
_SUPPORT_PATH = "src/apm_cli/integration/skill_support.py"
_TARGETS_PATH = "src/apm_cli/install/phases/targets.py"
_PIPELINE_PATH = "src/apm_cli/install/pipeline.py"


def check_skill_ownership_index(provider: FactsProvider) -> tuple[Violation, ...]:
    """Skill ownership derivation and same-run claims must route through one index."""
    rule_id = GUARD_SKILL_OWNERSHIP
    paths = (_OWNER_PATH, _INTEGRATOR_PATH, _SUPPORT_PATH, _TARGETS_PATH, _PIPELINE_PATH)
    loaded = {}
    failures: list[Violation] = []
    for path in paths:
        facts, fact_failures = _facts_for(provider, path, rule_id)
        loaded[path] = facts
        failures.extend(fact_failures)
    if failures:
        return tuple(failures)

    owner = loaded[_OWNER_PATH]
    integrator = loaded[_INTEGRATOR_PATH]
    support = loaded[_SUPPORT_PATH]
    targets = loaded[_TARGETS_PATH]
    pipeline = loaded[_PIPELINE_PATH]
    findings: list[Violation] = []

    required_owner_shapes = (
        "class SkillOwnershipIndex:",
        "def from_lockfile(",
        "def claim(",
        "def owner_for_leaf(",
        "def owner_for_deployed_skill_path(",
    )
    if any(not _present(owner, shape) for shape in required_owner_shapes):
        findings.append(
            _summary(
                rule_id,
                _OWNER_PATH,
                "SkillOwnershipIndex must own lockfile derivation and mutable ownership claims",
            )
        )

    competing = []
    for path in _python_paths(provider, _SRC_PREFIX):
        if path == _OWNER_PATH:
            continue
        facts = provider.file_facts(path)
        if any(definition.name == "SkillOwnershipIndex" for definition in facts.definitions):
            competing.append(path)
    if competing:
        findings.append(
            _summary(
                rule_id,
                competing[0],
                "SkillOwnershipIndex must have exactly one class owner",
            )
        )

    if _count_re(
        targets,
        re.compile(r"SkillOwnershipIndex\.from_lockfile\(ctx\.existing_lockfile\)"),
    ) != 1 or not _present(targets, "SkillIntegrator(ctx.skill_ownership_index)"):
        findings.append(
            _summary(
                rule_id,
                _TARGETS_PATH,
                "targets phase must construct one ownership index and inject it into SkillIntegrator",
            )
        )

    if (
        _present(integrator, "LockFile.read(")
        or _present(integrator, "_native_skill_session_owners")
        or _count_re(integrator, re.compile(r"ownership_index\.claim\(")) < 3
    ):
        findings.append(
            _summary(
                rule_id,
                _INTEGRATOR_PATH,
                "SkillIntegrator must consume the index and claim only accepted materializations",
            )
        )

    if not _present(support, "ownership_index.owned_agent_skill_names()") or _present(
        support, "dep.deployed_files"
    ):
        findings.append(
            _summary(
                rule_id,
                _SUPPORT_PATH,
                "skill cleanup must consume SkillOwnershipIndex instead of deriving ownership",
            )
        )

    if (
        not _present(pipeline, "existing_lockfile = ctx.existing_lockfile")
        or _count_re(pipeline, re.compile(r"LockFile\.read\(")) != 0
        or not _present(
            pipeline,
            "lockfile_snapshot = LockfileSnapshot.resolve(_lock_path, lockfile_snapshot)",
        )
    ):
        findings.append(
            _summary(
                rule_id,
                _PIPELINE_PATH,
                "pipeline managed_files must reuse the run-scoped parsed lockfile snapshot",
            )
        )

    return tuple(findings)
