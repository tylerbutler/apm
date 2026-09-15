"""Deterministic fixture and pre-timing state preparation."""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from apm_cli.deps.lockfile import LockFile
from apm_cli.models.dependency import DependencyReference
from apm_cli.utils.file_ops import robust_copytree, robust_rmtree
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
class _TemplateRepository:
    """Immutable revision metadata for one templated dependency repository."""

    name: str
    remote_url: str
    revision_a: str
    revision_b: str
    relative_content_path: Path
    content_marker: str


@dataclass(frozen=True)
class _FixtureTemplate:
    """Read-only package, compile, and Git inputs shared by one fixture size."""

    size: FixtureSize
    root: Path
    package_root: Path
    compile_project_root: Path
    revision_a_root: Path
    revision_b_root: Path
    repositories: tuple[_TemplateRepository, ...]


class FixtureFactory:
    """Build immutable size templates and materialize isolated samples."""

    def __init__(
        self,
        repository_root: Path,
        *,
        base_environment: Mapping[str, str] | None = None,
        template_root: Path | None = None,
    ) -> None:
        """Create a fixture factory for one source checkout."""
        self._repository_root = repository_root.resolve()
        self._base_environment = dict(base_environment or os.environ)
        self._temporary_templates: tempfile.TemporaryDirectory[str] | None = None
        if template_root is None:
            self._temporary_templates = tempfile.TemporaryDirectory(
                prefix="apm-benchmark-templates-"
            )
            self._template_root = Path(self._temporary_templates.name)
        else:
            self._template_root = template_root.resolve()
            self._template_root.mkdir(parents=True, exist_ok=False)
        self._templates: dict[str, _FixtureTemplate] = {}
        self._closed = False

    @property
    def template_root(self) -> Path:
        """Return the private template root for lifecycle verification."""
        return self._template_root

    def __enter__(self) -> FixtureFactory:
        """Return this factory as a managed template owner."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Remove all shared templates after the profile run."""
        del exc_info
        self.close()

    def close(self) -> None:
        """Remove shared templates without touching materialized samples."""
        if self._closed:
            return
        self._closed = True
        if self._temporary_templates is not None:
            _make_tree_writable(self._template_root)
            self._temporary_templates.cleanup()
            return
        _remove_tree_writable(self._template_root)

    def prepare_sample(
        self,
        scenario: BenchmarkScenario,
        sample_root: Path,
    ) -> PreparedFixture:
        """Construct and reset one independent scenario sample."""
        if self._closed:
            raise FixturePreparationError("Fixture factory is closed")
        try:
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
            template = self._template_for(size)
            if scenario.operation == "compile":
                return self._prepare_compile(scenario, template, isolated)
            if scenario.operation in {"install", "update"}:
                return self._prepare_dependency_operation(scenario, template, isolated)
            raise FixturePreparationError(f"Unsupported benchmark operation: {scenario.operation}")
        except Exception:
            _remove_tree_writable(sample_root)
            raise

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
        template: _FixtureTemplate,
        isolated: IsolatedApmEnvironment,
    ) -> PreparedFixture:
        size = template.size
        project_root = isolated.work_root / f"benchmark-compile-{size.id}"
        _copy_tree_mutable(
            template.compile_project_root,
            project_root,
        )
        environment = sanitize_environment(isolated.subprocess_env())
        expected_path = project_root / "AGENTS.md"
        marker = f"benchmark-compile-{size.id}-cold-marker"
        source_path = (
            project_root / ".apm" / "instructions" / "benchmark-instruction-0000.instructions.md"
        )
        source_path.write_text(
            _instruction_content(
                "benchmark-instruction-0000",
                size.payload_bytes,
                marker=marker,
            ),
            encoding="ascii",
            newline="",
        )
        if scenario.temperature == "warm":
            self._run_setup(scenario.command, cwd=project_root, environment=environment)
            marker = f"benchmark-compile-{size.id}-warm-revision-b"
            source_path.write_text(
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
            cwd=project_root,
            environment=environment,
            required_paths=(expected_path,),
            expected_file_markers=((expected_path, marker),),
        )

    def _prepare_dependency_operation(
        self,
        scenario: BenchmarkScenario,
        template: _FixtureTemplate,
        isolated: IsolatedApmEnvironment,
    ) -> PreparedFixture:
        size = template.size
        _copy_tree_mutable(
            template.package_root,
            isolated.package_root,
            dirs_exist_ok=True,
        )
        _copy_tree_mutable(
            template.revision_a_root,
            isolated.repository_root,
            dirs_exist_ok=True,
        )
        dependencies = tuple(
            {
                "git": item.remote_url,
                "ref": "main",
                "alias": item.name,
            }
            for item in template.repositories
        )
        environment = _dependency_environment(isolated, template.repositories)
        consumer_factory = LocalPackageFactory(isolated.work_root)
        project = consumer_factory.create(
            f"benchmark-{scenario.operation}-{size.id}",
            dependencies=dependencies,
            targets=("copilot",),
        )
        expected_install_paths = tuple(
            project.root / "apm_modules" / item.name for item in template.repositories
        )
        required_paths = (project.root / "apm.lock.yaml",)

        if scenario.operation == "install":
            if scenario.temperature == "warm":
                seed_root = isolated.root / "project-seed"
                _copy_tree_mutable(project.root, seed_root)
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
        _replace_tree(
            isolated.repository_root,
            template.revision_b_root,
        )
        expected_updates = tuple(
            ExpectedUpdate(
                repo_url=DependencyReference.parse(item.remote_url).canonical_repo_url,
                resolved_commit=item.revision_b,
                install_path=project.root / "apm_modules" / item.name,
                relative_content_path=item.relative_content_path,
                content_marker=item.content_marker,
            )
            for item in template.repositories
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

    def _template_for(self, size: FixtureSize) -> _FixtureTemplate:
        template = self._templates.get(size.id)
        if template is not None:
            return template
        template = self._build_template(size)
        self._templates[size.id] = template
        return template

    def _build_template(self, size: FixtureSize) -> _FixtureTemplate:
        final_root = self._template_root / size.id
        staging_root = self._template_root / f".{size.id}.building"
        _remove_tree_writable(staging_root)
        _remove_tree_writable(final_root)
        try:
            staging_root.mkdir(parents=True)
            build_environment = IsolatedApmEnvironment.create(
                staging_root / "build",
                base_env=self._base_environment,
            )
            repositories = self._build_dependency_templates(
                size,
                build_environment,
                staging_root,
            )
            compile_project = self._build_compile_template(size, build_environment)
            compile_template_root = staging_root / "compile-project"
            _copy_tree_mutable(compile_project.root, compile_template_root)
            _remove_tree_writable(build_environment.root)
            staging_root.rename(final_root)
            template = _FixtureTemplate(
                size=size,
                root=final_root,
                package_root=final_root / "package-payloads",
                compile_project_root=final_root / "compile-project",
                revision_a_root=final_root / "repositories-a",
                revision_b_root=final_root / "repositories-b",
                repositories=repositories,
            )
            _make_tree_readonly(final_root)
            return template
        except Exception:
            _remove_tree_writable(staging_root)
            _remove_tree_writable(final_root)
            raise

    def _build_dependency_templates(
        self,
        size: FixtureSize,
        isolated: IsolatedApmEnvironment,
        staging_root: Path,
    ) -> tuple[_TemplateRepository, ...]:
        package_factory = LocalPackageFactory(isolated.package_root)
        repository_factory = LocalGitRepositoryFactory(
            isolated.repository_root,
            env=isolated.subprocess_env(),
        )
        published: list[tuple[LocalPackage, LocalGitRepository, str, str]] = []
        for package_index in range(size.package_count):
            name = f"benchmark-package-{package_index:03d}"
            package = package_factory.create(name, targets=("copilot",))
            self._add_package_primitives(package_factory, package, size)
            repository = repository_factory.create(name, source_tree=package.root)
            revision_a = repository_factory.commit(
                repository,
                message=f"seed {name}",
            ).sha
            remote_url = f"https://github.com/apm-benchmark-fixtures/{name}"
            published.append((package, repository, remote_url, revision_a))

        package_template_root = staging_root / "package-payloads"
        revision_a_root = staging_root / "repositories-a"
        _copy_tree_mutable(isolated.package_root, package_template_root)
        self._copy_origins(published, revision_a_root)

        repositories: list[_TemplateRepository] = []
        for package, repository, remote_url, revision_a in published:
            primitive_name = f"{package.name}-primitive-0000"
            relative_path = Path("skills") / primitive_name / "SKILL.md"
            marker = f"revision-b-{package.name}"
            (repository.worktree / relative_path).write_text(
                _skill_content(
                    primitive_name,
                    size.payload_bytes,
                    marker=marker,
                ),
                encoding="ascii",
                newline="",
            )
            revision_b = repository_factory.commit(
                repository,
                message=f"update {package.name}",
            ).sha
            repositories.append(
                _TemplateRepository(
                    name=package.name,
                    remote_url=remote_url,
                    revision_a=revision_a,
                    revision_b=revision_b,
                    relative_content_path=relative_path,
                    content_marker=marker,
                )
            )
        self._copy_origins(published, staging_root / "repositories-b")
        return tuple(repositories)

    @staticmethod
    def _copy_origins(
        published: list[tuple[LocalPackage, LocalGitRepository, str, str]],
        destination: Path,
    ) -> None:
        destination.mkdir(parents=True)
        for package, repository, _remote_url, _revision_a in published:
            _copy_tree_mutable(
                repository.origin,
                destination / f"{package.name}.git",
            )

    @staticmethod
    def _build_compile_template(
        size: FixtureSize,
        isolated: IsolatedApmEnvironment,
    ) -> LocalPackage:
        package_factory = LocalPackageFactory(isolated.work_root)
        project = package_factory.create(
            f"benchmark-compile-{size.id}",
            targets=("copilot",),
        )
        primitive_count = size.package_count * size.primitives_per_package
        for index in range(primitive_count):
            name = f"benchmark-instruction-{index:04d}"
            package_factory.add_instruction(
                project,
                name,
                _instruction_content(name, size.payload_bytes),
            )
        return project

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
    _copy_tree_mutable(source, destination)


def _copy_tree_mutable(
    source: Path,
    destination: Path,
    *,
    dirs_exist_ok: bool = False,
) -> None:
    """Reflink-copy one template tree and restore sample write permissions."""
    robust_copytree(
        source,
        destination,
        dirs_exist_ok=dirs_exist_ok,
    )
    _make_tree_writable(destination)


def _dependency_environment(
    isolated: IsolatedApmEnvironment,
    repositories: tuple[_TemplateRepository, ...],
) -> dict[str, str]:
    """Route production dependency URLs to sample-local immutable revisions."""
    environment = isolated.subprocess_env()
    slots: list[tuple[str, str]] = []
    for item in repositories:
        origin = isolated.repository_root / f"{item.name}.git"
        rewrite_base = f"{origin.resolve().as_uri()}/"
        key = f"url.{rewrite_base}.insteadOf"
        bare = item.remote_url.removesuffix(".git")
        slots.extend(((key, bare), (key, f"{bare}.git")))
    environment["GIT_CONFIG_COUNT"] = str(len(slots))
    for index, (key, value) in enumerate(slots):
        environment[f"GIT_CONFIG_KEY_{index}"] = key
        environment[f"GIT_CONFIG_VALUE_{index}"] = value
    return sanitize_environment(environment)


def _make_tree_readonly(path: Path) -> None:
    """Protect shared templates from accidental sample mutation."""
    if not path.exists():
        return
    for candidate in sorted(path.rglob("*"), reverse=True):
        mode = candidate.stat().st_mode
        candidate.chmod(mode & ~0o222)
    path.chmod(path.stat().st_mode & ~0o222)


def _make_tree_writable(path: Path) -> None:
    """Restore owner write permission throughout a copied or cleanup tree."""
    if not path.exists():
        return
    path.chmod(path.stat().st_mode | 0o700)
    for candidate in path.rglob("*"):
        try:
            candidate.chmod(candidate.stat().st_mode | (0o700 if candidate.is_dir() else 0o600))
        except OSError:
            continue


def _remove_tree_writable(path: Path) -> None:
    """Remove a generated tree after restoring owner write permissions."""
    if not path.exists():
        return
    with contextlib.suppress(OSError):
        _make_tree_writable(path)
    robust_rmtree(path)
