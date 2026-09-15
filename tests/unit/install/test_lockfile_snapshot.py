"""Regression coverage for run-scoped parsed lockfile state."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.lockfile_snapshot import LockfileSnapshot
from apm_cli.install.phases.lockfile import LockfileBuilder


def _locked(repo_url: str, resolved_ref: str) -> LockedDependency:
    return LockedDependency(
        repo_url=repo_url,
        resolved_ref=resolved_ref,
        resolved_commit=resolved_ref,
    )


def test_known_absent_snapshot_does_not_reread_for_transitive_sources(tmp_path) -> None:
    """Known absence must preserve the unlocked fallback without disk I/O."""
    from apm_cli.integration._shared import resolve_locked_apm_yml_sources

    modules = tmp_path / "apm_modules"
    modules.mkdir()
    lock_path = tmp_path / "apm.lock.yaml"
    snapshot = LockfileSnapshot.supplied(lock_path, None)

    with patch(
        "apm_cli.deps.lockfile.LockFile.read",
        side_effect=AssertionError("known absence must not reread"),
    ):
        sources, direct = resolve_locked_apm_yml_sources(
            modules,
            lock_path,
            lockfile_snapshot=snapshot,
        )

    assert sources is None
    assert direct == set()


def test_transitive_collectors_use_current_snapshot_instead_of_stale_disk(tmp_path) -> None:
    """MCP and LSP traversal must follow the generated lockfile object."""
    from apm_cli.integration.lsp_integrator import LSPIntegrator
    from apm_cli.integration.mcp_integrator import MCPIntegrator

    modules = tmp_path / "apm_modules"
    current_dir = modules / "acme" / "current"
    current_dir.mkdir(parents=True)
    (current_dir / "apm.yml").write_text(
        "name: current\n"
        "version: 1.0.0\n"
        "dependencies:\n"
        "  mcp:\n"
        "    - ghcr.io/acme/current\n"
        "  lsp:\n"
        "    - name: current-lsp\n"
        "      command: current-lsp\n"
        "      extensionToLanguage:\n"
        "        .py: python\n",
        encoding="utf-8",
    )
    stale_dir = modules / "acme" / "stale"
    stale_dir.mkdir(parents=True)
    (stale_dir / "apm.yml").write_text(
        "name: stale\nversion: 1.0.0\n",
        encoding="utf-8",
    )

    lock_path = tmp_path / "apm.lock.yaml"
    stale = LockFile()
    stale.add_dependency(_locked("acme/stale", "a" * 40))
    stale.write(lock_path)
    current = LockFile()
    current.add_dependency(_locked("acme/current", "b" * 40))
    snapshot = LockfileSnapshot.supplied(lock_path, current)

    with patch(
        "apm_cli.deps.lockfile.LockFile.read",
        side_effect=AssertionError("supplied transitive snapshot must not reread"),
    ):
        mcp = MCPIntegrator.collect_transitive(
            modules,
            lock_path,
            lockfile_snapshot=snapshot,
        )
        lsp = LSPIntegrator.collect_transitive(
            modules,
            lock_path,
            lockfile_snapshot=snapshot,
        )

    assert [dependency.name for dependency in mcp] == ["ghcr.io/acme/current"]
    assert [dependency.name for dependency in lsp] == ["current-lsp"]


def test_partial_write_freshly_merges_concurrent_lockfile_state(tmp_path) -> None:
    """The one fresh comparison read must retain concurrent partial-install state."""
    lock_path = tmp_path / "apm.lock.yaml"
    pre_run = LockFile()
    pre_run.add_dependency(_locked("acme/original", "a" * 40))
    pre_run.write(lock_path)

    concurrent = LockFile()
    concurrent.add_dependency(_locked("acme/original", "a" * 40))
    concurrent.add_dependency(_locked("acme/concurrent", "b" * 40))
    concurrent.write(lock_path)

    candidate = LockFile()
    candidate.add_dependency(_locked("acme/selected", "c" * 40))
    ctx = SimpleNamespace(only_packages=["acme/selected"], logger=MagicMock())
    builder = LockfileBuilder(ctx)

    original_read = LockFile.read.__func__
    read_count = 0

    def counted_read(cls, path):
        nonlocal read_count
        read_count += 1
        return original_read(cls, path)

    with patch.object(LockFile, "read", classmethod(counted_read)):
        current = builder._write_if_changed(candidate, lock_path, LockFile)

    assert read_count == 1
    assert set(current.dependencies) == {
        "acme/original",
        "acme/concurrent",
        "acme/selected",
    }
    persisted = LockFile.read(lock_path)
    assert persisted is not None
    assert set(persisted.dependencies) == set(current.dependencies)


def test_manifest_reconcile_uses_supplied_snapshot_without_reread(tmp_path) -> None:
    """Reconciliation mutates and persists the supplied post-build lockfile."""
    from apm_cli.install.manifest_reconcile import reconcile_project_deployed_state

    lock_path = tmp_path / "apm.lock.yaml"
    lockfile = LockFile()
    lockfile.write(lock_path)
    snapshot = LockfileSnapshot.supplied(lock_path, lockfile)

    def mutate_state(**kwargs) -> bool:
        kwargs["lockfile"].mcp_servers = ["reconciled"]
        return True

    with (
        patch(
            "apm_cli.deps.lockfile.LockFile.read",
            side_effect=AssertionError("supplied reconciliation snapshot must not reread"),
        ),
        patch(
            "apm_cli.install.manifest_reconcile.declared_target_profiles",
            return_value=[],
        ),
        patch(
            "apm_cli.integration.targets.active_targets",
            return_value=[],
        ),
        patch(
            "apm_cli.install.manifest_reconcile.reconcile_deployed_state",
            side_effect=mutate_state,
        ),
    ):
        changed = reconcile_project_deployed_state(
            tmp_path,
            explicit_target=None,
            lockfile_snapshot=snapshot,
        )

    assert changed is True
    assert snapshot.lockfile is lockfile
    assert snapshot.lockfile.mcp_servers == ["reconciled"]
    persisted = LockFile.read(lock_path)
    assert persisted is not None
    assert persisted.mcp_servers == ["reconciled"]
