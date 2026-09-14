"""Deterministic fixture and pre-timing state preparation."""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from apm_cli.deps.lockfile import LockFile
from apm_cli.models.dependency import DependencyReference
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepository, LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackage, LocalPackageFactory

from .catalog import (
    CONTROL_SCENARIO_ID,
    INSTALL_COMMAND,
    LIVE_DEPENDENCY,
    BenchmarkScenario,
    FixtureSize,
    get_fixture_size,
)
from .runner import sanitize_environment, source_cli_command


class FixturePreparationError(RuntimeError):
    """Raised when untimed fixture construction cannot reach the required state."""


@dataclass(frozen=True)
class ExpectedUpdate:
    """Revision and deployed-content evidence expected after one update."""

    repo_url: str
    resolved_commit: str
    install_path: Path
    relative_content_path: Path
    content_marker: str


@dataclass(frozen=True)
class PreparedFixture:
    """One isolated, fully prepared benchmark sample."""

    scenario_id: str
    cwd: Path
    environment: Mapping[str, str]
    expected_install_paths: tuple[Path, ...] = ()
    expected_updates: tuple[ExpectedUpdate, ...] = ()
    required_paths: tuple[Path, ...] = ()
    expected_file_markers: tuple[tuple[Path, str], ...] = ()

    def validate(self, *, stdout: str, stderr: str) -> None:
        """Validate command output and durable postconditions."""
        combined = f"{stdout}\n{stderr}".strip()
        if self.scenario_id == CONTROL_SCENARIO_ID:
            if not combined or "apm" not in combined.lower():
                raise AssertionError("startup command did not print an APM version")
            return

        for required_path in self.required_paths:
            if not required_path.is_file():
                raise AssertionError(f"required benchmark output is missing: {required_path}")

        if self.expected_install_paths:
            missing = [path for path in self.expected_install_paths if not path.is_dir()]
            if missing:
                raise AssertionError(
                    "installed benchmark packages are missing: "
                    + ", ".join(str(path) for path in missing)
                )

        for path, marker in self.expected_file_markers:
            if not path.is_file():
                raise AssertionError(f"expected benchmark file is missing: {path}")
            content = path.read_text(encoding="utf-8")
            if marker not in content:
                raise AssertionError(f"expected benchmark marker {marker!r} is missing from {path}")

        if self.expected_updates:
            self._validate_updates()

    def _validate_updates(self) -> None:
        """Bind every expected revision to its lock entry and deployed content."""
        lock_path = self.cwd / "apm.lock.yaml"
        lockfile = LockFile.read(lock_path)
        if lockfile is None:
            raise AssertionError(f"updated lockfile is missing: {lock_path}")
        dependencies = {
            dependency.repo_url: dependency for dependency in lockfile.get_package_dependencies()
        }
        if len(dependencies) != len(lockfile.get_package_dependencies()):
            raise AssertionError("updated lockfile contains duplicate dependency identities")
        for expected in self.expected_updates:
            dependency = dependencies.get(expected.repo_url)
            if dependency is None:
                raise AssertionError(
                    f"updated dependency is missing from lockfile: {expected.repo_url}"
                )
            if dependency.resolved_commit != expected.resolved_commit:
                raise AssertionError(
                    f"updated dependency revision mismatch for {expected.repo_url}: "
                    f"{dependency.resolved_commit!r}"
                )
            deployed_content = expected.install_path / expected.relative_content_path
            if not deployed_content.is_file():
                raise AssertionError(f"updated dependency content is missing: {deployed_content}")
            if expected.content_marker not in deployed_content.read_text(encoding="utf-8"):
                raise AssertionError(f"updated dependency content is stale: {expected.repo_url}")


@dataclass(frozen=True)
class _PublishedFixture:
    """Local package repository used by an install or update scenario."""

    package: LocalPackage
    repository: LocalGitRepository
    remote_url: str


