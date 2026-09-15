"""Component tests for deterministic benchmark fixture preparation."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.utils.yaml_io import load_yaml
from scripts.perf.benchmark_matrix.catalog import get_scenario
from scripts.perf.benchmark_matrix.fixtures import (
    FixtureFactory,
    FixturePreparationError,
)

pytestmark = pytest.mark.component


def _tree_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _origin_revision(origin: Path, environment: object) -> str:
    assert isinstance(environment, dict)
    result = subprocess.run(
        ("git", "--git-dir", str(origin), "rev-parse", "refs/heads/main"),
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return result.stdout.strip()


def _git_tree_paths(origin: Path, revision: str) -> tuple[str, ...]:
    result = subprocess.run(
        ("git", "--git-dir", str(origin), "ls-tree", "-r", "--name-only", revision),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return tuple(result.stdout.splitlines())


def _git_file_bytes(origin: Path, revision: str, relative_path: str) -> bytes:
    result = subprocess.run(
        ("git", "--git-dir", str(origin), "show", f"{revision}:{relative_path}"),
        capture_output=True,
        check=True,
        timeout=30,
    )
    return result.stdout


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


def test_size_template_is_built_once_and_reused_across_scenarios(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Install and compile samples of one size share one immutable build."""
    factory = FixtureFactory(tmp_path, template_root=tmp_path / "templates")
    original = factory._build_template
    built_sizes: list[str] = []

    def wrapped(size: object):
        built_sizes.append(size.id)
        return original(size)

    monkeypatch.setattr(factory, "_build_template", wrapped)
    factory.prepare_sample(get_scenario("install.cold.small"), tmp_path / "install-a")
    factory.prepare_sample(get_scenario("compile.cold.small"), tmp_path / "compile")
    factory.prepare_sample(get_scenario("install.cold.medium"), tmp_path / "install-medium")
    factory.prepare_sample(get_scenario("install.cold.small"), tmp_path / "install-b")

    assert built_sizes == ["small", "medium"]


def test_materialized_samples_are_mutable_isolated_and_not_hardlinked(
    tmp_path: Path,
) -> None:
    """Every sample receives distinct writable payload, origin, and cache roots."""
    factory = FixtureFactory(tmp_path, template_root=tmp_path / "templates")
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first = factory.prepare_sample(get_scenario("install.cold.small"), first_root)
    second = factory.prepare_sample(get_scenario("install.cold.small"), second_root)

    relative_payload = (
        Path("benchmark-package-000")
        / "skills"
        / "benchmark-package-000-primitive-0000"
        / "SKILL.md"
    )
    template_payload = factory.template_root / "small" / "package-payloads" / relative_payload
    first_payload = first_root / "packages" / relative_payload
    second_payload = second_root / "packages" / relative_payload
    assert first.cwd != second.cwd
    assert first_root / "cache" != second_root / "cache"
    assert first_root / "repositories" != second_root / "repositories"
    assert first_payload.stat().st_ino != template_payload.stat().st_ino
    assert second_payload.stat().st_ino != template_payload.stat().st_ino
    assert template_payload.stat().st_mode & stat.S_IWUSR == 0

    original = template_payload.read_bytes()
    first_payload.write_text("sample-local mutation\n", encoding="ascii")
    (first_root / "cache" / "sample-only").write_text("first\n", encoding="ascii")
    assert second_payload.read_bytes() == original
    assert template_payload.read_bytes() == original
    assert not (second_root / "cache" / "sample-only").exists()


