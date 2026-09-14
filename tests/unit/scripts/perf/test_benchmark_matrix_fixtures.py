"""Component tests for deterministic benchmark fixture preparation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from scripts.perf.benchmark_matrix.catalog import get_scenario
from scripts.perf.benchmark_matrix.fixtures import FixtureFactory

pytestmark = pytest.mark.component


def test_cold_install_fixture_is_hermetic_and_deterministic(tmp_path: Path) -> None:
    """Local repositories and manifests are repeatable without ambient tokens."""
    base_environment = dict(os.environ)
    base_environment["GITHUB_TOKEN"] = "must-not-survive"
    factory = FixtureFactory(tmp_path, base_environment=base_environment)
    first = factory.prepare_sample(
        get_scenario("install.cold.small"),
        tmp_path / "first",
    )
    second = factory.prepare_sample(
        get_scenario("install.cold.small"),
        tmp_path / "second",
    )

    assert len(first.expected_install_paths) == 1
    assert first.expected_install_paths[0].name == "benchmark-package-000"
    assert "GITHUB_TOKEN" not in first.environment
    assert first.environment["GIT_CONFIG_COUNT"] == "2"
    assert (first.cwd / "apm.yml").read_bytes() == (second.cwd / "apm.yml").read_bytes()
    first_origin = tmp_path / "first" / "repositories" / "benchmark-package-000.git"
    second_origin = tmp_path / "second" / "repositories" / "benchmark-package-000.git"
    assert first_origin.is_dir()
    assert second_origin.is_dir()


def test_compile_fixture_scales_local_primitives_and_validates_output(
    tmp_path: Path,
) -> None:
    """Compile rows use deterministic local source inputs sized by the catalog."""
    fixture = FixtureFactory(tmp_path).prepare_sample(
        get_scenario("compile.cold.small"),
        tmp_path / "compile",
    )
    instructions = sorted((fixture.cwd / ".apm" / "instructions").glob("*.md"))
    assert len(instructions) == 2

    required = fixture.required_paths[0]
    required.parent.mkdir(parents=True, exist_ok=True)
    marker = fixture.expected_file_markers[0][1]
    required.write_text(f"compiled {marker}\n", encoding="ascii", newline="")
    fixture.validate(stdout="", stderr="")


def test_warm_compile_mutates_source_after_setup_and_requires_new_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The timed warm compile must consume a revision created after priming."""
    factory = FixtureFactory(tmp_path)

    def fake_setup(
        args: tuple[str, ...],
        *,
        cwd: Path,
        environment: object,
    ) -> None:
        del args, environment
        (cwd / "AGENTS.md").write_text("primed output\n", encoding="ascii")

    monkeypatch.setattr(factory, "_run_setup", fake_setup)
    fixture = factory.prepare_sample(
        get_scenario("compile.warm.small"),
        tmp_path / "compile-warm",
    )
    marker = fixture.expected_file_markers[0][1]
    source = fixture.cwd / ".apm" / "instructions" / "benchmark-instruction-0000.instructions.md"
    assert marker in source.read_text(encoding="ascii")
    with pytest.raises(AssertionError, match="marker"):
        fixture.validate(stdout="", stderr="")

    (fixture.cwd / "AGENTS.md").write_text(marker + "\n", encoding="ascii")
    fixture.validate(stdout="", stderr="")


def test_update_fixture_advances_local_origins_outside_timing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Update preconditions and repository mutation happen before measurement."""
    factory = FixtureFactory(tmp_path)

    def fake_setup(
        args: tuple[str, ...],
        *,
        cwd: Path,
        environment: object,
    ) -> None:
        del args, environment
        (cwd / "apm_modules").mkdir(parents=True)

    monkeypatch.setattr(factory, "_run_setup", fake_setup)
    fixture = factory.prepare_sample(
        get_scenario("update.cold.small"),
        tmp_path / "update",
    )
    assert len(fixture.expected_updates) == 1
    assert list((tmp_path / "update" / "cache").iterdir()) == []

    expected = fixture.expected_updates[0]
    dependency = LockedDependency(
        repo_url=expected.repo_url,
        resolved_commit=expected.resolved_commit,
    )
    lockfile = LockFile()
    lockfile.add_dependency(dependency)
    lockfile.write(fixture.cwd / "apm.lock.yaml")
    content_path = expected.install_path / expected.relative_content_path
    content_path.parent.mkdir(parents=True)
    content_path.write_text(expected.content_marker + "\n", encoding="ascii")
    fixture.validate(stdout="", stderr="")

    dependency.resolved_commit = "0" * 40
    lockfile.write(fixture.cwd / "apm.lock.yaml")
    with pytest.raises(AssertionError, match="revision mismatch"):
        fixture.validate(stdout="", stderr="")


def test_update_validation_binds_each_revision_to_its_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lockfile with valid but swapped package SHAs fails correctness."""
    factory = FixtureFactory(tmp_path)
    monkeypatch.setattr(factory, "_run_setup", lambda *args, **kwargs: None)
    fixture = factory.prepare_sample(
        get_scenario("update.cold.medium"),
        tmp_path / "update-medium",
    )
    lockfile = LockFile()
    updates = fixture.expected_updates
    for index, expected in enumerate(updates):
        wrong_revision = updates[(index + 1) % len(updates)].resolved_commit
        lockfile.add_dependency(
            LockedDependency(
                repo_url=expected.repo_url,
                resolved_commit=wrong_revision,
            )
        )
        content_path = expected.install_path / expected.relative_content_path
        content_path.parent.mkdir(parents=True)
        content_path.write_text(expected.content_marker + "\n", encoding="ascii")
    lockfile.write(fixture.cwd / "apm.lock.yaml")

    with pytest.raises(AssertionError, match="revision mismatch"):
        fixture.validate(stdout="", stderr="")


def test_live_fixture_is_explicitly_nonhermetic_but_credential_free(
    tmp_path: Path,
) -> None:
    """Live rows remove the network guard without restoring ambient credentials."""
    base_environment = dict(os.environ)
    base_environment["GH_TOKEN"] = "must-not-survive"
    fixture = FixtureFactory(tmp_path, base_environment=base_environment).prepare_sample(
        get_scenario("install.live.small"),
        tmp_path / "live",
    )
    assert "GH_TOKEN" not in fixture.environment
    assert "PYTHONPATH" not in fixture.environment
    assert fixture.environment["GIT_ALLOW_PROTOCOL"] == "https:file"
    assert len(fixture.expected_install_paths) == 1
    assert fixture.expected_install_paths[0] == (
        fixture.cwd / "apm_modules" / "microsoft" / "apm-sample-package"
    )