class FixtureFactory:
    """Build a fresh sample state entirely outside the timed region."""

    def __init__(
        self,
        repository_root: Path,
        *,
        base_environment: Mapping[str, str] | None = None,
    ) -> None:
        """Create a fixture factory for one source checkout."""
        self._repository_root = repository_root.resolve()
        self._base_environment = dict(base_environment or os.environ)

    def prepare_sample(
        self,
        scenario: BenchmarkScenario,
        sample_root: Path,
    ) -> PreparedFixture:
        """Construct and reset one independent scenario sample."""
        isolated = IsolatedApmEnvironment.create(
            sample_root,
            base_env=self._base_environment,
        )
        if scenario.operation == "startup":
            return PreparedFixture(
                scenario_id=scenario.id,
                cwd=isolated.work_root,
                environment=sanitize_environment(isolated.subprocess_env()),
            )
        if scenario.live:
            return self._prepare_live(scenario, isolated)
        size = get_fixture_size(_required_size_id(scenario))
        if scenario.operation == "compile":
            return self._prepare_compile(scenario, size, isolated)
        if scenario.operation in {"install", "update"}:
            return self._prepare_dependency_operation(scenario, size, isolated)
        raise FixturePreparationError(f"Unsupported benchmark operation: {scenario.operation}")

    def _prepare_live(
        self,
        scenario: BenchmarkScenario,
        isolated: IsolatedApmEnvironment,
    ) -> PreparedFixture:
        package_factory = LocalPackageFactory(isolated.work_root)
        project = package_factory.create(
            "benchmark-live-consumer",
            dependencies=(LIVE_DEPENDENCY,),
            targets=("copilot",),
        )
        environment = sanitize_environment(isolated.subprocess_env())
        # The isolated test utility blocks IP networking through sitecustomize.
        # Live rows deliberately allow public anonymous GitHub access while
        # retaining the utility's credential and filesystem isolation.
        environment.pop("PYTHONPATH", None)
        environment["GIT_ALLOW_PROTOCOL"] = "https:file"
        install_path = DependencyReference.parse(LIVE_DEPENDENCY).get_install_path(
            project.root / "apm_modules"
        )
        return PreparedFixture(
            scenario_id=scenario.id,
            cwd=project.root,
            environment=environment,
            expected_install_paths=(install_path,),
            required_paths=(project.root / "apm.lock.yaml",),
        )

    def _prepare_compile(
        self,
        scenario: BenchmarkScenario,
        size: FixtureSize,
        isolated: IsolatedApmEnvironment,
    ) -> PreparedFixture:
        package_factory = LocalPackageFactory(isolated.work_root)
        project = package_factory.create(
            f"benchmark-compile-{size.id}",
            targets=("copilot",),
        )
        primitive_count = size.package_count * size.primitives_per_package
        source_paths: list[Path] = []
        for index in range(primitive_count):
            name = f"benchmark-instruction-{index:04d}"
            source_paths.append(
                package_factory.add_instruction(
                    project,
                    name,
                    _instruction_content(name, size.payload_bytes),
                )
            )
        environment = sanitize_environment(isolated.subprocess_env())
        expected_path = project.root / "AGENTS.md"
        marker = f"benchmark-compile-{size.id}-cold-marker"
        source_paths[0].write_text(
            _instruction_content(
                "benchmark-instruction-0000",
                size.payload_bytes,
                marker=marker,
            ),
            encoding="ascii",
            newline="",
        )
        if scenario.temperature == "warm":
            self._run_setup(scenario.command, cwd=project.root, environment=environment)
            marker = f"benchmark-compile-{size.id}-warm-revision-b"
            source_paths[0].write_text(
                _instruction_content(
                    "benchmark-instruction-0000",
                    size.payload_bytes,
                    marker=marker,
                ),
                encoding="ascii",
                newline="",
            )
        return PreparedFixture(
            scenario_id=scenario.id,
            cwd=project.root,
            environment=environment,
            required_paths=(expected_path,),
            expected_file_markers=((expected_path, marker),),
        )

    def _prepare_dependency_operation(
        self,
        scenario: BenchmarkScenario,
        size: FixtureSize,
        isolated: IsolatedApmEnvironment,
    ) -> PreparedFixture:
        package_factory = LocalPackageFactory(isolated.package_root)
        repository_factory = LocalGitRepositoryFactory(
            isolated.repository_root,
            env=isolated.subprocess_env(),
        )
        published: list[_PublishedFixture] = []
        dependencies: list[dict[str, object]] = []
        rewrites: list[tuple[LocalGitRepository, str]] = []
        for package_index in range(size.package_count):
            name = f"benchmark-package-{package_index:03d}"
            package = package_factory.create(name, targets=("copilot",))
            self._add_package_primitives(package_factory, package, size)
            repository = repository_factory.create(name, source_tree=package.root)
            repository_factory.commit(repository, message=f"seed {name}")
            remote_url = f"https://github.com/apm-benchmark-fixtures/{name}"
            published.append(
                _PublishedFixture(
                    package=package,
                    repository=repository,
                    remote_url=remote_url,
                )
            )
            dependencies.append(
                {
                    "git": remote_url,
                    "ref": "main",
                    "alias": name,
                }
            )
            rewrites.append((repository, remote_url))

        environment = sanitize_environment(
            repository_factory.url_rewrite_subprocess_env_many(tuple(rewrites))
        )
        consumer_factory = LocalPackageFactory(isolated.work_root)
        project = consumer_factory.create(
            f"benchmark-{scenario.operation}-{size.id}",
            dependencies=tuple(dependencies),
            targets=("copilot",),
        )
        expected_install_paths = tuple(
            project.root / "apm_modules" / item.package.name for item in published
        )
        required_paths = (project.root / "apm.lock.yaml",)

        if scenario.operation == "install":
            if scenario.temperature == "warm":
                seed_root = isolated.root / "project-seed"
                shutil.copytree(project.root, seed_root)
                self._run_setup(scenario.command, cwd=project.root, environment=environment)
                _replace_tree(project.root, seed_root)
            return PreparedFixture(
                scenario_id=scenario.id,
                cwd=project.root,
                environment=environment,
                expected_install_paths=expected_install_paths,
                required_paths=required_paths,
            )

        self._run_setup(
            INSTALL_COMMAND,
            cwd=project.root,
            environment=environment,
        )
        expected_updates = self._advance_repositories(
            repository_factory,
            published,
            size,
            project.root / "apm_modules",
        )
        if scenario.temperature == "cold":
            _remove_tree_writable(isolated.cache_root)
            isolated.cache_root.mkdir(parents=True)
        return PreparedFixture(
            scenario_id=scenario.id,
            cwd=project.root,
            environment=environment,
            expected_install_paths=expected_install_paths,
            expected_updates=expected_updates,
            required_paths=required_paths,
        )

    def _add_package_primitives(
        self,
        package_factory: LocalPackageFactory,
        package: LocalPackage,
        size: FixtureSize,
    ) -> None:
        for primitive_index in range(size.primitives_per_package):
            name = f"{package.name}-primitive-{primitive_index:04d}"
            if primitive_index % 2 == 0:
                package_factory.add_skill(
                    package,
                    name,
                    _skill_content(name, size.payload_bytes),
                )
            else:
                package_factory.add_instruction(
                    package,
                    name,
                    _instruction_content(name, size.payload_bytes),
                )

    def _advance_repositories(
        self,
        repository_factory: LocalGitRepositoryFactory,
        published: list[_PublishedFixture],
        size: FixtureSize,
        module_root: Path,
    ) -> tuple[ExpectedUpdate, ...]:
        updates: list[ExpectedUpdate] = []
        for item in published:
            primitive_name = f"{item.package.name}-primitive-0000"
            relative_path = Path("skills") / primitive_name / "SKILL.md"
            update_path = item.repository.worktree / relative_path
            marker = f"revision-b-{item.package.name}"
            update_path.write_text(
                _skill_content(
                    primitive_name,
                    size.payload_bytes,
                    marker=marker,
                ),
                encoding="ascii",
                newline="",
            )
            commit = repository_factory.commit(
                item.repository,
                message=f"update {item.package.name}",
            )
            updates.append(
                ExpectedUpdate(
                    repo_url=DependencyReference.parse(item.remote_url).canonical_repo_url,
                    resolved_commit=commit.sha,
                    install_path=module_root / item.package.name,
                    relative_content_path=relative_path,
                    content_marker=marker,
                )
            )
        return tuple(updates)

    def _run_setup(
        self,
        args: tuple[str, ...],
        *,
        cwd: Path,
        environment: Mapping[str, str],
    ) -> None:
        command = (*source_cli_command(self._repository_root), *args)
        try:
            result = subprocess.run(  # noqa: S603
                command,
                cwd=cwd,
                env=dict(environment),
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise FixturePreparationError(f"Untimed fixture setup timed out: {command!r}") from exc
        if result.returncode != 0:
            raise FixturePreparationError(
                "Untimed fixture setup failed\n"
                f"command={command!r}\n"
                f"cwd={cwd}\n"
                f"returncode={result.returncode}\n"
                f"stdout={result.stdout!r}\n"
                f"stderr={result.stderr!r}"
            )


def _required_size_id(scenario: BenchmarkScenario) -> str:
    if scenario.fixture_size_id is None:
        raise FixturePreparationError(f"Scenario requires a fixture size: {scenario.id}")
    return scenario.fixture_size_id


def _payload(label: str, payload_bytes: int) -> str:
    """Return deterministic printable ASCII text at least payload_bytes long."""
    line = f"{label}: benchmark payload\n"
    repetitions = max(1, (payload_bytes + len(line) - 1) // len(line))
    return (line * repetitions)[:payload_bytes] + "\n"


def _skill_content(
    name: str,
    payload_bytes: int,
    *,
    marker: str | None = None,
) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: Hermetic benchmark skill {name}\n"
        "---\n"
        f"# {name}\n\n"
        f"{marker + chr(10) if marker is not None else ''}"
        f"{_payload(name, payload_bytes)}"
    )


def _instruction_content(
    name: str,
    payload_bytes: int,
    *,
    marker: str | None = None,
) -> str:
    return (
        "---\n"
        "applyTo: '**'\n"
        f"description: Hermetic benchmark instruction {name}\n"
        "---\n"
        f"# {name}\n\n"
        f"{marker + chr(10) if marker is not None else ''}"
        f"{_payload(name, payload_bytes)}"
    )


def _replace_tree(destination: Path, source: Path) -> None:
    """Replace a project tree with a pristine untimed seed copy."""
    _remove_tree_writable(destination)
    shutil.copytree(source, destination)


def _remove_tree_writable(path: Path) -> None:
    """Remove a generated tree after restoring owner write permissions."""
    if not path.exists():
        return
    for candidate in path.rglob("*"):
        try:
            candidate.chmod(0o700 if candidate.is_dir() else 0o600)
        except OSError:
            continue
    with contextlib.suppress(OSError):
        path.chmod(0o700)
    shutil.rmtree(path)
