"""Tests for the canonical run-scoped skill ownership index."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.integration.skill_integrator import SkillIntegrator
from apm_cli.integration.skill_ownership import SkillOwnershipIndex

pytestmark = pytest.mark.component


def test_from_lockfile_builds_both_maps_in_one_pass_with_last_entry_wins() -> None:
    """One lockfile pass preserves leaf precedence and exact native path scope."""
    lockfile = LockFile()
    lockfile.add_dependency(
        LockedDependency(
            repo_url="a/first",
            deployed_files=[
                ".agents/skills/shared/",
                ".claude/skills/shared/",
            ],
        )
    )
    lockfile.add_dependency(
        LockedDependency(
            repo_url="z/last",
            deployed_files=[
                ".agents\\skills\\shared\\",
                ".github/commands/shared/",
            ],
        )
    )

    with patch.object(
        lockfile,
        "get_package_dependencies",
        wraps=lockfile.get_package_dependencies,
    ) as get_dependencies:
        index = SkillOwnershipIndex.from_lockfile(lockfile)

    get_dependencies.assert_called_once_with()
    assert index.owner_for_leaf("shared") == "z/last"
    assert index.owner_for_deployed_skill_path(".agents/skills/shared/") == "z/last"
    assert index.owner_for_deployed_skill_path(".claude/skills/shared") == "a/first"
    assert index.owner_for_deployed_skill_path(".github/commands/shared") is None


def test_claim_updates_leaf_and_exact_path_ownership_incrementally() -> None:
    """Accepted same-run materializations immediately become the current owner."""
    index = SkillOwnershipIndex()

    index.claim(".agents/skills/topic/", "owner/first")
    index.claim(".claude/skills/topic", "owner/second")

    assert index.owner_for_leaf("topic") == "owner/second"
    assert index.owner_for_deployed_skill_path(".agents/skills/topic") == "owner/first"
    assert index.owner_for_deployed_skill_path(".claude/skills/topic/") == "owner/second"
    assert index.deployed_skill_paths() == {
        ".agents/skills/topic",
        ".claude/skills/topic",
    }


def test_promoted_skill_claims_only_after_success_or_identical_acceptance(
    tmp_path: Path,
) -> None:
    """Skipped foreign content stays unclaimed; copied and identical content is claimed."""
    source_root = tmp_path / "source"
    source = source_root / "topic"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("# source", encoding="utf-8")

    target_root = tmp_path / ".agents" / "skills"
    target = target_root / "topic"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("# foreign", encoding="utf-8")
    index = SkillOwnershipIndex()

    count, deployed = SkillIntegrator._promote_sub_skills(
        source_root,
        target_root,
        "owner/package",
        ownership_index=index,
        managed_files=set(),
        force=False,
        project_root=tmp_path,
    )

    assert count == 0
    assert deployed == []
    assert index.owner_for_deployed_skill_path(".agents/skills/topic") is None

    (target / "SKILL.md").write_text("# source", encoding="utf-8")
    count, deployed = SkillIntegrator._promote_sub_skills(
        source_root,
        target_root,
        "owner/package",
        ownership_index=index,
        managed_files=set(),
        force=False,
        project_root=tmp_path,
    )

    assert count == 1
    assert deployed == [target]
    assert index.owner_for_deployed_skill_path(".agents/skills/topic") == "owner/package"


def test_standalone_integrator_loads_ownership_once_per_project(tmp_path: Path) -> None:
    """Direct callers reuse one lazily loaded index instead of rereading the lockfile."""
    loaded = SkillOwnershipIndex()
    integrator = SkillIntegrator()

    with patch.object(SkillOwnershipIndex, "load", return_value=loaded) as load:
        first = integrator._ownership_index_for(tmp_path)
        second = integrator._ownership_index_for(tmp_path)

    load.assert_called_once_with(tmp_path)
    assert first is second
