"""Tests for bounded current-ref annotation before update-plan construction."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from apm_cli.deps.tiered_ref_resolver import (
    L0PerRunCache,
    L3LegacyClone,
    PerRunRefCache,
    RefResolution,
    TieredRefResolver,
)
from apm_cli.install.helpers.ref_reuse import annotate_update_plan_refs
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.models.dependency.types import GitReferenceType, ResolvedReference


def _dep(index: int, *, repo: str | None = None) -> DependencyReference:
    """Build one eligible update-plan dependency."""
    return DependencyReference(
        repo_url=repo or f"org/repo-{index}",
        reference="main",
    )


def _resolved(dep: DependencyReference, index: int = 0) -> ResolvedReference:
    """Build one resolved branch result for *dep*."""
    return ResolvedReference(
        original_ref=str(dep),
        ref_type=GitReferenceType.BRANCH,
        resolved_commit=f"{index:040x}",
        ref_name=dep.reference or "main",
    )


def test_annotation_bounds_concurrent_resolves_and_preserves_order() -> None:
    """The worker bound is honored while results stay attached in input order."""
    worker_limit = 3
    state_lock = threading.Lock()
    release = threading.Event()
    active = 0
    maximum_active = 0

    def resolve(dep: DependencyReference) -> ResolvedReference:
        nonlocal active, maximum_active
        index = int(dep.repo_url.rsplit("-", 1)[-1])
        with state_lock:
            active += 1
            maximum_active = max(maximum_active, active)
            if maximum_active == worker_limit:
                release.set()
        assert release.wait(timeout=5)
        time.sleep(0.005)
        with state_lock:
            active -= 1
        return _resolved(dep, index)

    deps = [_dep(index) for index in range(7)]
    downloader = SimpleNamespace(resolve_git_reference=resolve)

    result = annotate_update_plan_refs(
        deps,
        downloader,
        update_refs=True,
        max_workers=worker_limit,
    )

    assert result is deps
    assert maximum_active == worker_limit
    assert [dep.resolved_reference.resolved_commit for dep in deps] == [
        f"{index:040x}" for index in range(7)
    ]


def test_annotation_uses_tiered_single_flight_for_duplicate_refs() -> None:
    """Parallel duplicate refs perform one underlying resolve per normalized key."""
    calls: list[str] = []
    calls_lock = threading.Lock()

    class _CountingTier:
        name = "remote_ref"

        def try_resolve(
            self,
            dep_ref: DependencyReference,
            ref: str,
        ) -> RefResolution:
            with calls_lock:
                calls.append(dep_ref.repo_url)
            time.sleep(0.01)
            return RefResolution(
                ref_type=GitReferenceType.BRANCH,
                resolved_commit="a" * 40,
                ref_name=ref,
            )

    legacy_inner = SimpleNamespace(
        resolve=lambda _dep: (_ for _ in ()).throw(
            AssertionError("legacy resolution should not run")
        )
    )
    cache = PerRunRefCache()
    legacy = L3LegacyClone(legacy_inner)
    resolver = TieredRefResolver(
        tiers=[L0PerRunCache(cache), _CountingTier(), legacy],
        cache=cache,
        legacy=legacy,
    )
    downloader = SimpleNamespace(resolve_git_reference=resolver.resolve)
    deps = [
        _dep(0, repo="org/shared"),
        _dep(1, repo="org/shared"),
        _dep(2, repo="org/other"),
        _dep(3, repo="org/shared"),
        _dep(4, repo="org/other"),
    ]

    annotate_update_plan_refs(
        deps,
        downloader,
        update_refs=True,
        max_workers=4,
    )

    assert sorted(calls) == ["org/other", "org/shared"]
    assert resolver.stats["remote_ref"] == 2
    assert resolver.stats["coalesced"] + resolver.stats["per_run_cache"] == 3
    assert all(dep.resolved_reference is not None for dep in deps)


def test_annotation_preserves_first_input_order_error() -> None:
    """Concurrent completion order does not change the surfaced exception."""

    def resolve(dep: DependencyReference) -> ResolvedReference:
        if dep.repo_url == "org/first":
            time.sleep(0.02)
            raise RuntimeError("first resolution failed")
        raise RuntimeError("second resolution failed")

    deps = [_dep(0, repo="org/first"), _dep(1, repo="org/second")]
    downloader = SimpleNamespace(resolve_git_reference=resolve)

    with pytest.raises(RuntimeError, match="first resolution failed"):
        annotate_update_plan_refs(
            deps,
            downloader,
            update_refs=True,
            max_workers=2,
        )

    assert all(getattr(dep, "resolved_reference", None) is None for dep in deps)


def test_annotation_skips_ineligible_dependencies_without_workers() -> None:
    """Existing, local, registry, and proxy resolutions remain untouched."""
    existing = _dep(0)
    existing.resolved_reference = _resolved(existing)
    local = DependencyReference(repo_url="local", local_path="./local", is_local=True)
    registry = DependencyReference(repo_url="org/registry", source="registry")
    proxy = DependencyReference(
        repo_url="org/proxy",
        reference="main",
        artifactory_prefix="proxy",
    )
    downloader = SimpleNamespace(
        resolve_git_reference=lambda _dep: (_ for _ in ()).throw(
            AssertionError("ineligible dependency was resolved")
        )
    )

    deps = [existing, local, registry, proxy]
    result = annotate_update_plan_refs(
        deps,
        downloader,
        update_refs=True,
        max_workers=4,
    )

    assert result is deps
    assert existing.resolved_reference is not None
    assert getattr(local, "resolved_reference", None) is None
    assert getattr(registry, "resolved_reference", None) is None
    assert getattr(proxy, "resolved_reference", None) is None
