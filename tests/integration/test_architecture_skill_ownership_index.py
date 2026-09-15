"""Mutation coverage for the run-scoped skill ownership index."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component

ROOT = Path(__file__).resolve().parents[2]
RULE_ID = "install-deployment-skill-ownership-index"


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        (
            "src/apm_cli/integration/skill_ownership.py",
            "class SkillOwnershipIndex:",
            "class DisabledSkillOwnershipIndex:",
        ),
        (
            "src/apm_cli/install/phases/targets.py",
            "SkillOwnershipIndex.from_lockfile(ctx.existing_lockfile)",
            "SkillOwnershipIndex()",
        ),
        (
            "src/apm_cli/install/phases/targets.py",
            '"skill": SkillIntegrator(ctx.skill_ownership_index),',
            '"skill": SkillIntegrator(),',
        ),
        (
            "src/apm_cli/integration/skill_integrator.py",
            "ownership_index.claim(ownership_path, current_key)",
            "pass",
        ),
        (
            "src/apm_cli/integration/skill_integrator.py",
            "ownership_index.claim(rel_path, parent_name)",
            "pass",
        ),
        (
            "src/apm_cli/install/pipeline.py",
            "existing_lockfile = ctx.existing_lockfile",
            "existing_lockfile = LockFile.read(get_lockfile_path(apm_dir))",
        ),
    ],
)
def test_skill_ownership_guard_rejects_boundary_mutations(
    path: str,
    old: str,
    new: str,
) -> None:
    """Each load, injection, claim, and reuse boundary is mutation-protected."""
    source = (ROOT / path).read_text(encoding="utf-8")
    assert old in source
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)

    report = run_selected_rules(
        ROOT,
        (RULE_ID,),
        source_overrides={path: mutated},
    )

    assert report.failures == ()
    assert any(violation.rule_id == RULE_ID for violation in report.violations)


def test_skill_ownership_guard_rejects_competing_owner() -> None:
    """A second class definition cannot become a competing authority."""
    path = "src/apm_cli/integration/skill_support.py"
    source = (ROOT / path).read_text(encoding="utf-8")
    mutated = source + "\n\nclass SkillOwnershipIndex:\n    pass\n"

    report = run_selected_rules(
        ROOT,
        (RULE_ID,),
        source_overrides={path: mutated},
    )

    assert report.failures == ()
    assert any(violation.rule_id == RULE_ID for violation in report.violations)


def test_skill_ownership_guard_is_registered_and_clean() -> None:
    """The registered architecture owner passes on the real repository."""
    report = run_selected_rules(ROOT, (RULE_ID,))

    assert report.failures == ()
    assert report.violations == ()