def test_update_setup_reads_revision_a_then_timed_state_exposes_revision_b(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Update setup and measurement use sample-local immutable origin snapshots."""
    sample_root = tmp_path / "update"
    factory = FixtureFactory(tmp_path, template_root=tmp_path / "templates")
    factory.prepare_sample(get_scenario("install.cold.small"), tmp_path / "template-seed")
    template_a = factory.template_root / "small" / "repositories-a" / "benchmark-package-000.git"
    template_b = factory.template_root / "small" / "repositories-b" / "benchmark-package-000.git"
    template_a_revision = _origin_revision(template_a, dict(os.environ))
    template_b_revision = _origin_revision(template_b, dict(os.environ))
    template_before = _tree_snapshot(factory.template_root / "small")
    setup_revisions: list[str] = []

    def fake_setup(
        args: tuple[str, ...],
        *,
        cwd: Path,
        environment: object,
    ) -> None:
        del args
        current_sample_root = cwd.parents[1]
        setup_revisions.append(
            _origin_revision(
                current_sample_root / "repositories" / "benchmark-package-000.git",
                environment,
            )
        )

    monkeypatch.setattr(factory, "_run_setup", fake_setup)
    first = factory.prepare_sample(
        get_scenario("update.warm.small"),
        sample_root,
    )
    second_root = tmp_path / "update-second"
    second = factory.prepare_sample(
        get_scenario("update.warm.small"),
        second_root,
    )
    first_expected = first.expected_updates[0]
    first_timed_revision = _origin_revision(
        sample_root / "repositories" / "benchmark-package-000.git",
        dict(first.environment),
    )
    second_timed_revision = _origin_revision(
        second_root / "repositories" / "benchmark-package-000.git",
        dict(second.environment),
    )

    assert setup_revisions == [template_a_revision, template_a_revision]
    assert first_timed_revision == template_b_revision
    assert second_timed_revision == template_b_revision
    assert first_timed_revision == first_expected.resolved_commit
    assert second_timed_revision == second.expected_updates[0].resolved_commit
    assert first_expected.content_marker.startswith("revision-b-")
    assert _origin_revision(template_a, dict(os.environ)) == template_a_revision
    assert _origin_revision(template_b, dict(os.environ)) == template_b_revision
    assert _tree_snapshot(factory.template_root / "small") == template_before


def test_templates_survive_sample_mutation_and_failed_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed or mutated sample cannot contaminate a later materialization."""
    factory = FixtureFactory(tmp_path, template_root=tmp_path / "templates")
    first_root = tmp_path / "first"
    factory.prepare_sample(get_scenario("install.cold.small"), first_root)
    template = factory.template_root / "small"
    before = _tree_snapshot(template)
    payload = next((first_root / "packages").rglob("SKILL.md"))
    payload.write_text("mutated sample\n", encoding="ascii")

    def fail_setup(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise FixturePreparationError("injected setup failure")

    monkeypatch.setattr(factory, "_run_setup", fail_setup)
    failed_root = tmp_path / "failed"
    with pytest.raises(FixturePreparationError, match="injected"):
        factory.prepare_sample(get_scenario("install.warm.small"), failed_root)
    assert not failed_root.exists()
    assert _tree_snapshot(template) == before

    monkeypatch.setattr(factory, "_run_setup", lambda *args, **kwargs: None)
    recovered = factory.prepare_sample(
        get_scenario("install.warm.small"),
        failed_root,
    )
    recovered_payload = next((failed_root / "packages").rglob("SKILL.md"))
    assert recovered.cwd.is_dir()
    assert (
        recovered_payload.read_bytes()
        == next((template / "package-payloads").rglob("SKILL.md")).read_bytes()
    )


def test_failed_template_build_cleans_staging_and_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial template build leaves no reusable state and can retry cleanly."""
    factory = FixtureFactory(tmp_path, template_root=tmp_path / "templates")
    original = factory._build_dependency_templates
    calls = 0

    def fail_once(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise FixturePreparationError("injected template failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(factory, "_build_dependency_templates", fail_once)
    sample_root = tmp_path / "sample"
    with pytest.raises(FixturePreparationError, match="template failure"):
        factory.prepare_sample(get_scenario("install.cold.small"), sample_root)

    assert not sample_root.exists()
    assert not (factory.template_root / ".small.building").exists()
    assert not (factory.template_root / "small").exists()

    recovered = factory.prepare_sample(
        get_scenario("install.cold.small"),
        sample_root,
    )
    assert recovered.cwd.is_dir()
    assert (factory.template_root / "small").is_dir()


def test_factory_cleanup_removes_templates_but_not_samples(tmp_path: Path) -> None:
    """The profile owner cleans shared inputs after retaining sample state."""
    template_root = tmp_path / "templates"
    sample_root = tmp_path / "sample"
    with FixtureFactory(tmp_path, template_root=template_root) as factory:
        fixture = factory.prepare_sample(
            get_scenario("install.cold.small"),
            sample_root,
        )
        assert template_root.is_dir()
        assert fixture.cwd.is_dir()

    assert not template_root.exists()
    assert fixture.cwd.is_dir()


def test_cold_and_warm_update_caches_remain_sample_local(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cold setup cache is removed while warm setup cache remains isolated."""
    factory = FixtureFactory(tmp_path, template_root=tmp_path / "templates")

    def fake_setup(
        args: tuple[str, ...],
        *,
        cwd: Path,
        environment: object,
    ) -> None:
        del args, environment
        sample_root = cwd.parents[1]
        (sample_root / "cache" / "primed").write_text("warm\n", encoding="ascii")

    monkeypatch.setattr(factory, "_run_setup", fake_setup)
    cold_root = tmp_path / "cold"
    warm_root = tmp_path / "warm"
    factory.prepare_sample(get_scenario("update.cold.small"), cold_root)
    factory.prepare_sample(get_scenario("update.warm.small"), warm_root)

    assert list((cold_root / "cache").iterdir()) == []
    assert (warm_root / "cache" / "primed").read_text(encoding="ascii") == "warm\n"
    assert not (cold_root / "cache" / "primed").exists()


def test_warm_install_primes_cache_but_restores_pristine_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Warm install keeps reusable cache but removes setup-generated project state."""
    factory = FixtureFactory(tmp_path, template_root=tmp_path / "templates")
    sample_root = tmp_path / "warm-install"

    def fake_setup(
        args: tuple[str, ...],
        *,
        cwd: Path,
        environment: object,
    ) -> None:
        del args, environment
        (sample_root / "cache" / "primed").write_text("cache\n", encoding="ascii")
        (cwd / "apm.lock.yaml").write_text("generated\n", encoding="ascii")
        (cwd / "apm_modules" / "generated").mkdir(parents=True)
        (cwd / "setup-only").write_text("generated\n", encoding="ascii")

    monkeypatch.setattr(factory, "_run_setup", fake_setup)
    fixture = factory.prepare_sample(
        get_scenario("install.warm.small"),
        sample_root,
    )

    assert (sample_root / "cache" / "primed").read_text(encoding="ascii") == "cache\n"
    assert (fixture.cwd / "apm.yml").is_file()
    assert not (fixture.cwd / "apm.lock.yaml").exists()
    assert not (fixture.cwd / "apm_modules").exists()
    assert not (fixture.cwd / "setup-only").exists()


def test_generated_dependency_fixture_preserves_manifest_urls_and_revisions(
    tmp_path: Path,
) -> None:
    """Templating retains the exact consumer manifest and Git content contract."""
    factory = FixtureFactory(tmp_path, template_root=tmp_path / "templates")
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first = factory.prepare_sample(get_scenario("install.cold.small"), first_root)
    second = factory.prepare_sample(get_scenario("install.cold.small"), second_root)
    first_manifest = load_yaml(first.cwd / "apm.yml")
    second_manifest = load_yaml(second.cwd / "apm.yml")
    assert first_manifest == second_manifest
    dependencies = first_manifest["dependencies"]["apm"]
    assert len(dependencies) == 1
    dependency = dependencies[0]
    parsed = urlparse(dependency["git"])
    assert parsed.scheme == "https"
    assert parsed.hostname == "github.com"
    assert parsed.path == "/apm-benchmark-fixtures/benchmark-package-000"
    assert dependency["ref"] == "main"
    assert dependency["alias"] == "benchmark-package-000"

    first_origin = first_root / "repositories" / "benchmark-package-000.git"
    second_origin = second_root / "repositories" / "benchmark-package-000.git"
    first_revision = _origin_revision(first_origin, dict(first.environment))
    second_revision = _origin_revision(second_origin, dict(second.environment))
    assert first_revision == second_revision
    relative_content = "skills/benchmark-package-000-primitive-0000/SKILL.md"
    git_content = subprocess.run(
        ("git", "--git-dir", str(first_origin), "show", f"{first_revision}:{relative_content}"),
        env=dict(first.environment),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout
    package_content = (
        first_root / "packages" / "benchmark-package-000" / relative_content
    ).read_text(encoding="ascii")
    assert git_content == package_content


def test_medium_dependency_templates_preserve_whole_revision_trees(
    tmp_path: Path,
) -> None:
    """Every package and primitive matches revision A and only one file changes in B."""
    factory = FixtureFactory(tmp_path, template_root=tmp_path / "templates")
    sample_root = tmp_path / "medium"
    factory.prepare_sample(get_scenario("install.cold.medium"), sample_root)

    for package_index in range(4):
        package_name = f"benchmark-package-{package_index:03d}"
        package_root = sample_root / "packages" / package_name
        revision_a_origin = sample_root / "repositories" / f"{package_name}.git"
        revision_b_origin = (
            factory.template_root / "medium" / "repositories-b" / f"{package_name}.git"
        )
        revision_a = _origin_revision(revision_a_origin, dict(os.environ))
        revision_b = _origin_revision(revision_b_origin, dict(os.environ))
        package_paths = tuple(
            path.relative_to(package_root).as_posix()
            for path in sorted(package_root.rglob("*"))
            if path.is_file()
        )
        revision_a_paths = _git_tree_paths(revision_a_origin, revision_a)
        revision_b_paths = _git_tree_paths(revision_b_origin, revision_b)
        assert revision_a_paths == package_paths
        assert revision_b_paths == package_paths

        changed_paths: list[str] = []
        for relative_path in package_paths:
            package_bytes = (package_root / relative_path).read_bytes()
            revision_a_bytes = _git_file_bytes(
                revision_a_origin,
                revision_a,
                relative_path,
            )
            revision_b_bytes = _git_file_bytes(
                revision_b_origin,
                revision_b,
                relative_path,
            )
            assert revision_a_bytes == package_bytes
            if revision_b_bytes != revision_a_bytes:
                changed_paths.append(relative_path)
        assert changed_paths == [f"skills/{package_name}-primitive-0000/SKILL.md"]
        changed_content = _git_file_bytes(
            revision_b_origin,
            revision_b,
            changed_paths[0],
        ).decode("ascii")
        assert f"revision-b-{package_name}" in changed_content


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
