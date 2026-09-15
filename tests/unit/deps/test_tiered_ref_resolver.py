"""Unit tests for ``apm_cli.deps.tiered_ref_resolver`` (#1369).

Each tier is tested in isolation by mocking the dependency it reaches
into. The orchestrator is tested for cache hit, coalesce-lock, fall-
through, and feature-flag behaviour.
"""

from __future__ import annotations

import os
import sys
import threading
import types
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from apm_cli.cache.url_normalize import cache_shard_key
from apm_cli.deps.tiered_ref_resolver import (
    L0PerRunCache,
    L1CommitsAPI,
    L2BareRevParse,
    L2RemoteRef,
    L3LegacyClone,
    PerRunRefCache,
    RefFreshnessPolicy,
    RefResolution,
    TieredRefResolver,
    _repository_cache_identity,
    build_tiered_ref_resolver,
    is_tiered_resolver_enabled,
)
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.models.dependency.types import GitReferenceType, ResolvedReference

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


def _dep(repo: str = "owner/repo", ref: str = "main") -> DependencyReference:
    return DependencyReference(repo_url=repo, reference=ref)


def _resolution(
    sha: str = SHA_A,
    *,
    ref_type: GitReferenceType = GitReferenceType.BRANCH,
    ref_name: str = "main",
) -> RefResolution:
    return RefResolution(
        ref_type=ref_type,
        resolved_commit=sha,
        ref_name=ref_name,
    )


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("update_refs", "refresh", "expected"),
    [
        (False, False, RefFreshnessPolicy.REPRODUCIBLE),
        (True, False, RefFreshnessPolicy.CURRENT_REMOTE),
        (False, True, RefFreshnessPolicy.CURRENT_REMOTE),
        (True, True, RefFreshnessPolicy.CURRENT_REMOTE),
    ],
)
def test_freshness_policy_maps_install_intent_once(update_refs, refresh, expected):
    policy = RefFreshnessPolicy.for_install_intent(
        update_refs=update_refs,
        refresh=refresh,
    )

    assert policy is expected
    assert policy.requires_remote is (expected is RefFreshnessPolicy.CURRENT_REMOTE)
    assert policy.allows_lock_seed is (expected is RefFreshnessPolicy.REPRODUCIBLE)
    assert policy.allows_bare_cache is (expected is RefFreshnessPolicy.REPRODUCIBLE)


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, True),
        ("1", True),
        ("true", True),
        ("yes", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("no", False),
        ("off", False),
        ("", False),
    ],
)
def test_is_tiered_resolver_enabled(value, expected, monkeypatch):
    if value is None:
        monkeypatch.delenv("APM_TIERED_RESOLVER", raising=False)
    else:
        monkeypatch.setenv("APM_TIERED_RESOLVER", value)
    assert is_tiered_resolver_enabled() is expected


# ---------------------------------------------------------------------------
# PerRunRefCache + L0
# ---------------------------------------------------------------------------


def test_per_run_cache_roundtrip():
    cache = PerRunRefCache()
    assert cache.get("owner/repo", "main") is None
    resolution = _resolution()
    cache.put("owner/repo", "main", resolution)
    assert cache.get("owner/repo", "main") == resolution
    assert cache.size() == 1


def test_l0_per_run_cache_tier_hits_cache():
    cache = PerRunRefCache()
    resolution = _resolution()
    cache.put(_repository_cache_identity(_dep()), "main", resolution)
    tier = L0PerRunCache(cache=cache)
    assert tier.try_resolve(_dep(), "main") == resolution


def test_l0_per_run_cache_keeps_same_path_on_different_hosts_separate():
    cache = PerRunRefCache()
    github_dep = DependencyReference(
        repo_url="acme/platform/team/repo-a",
        host="github.com",
        reference="main",
    )
    gitlab_dep = DependencyReference(
        repo_url="acme/platform/team/repo-a",
        host="gitlab.com",
        reference="main",
    )
    cache.put(_repository_cache_identity(github_dep), "main", _resolution())
    tier = L0PerRunCache(cache=cache)

    assert tier.try_resolve(gitlab_dep, "main") is None


def test_l0_per_run_cache_tier_misses_when_cold():
    tier = L0PerRunCache(cache=PerRunRefCache())
    assert tier.try_resolve(_dep(), "main") is None


# ---------------------------------------------------------------------------
# L1 CommitsAPI
# ---------------------------------------------------------------------------


def test_l1_returns_sha_directly_when_ref_is_already_a_sha():
    host = MagicMock()
    tier = L1CommitsAPI(host=host)
    assert tier.try_resolve(_dep(ref=SHA_A), SHA_A) == _resolution(
        ref_type=GitReferenceType.COMMIT,
        ref_name=SHA_A,
    )


def test_l1_delegates_to_legacy_resolve_commit_sha_for_ref():
    legacy = MagicMock()
    legacy.resolve_commit_sha_for_ref.return_value = SHA_B
    host = MagicMock()
    host._refs = legacy
    tier = L1CommitsAPI(host=host)
    assert tier.try_resolve(_dep(), "main") == _resolution(SHA_B)
    legacy.resolve_commit_sha_for_ref.assert_called_once()


def test_l1_preserves_syntactic_tag_type():
    legacy = MagicMock()
    legacy.resolve_commit_sha_for_ref.return_value = SHA_B
    host = MagicMock()
    host._refs = legacy
    tier = L1CommitsAPI(host=host)

    result = tier.try_resolve(_dep(ref="v1.2.3"), "v1.2.3")

    assert result == _resolution(
        SHA_B,
        ref_type=GitReferenceType.TAG,
        ref_name="v1.2.3",
    )


def test_l1_current_remote_defers_named_ref_type_to_exact_lookup():
    legacy = MagicMock()
    legacy.resolve_commit_sha_for_ref.return_value = SHA_B
    host = MagicMock()
    host._refs = legacy
    tier = L1CommitsAPI(host=host, allow_syntax_type_hint=False)

    assert tier.try_resolve(_dep(ref="release"), "release") is None
    legacy.resolve_commit_sha_for_ref.assert_called_once()


def test_l1_returns_none_for_artifactory():
    dep = DependencyReference(
        repo_url="owner/repo", reference="main", artifactory_prefix="artifactory/github"
    )
    legacy = MagicMock()
    host = MagicMock()
    host._refs = legacy
    tier = L1CommitsAPI(host=host)
    assert tier.try_resolve(dep, "main") is None
    legacy.resolve_commit_sha_for_ref.assert_not_called()


def test_l1_returns_none_when_legacy_raises():
    legacy = MagicMock()
    legacy.resolve_commit_sha_for_ref.side_effect = RuntimeError("boom")
    host = MagicMock()
    host._refs = legacy
    tier = L1CommitsAPI(host=host)
    assert tier.try_resolve(_dep(), "main") is None


def test_l1_returns_none_when_host_has_no_refs():
    host = types.SimpleNamespace()  # no _refs attribute
    tier = L1CommitsAPI(host=host)
    assert tier.try_resolve(_dep(), "main") is None


# ---------------------------------------------------------------------------
# L2 BareRevParse
# ---------------------------------------------------------------------------


def test_l2_returns_none_when_no_git_cache():
    tier = L2BareRevParse(git_cache=None)
    assert tier.try_resolve(_dep(), "main") is None


def test_l2_returns_none_when_bare_dir_missing(tmp_path):
    fake_cache = types.SimpleNamespace(_db_root=tmp_path / "nonexistent")
    tier = L2BareRevParse(git_cache=fake_cache)
    assert tier.try_resolve(_dep(), "main") is None


def test_l2_short_circuits_on_sha_input():
    fake_cache = types.SimpleNamespace(_db_root=None)
    tier = L2BareRevParse(git_cache=fake_cache)
    assert tier.try_resolve(_dep(ref=SHA_A), SHA_A) == _resolution(
        ref_type=GitReferenceType.COMMIT,
        ref_name=SHA_A,
    )


@pytest.mark.parametrize(
    "dependency",
    [
        DependencyReference(repo_url="owner/repo", host="github.com", reference="main"),
        DependencyReference(
            repo_url="acme/platform/team/repo",
            host="gitlab.com",
            reference="main",
        ),
    ],
)
def test_l2_rev_parse_uses_git_cache_repository_identity(tmp_path, dependency):
    bare = tmp_path / cache_shard_key(dependency.to_github_url())
    bare.mkdir(parents=True)
    fake_cache = types.SimpleNamespace(_db_root=tmp_path)
    tier = L2BareRevParse(git_cache=fake_cache)

    resolution = _resolution()
    with patch.object(L2BareRevParse, "_rev_parse", return_value=resolution) as rp:
        result = tier.try_resolve(dependency, "main")

    assert result == resolution
    rp.assert_called_once_with(bare, "main")


# ---------------------------------------------------------------------------
# L2 RemoteRef
# ---------------------------------------------------------------------------


def test_l2_remote_ref_preserves_branch_type():
    resolver = MagicMock()
    resolver.resolve_remote_ref.return_value = ResolvedReference(
        original_ref="owner/repo#main",
        ref_type=GitReferenceType.BRANCH,
        resolved_commit=SHA_A,
        ref_name="main",
    )
    tier = L2RemoteRef(resolver=resolver)

    assert tier.try_resolve(_dep(), "main") == _resolution()
    resolver.resolve_remote_ref.assert_called_once_with(_dep(), "main")


def test_l2_remote_ref_preserves_tag_type():
    resolver = MagicMock()
    resolver.resolve_remote_ref.return_value = ResolvedReference(
        original_ref="owner/repo#release",
        ref_type=GitReferenceType.TAG,
        resolved_commit=SHA_B,
        ref_name="release",
    )
    tier = L2RemoteRef(resolver=resolver)

    assert tier.try_resolve(_dep(ref="release"), "release") == _resolution(
        SHA_B,
        ref_type=GitReferenceType.TAG,
        ref_name="release",
    )


def test_l2_remote_ref_returns_none_for_missing_or_ambiguous_ref():
    resolver = MagicMock()
    resolver.resolve_remote_ref.return_value = None
    tier = L2RemoteRef(resolver=resolver)

    assert tier.try_resolve(_dep(), "main") is None


# ---------------------------------------------------------------------------
# L3 LegacyClone
# ---------------------------------------------------------------------------


def test_l3_returns_sha_from_legacy_resolve():
    legacy = MagicMock()
    legacy.resolve.return_value = ResolvedReference(
        original_ref="owner/repo#main",
        ref_type=GitReferenceType.BRANCH,
        resolved_commit=SHA_C,
        ref_name="main",
    )
    tier = L3LegacyClone(legacy_resolver=legacy)
    assert tier.try_resolve(_dep(), "main") == _resolution(SHA_C)


def test_l3_returns_none_when_legacy_raises():
    legacy = MagicMock()
    legacy.resolve.side_effect = RuntimeError("network down")
    tier = L3LegacyClone(legacy_resolver=legacy)
    with pytest.raises(RuntimeError, match="network down"):
        tier.try_resolve(_dep(), "main")


def test_l3_resolve_full_passes_through():
    legacy = MagicMock()
    rr = ResolvedReference(
        original_ref="owner/repo#main",
        ref_type=GitReferenceType.BRANCH,
        resolved_commit=SHA_C,
        ref_name="main",
    )
    legacy.resolve.return_value = rr
    tier = L3LegacyClone(legacy_resolver=legacy)
    assert tier.resolve_full(_dep()) is rr


# ---------------------------------------------------------------------------
# TieredRefResolver orchestrator
# ---------------------------------------------------------------------------


def _make_legacy_with(sha: str) -> L3LegacyClone:
    inner = MagicMock()
    inner.resolve.return_value = ResolvedReference(
        original_ref="owner/repo#main",
        ref_type=GitReferenceType.BRANCH,
        resolved_commit=sha,
        ref_name="main",
    )
    return L3LegacyClone(legacy_resolver=inner)


def test_orchestrator_caches_after_first_resolve():
    cache = PerRunRefCache()
    counting_tier = MagicMock()
    counting_tier.name = "counting"
    counting_tier.try_resolve.return_value = _resolution()
    legacy = _make_legacy_with(SHA_A)
    resolver = TieredRefResolver(
        tiers=[L0PerRunCache(cache=cache), counting_tier, legacy],
        cache=cache,
        legacy=legacy,
    )

    r1 = resolver.resolve(_dep())
    r2 = resolver.resolve(_dep())
    r3 = resolver.resolve(_dep())

    assert r1.resolved_commit == SHA_A
    assert r2.resolved_commit == SHA_A
    assert r3.resolved_commit == SHA_A
    # First call: cold L0 miss -> counting tier. Subsequent calls: L0 hit.
    counting_tier.try_resolve.assert_called_once()
    assert resolver.stats["counting"] == 1
    assert resolver.stats["per_run_cache"] == 2


def test_seed_populates_l0_and_avoids_network_tier():
    """A seeded ref resolves via L0 without touching the network tier."""
    cache = PerRunRefCache()
    network_tier = MagicMock()
    network_tier.name = "commits_api"
    network_tier.try_resolve.return_value = _resolution(SHA_B)  # would win if reached
    legacy = _make_legacy_with(SHA_B)
    resolver = TieredRefResolver(
        tiers=[L0PerRunCache(cache=cache), network_tier, legacy],
        cache=cache,
        legacy=legacy,
    )

    assert resolver.seed("owner/repo", "main", SHA_A) is True

    result = resolver.resolve(_dep(ref="main"))

    assert result.resolved_commit == SHA_A  # seeded value, not the tier's
    network_tier.try_resolve.assert_not_called()
    assert resolver.stats["per_run_cache"] == 1
    assert resolver.stats["commits_api"] == 0


def test_sha_ref_counts_as_passthrough_not_commits_api():
    """An already-concrete SHA ref resolves with zero I/O and does NOT
    increment the commits-API tier counter."""
    cache = PerRunRefCache()
    network_tier = MagicMock()
    network_tier.name = "commits_api"
    network_tier.try_resolve.return_value = _resolution(SHA_B)
    legacy = _make_legacy_with(SHA_B)
    resolver = TieredRefResolver(
        tiers=[L0PerRunCache(cache=cache), network_tier, legacy],
        cache=cache,
        legacy=legacy,
    )

    result = resolver.resolve(_dep(ref=SHA_A))

    assert result.resolved_commit == SHA_A
    network_tier.try_resolve.assert_not_called()
    assert resolver.stats["commits_api"] == 0
    assert resolver.stats["sha_passthrough"] == 1


def test_seed_rejects_non_sha_and_empty_ref():
    """seed() is a no-op for non-commit SHAs or empty refs."""
    cache = PerRunRefCache()
    legacy = _make_legacy_with(SHA_A)
    resolver = TieredRefResolver(
        tiers=[L0PerRunCache(cache=cache), legacy],
        cache=cache,
        legacy=legacy,
    )

    assert resolver.seed("owner/repo", "main", "not-a-sha") is False
    assert resolver.seed("owner/repo", "", SHA_A) is False
    assert resolver.seed("owner/repo", "v1.0.0", "") is False
    assert cache.size() == 0


def test_seed_normalizes_sha_case():
    """A seeded upper-case SHA is stored and returned lower-cased."""
    cache = PerRunRefCache()
    legacy = _make_legacy_with(SHA_A)
    resolver = TieredRefResolver(
        tiers=[L0PerRunCache(cache=cache), legacy],
        cache=cache,
        legacy=legacy,
    )

    assert resolver.seed("owner/repo", "release", ("A" * 40)) is True
    result = resolver.resolve(_dep(ref="release"))
    assert result.resolved_commit == "a" * 40


def test_orchestrator_collapses_concurrent_resolves():
    cache = PerRunRefCache()
    legacy = _make_legacy_with(SHA_A)

    in_flight = threading.Event()
    can_continue = threading.Event()
    call_count = [0]

    def slow_tier(dep_ref, ref):
        call_count[0] += 1
        in_flight.set()
        can_continue.wait(timeout=2)
        return _resolution()

    slow = MagicMock()
    slow.name = "slow"
    slow.try_resolve.side_effect = slow_tier

    resolver = TieredRefResolver(
        tiers=[L0PerRunCache(cache=cache), slow, legacy],
        cache=cache,
        legacy=legacy,
    )

    results = []

    def run():
        results.append(resolver.resolve(_dep()))

    t1 = threading.Thread(target=run)
    t2 = threading.Thread(target=run)
    t1.start()
    in_flight.wait(timeout=2)
    t2.start()
    # Give T2 a moment to enter resolve() and queue on the event.
    threading.Event().wait(0.05)
    can_continue.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert len(results) == 2
    assert all(r.resolved_commit == SHA_A for r in results)
    # Critical assertion: even though two threads raced, only ONE
    # underlying tier call happened.
    assert call_count[0] == 1
    assert resolver.stats["coalesced"] >= 1


def test_orchestrator_falls_through_when_all_tiers_return_none():
    cache = PerRunRefCache()
    miss = MagicMock()
    miss.name = "miss"
    miss.try_resolve.return_value = None
    legacy = _make_legacy_with(SHA_B)
    resolver = TieredRefResolver(
        tiers=[L0PerRunCache(cache=cache), miss, legacy],
        cache=cache,
        legacy=legacy,
    )
    result = resolver.resolve(_dep())
    assert result.resolved_commit == SHA_B


def test_orchestrator_handles_string_input():
    cache = PerRunRefCache()
    cache.put(_repository_cache_identity(_dep()), "main", _resolution())
    legacy = _make_legacy_with(SHA_A)
    resolver = TieredRefResolver(
        tiers=[L0PerRunCache(cache=cache), legacy],
        cache=cache,
        legacy=legacy,
    )
    result = resolver.resolve("owner/repo#main")
    assert result.resolved_commit == SHA_A


def test_orchestrator_routes_no_ref_to_legacy():
    cache = PerRunRefCache()
    legacy = _make_legacy_with(SHA_C)
    miss = MagicMock()
    miss.name = "miss"
    miss.try_resolve.return_value = None
    resolver = TieredRefResolver(
        tiers=[L0PerRunCache(cache=cache), miss, legacy],
        cache=cache,
        legacy=legacy,
    )
    dep = DependencyReference(repo_url="owner/repo", reference=None)
    result = resolver.resolve(dep)
    assert result.resolved_commit == SHA_C
    # No-ref path skips the tier dispatch entirely.
    miss.try_resolve.assert_not_called()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_factory_returns_none_when_feature_flag_disabled(monkeypatch):
    monkeypatch.setenv("APM_TIERED_RESOLVER", "0")
    downloader = MagicMock()
    downloader._refs = MagicMock()
    assert build_tiered_ref_resolver(downloader=downloader) is None


def test_factory_returns_none_when_downloader_has_no_refs(monkeypatch):
    monkeypatch.setenv("APM_TIERED_RESOLVER", "1")
    downloader = types.SimpleNamespace()  # no _refs
    assert build_tiered_ref_resolver(downloader=downloader) is None


def test_factory_builds_full_stack_when_enabled(monkeypatch):
    monkeypatch.setenv("APM_TIERED_RESOLVER", "1")
    downloader = MagicMock()
    downloader._refs = MagicMock()
    resolver = build_tiered_ref_resolver(downloader=downloader)
    assert isinstance(resolver, TieredRefResolver)
    # 4 tiers: L0, L1, L2, L3
    assert len(resolver._tiers) == 4
    tier_names = [t.name for t in resolver._tiers]
    assert tier_names == ["per_run_cache", "commits_api", "bare_rev_parse", "legacy_clone"]


# ---------------------------------------------------------------------------
# Factory -- update_refs behaviour (#2342)
# ---------------------------------------------------------------------------


def test_factory_excludes_l2_when_update_refs_true(monkeypatch):
    """update_refs=True must remove L2BareRevParse from the stack (#2342).

    L2 reads the local bare-repo cache without fetching from remote, so
    during update/outdated runs it would silently return a stale SHA.
    Excluding it forces resolution through L1 (CommitsAPI) and L3 (legacy
    clone), both of which contact the network.
    """
    monkeypatch.setenv("APM_TIERED_RESOLVER", "1")
    downloader = MagicMock()
    downloader._refs = MagicMock()
    resolver = build_tiered_ref_resolver(
        downloader=downloader,
        freshness_policy=RefFreshnessPolicy.CURRENT_REMOTE,
    )
    assert isinstance(resolver, TieredRefResolver)
    tier_names = [t.name for t in resolver._tiers]
    # bare_rev_parse must NOT be present
    assert "bare_rev_parse" not in tier_names
    # L0, L1, exact remote ref, and L3 remain.
    assert tier_names == ["per_run_cache", "commits_api", "remote_ref", "legacy_clone"]


def test_factory_includes_l2_when_update_refs_false(monkeypatch):
    """update_refs=False (default install) must retain L2BareRevParse.

    This is a regression trap: the bare-rev-parse tier is a performance
    optimisation for non-update runs and must not be accidentally removed.
    """
    monkeypatch.setenv("APM_TIERED_RESOLVER", "1")
    downloader = MagicMock()
    downloader._refs = MagicMock()
    resolver = build_tiered_ref_resolver(
        downloader=downloader,
        freshness_policy=RefFreshnessPolicy.REPRODUCIBLE,
    )
    assert isinstance(resolver, TieredRefResolver)
    tier_names = [t.name for t in resolver._tiers]
    assert "bare_rev_parse" in tier_names
    assert tier_names == ["per_run_cache", "commits_api", "bare_rev_parse", "legacy_clone"]


def test_stale_bare_bypassed_on_update(monkeypatch, tmp_path):
    """With update_refs=True, a stale local bare SHA is never returned (#2342).

    Scenario:
    - L2BareRevParse would return SHA_A (stale cached value).
    - L1 CommitsAPI returns SHA_B (fresh upstream value).
    - When update_refs=True, L2 is excluded so the resolver returns SHA_B.
    - When update_refs=False, L2 is in the stack but L1 fires first anyway,
      so SHA_B is returned and the stale path is never reached in normal flow.
      The important invariant is that in update mode, L2's stale answer can
      never surface even if L1 were somehow bypassed.
    """
    monkeypatch.setenv("APM_TIERED_RESOLVER", "1")

    bare = tmp_path / cache_shard_key(_dep().to_github_url())
    bare.mkdir(parents=True)
    git_cache = types.SimpleNamespace(_db_root=tmp_path)
    downloader = MagicMock()
    fake_refs = MagicMock()
    fake_refs.resolve_commit_sha_for_ref.return_value = None
    fake_refs.resolve_remote_ref.return_value = ResolvedReference(
        original_ref="owner/repo#main",
        ref_type=GitReferenceType.BRANCH,
        resolved_commit=SHA_B,
        ref_name="main",
    )
    downloader._refs = fake_refs

    resolver = build_tiered_ref_resolver(
        downloader=downloader,
        git_cache=git_cache,
        freshness_policy=RefFreshnessPolicy.CURRENT_REMOTE,
    )
    assert isinstance(resolver, TieredRefResolver)

    with patch.object(L2BareRevParse, "_rev_parse", return_value=_resolution()) as stale_l2:
        result = resolver.resolve(_dep(repo="owner/repo", ref="main"))

    assert result.resolved_commit == SHA_B
    stale_l2.assert_not_called()
    fake_refs.resolve_commit_sha_for_ref.assert_called_once()
    fake_refs.resolve_remote_ref.assert_called_once()
    fake_refs.resolve.assert_not_called()
    assert resolver.stats["remote_ref"] == 1
    assert resolver.stats["legacy_clone"] == 0
    assert "bare_rev_parse" not in resolver.stats


def test_normal_policy_uses_l2_when_api_unavailable_without_clone(monkeypatch, tmp_path):
    """A normal warm install keeps the zero-network L2 performance boundary."""
    monkeypatch.setenv("APM_TIERED_RESOLVER", "1")
    bare = tmp_path / cache_shard_key(_dep().to_github_url())
    bare.mkdir(parents=True)
    git_cache = types.SimpleNamespace(_db_root=tmp_path)
    downloader = MagicMock()
    fake_refs = MagicMock()
    fake_refs.resolve_commit_sha_for_ref.return_value = None
    downloader._refs = fake_refs
    resolver = build_tiered_ref_resolver(
        downloader=downloader,
        git_cache=git_cache,
        freshness_policy=RefFreshnessPolicy.REPRODUCIBLE,
    )
    assert isinstance(resolver, TieredRefResolver)

    with patch.object(L2BareRevParse, "_rev_parse", return_value=_resolution()) as cached_l2:
        result = resolver.resolve(_dep())

    assert result.resolved_commit == SHA_A
    cached_l2.assert_called_once_with(bare, "main")
    fake_refs.resolve_commit_sha_for_ref.assert_called_once()
    fake_refs.resolve.assert_not_called()
    assert resolver.stats["bare_rev_parse"] == 1
    assert resolver.stats["legacy_clone"] == 0


def test_current_policy_fails_closed_when_remote_tiers_fail(monkeypatch, tmp_path):
    """Freshness-required resolution never substitutes a stale L2 answer."""
    monkeypatch.setenv("APM_TIERED_RESOLVER", "1")
    bare = tmp_path / cache_shard_key(_dep().to_github_url())
    bare.mkdir(parents=True)
    downloader = MagicMock()
    fake_refs = MagicMock()
    fake_refs.resolve_commit_sha_for_ref.return_value = None
    fake_refs.resolve_remote_ref.return_value = None
    fake_refs.resolve.side_effect = RuntimeError("remote unavailable")
    downloader._refs = fake_refs
    resolver = build_tiered_ref_resolver(
        downloader=downloader,
        git_cache=types.SimpleNamespace(_db_root=tmp_path),
        freshness_policy=RefFreshnessPolicy.CURRENT_REMOTE,
    )
    assert isinstance(resolver, TieredRefResolver)

    with (
        patch.object(L2BareRevParse, "_rev_parse", return_value=_resolution()) as stale_l2,
        pytest.raises(RuntimeError, match="remote unavailable"),
    ):
        resolver.resolve(_dep())

    stale_l2.assert_not_called()
    assert resolver._cache.size() == 0
    assert "bare_rev_parse" not in resolver.stats


def test_current_policy_remote_tag_type_survives_cache_hit(monkeypatch):
    monkeypatch.setenv("APM_TIERED_RESOLVER", "1")
    downloader = MagicMock()
    fake_refs = MagicMock()
    fake_refs.resolve_commit_sha_for_ref.return_value = None
    fake_refs.resolve_remote_ref.return_value = ResolvedReference(
        original_ref="owner/repo#release",
        ref_type=GitReferenceType.TAG,
        resolved_commit=SHA_B,
        ref_name="release",
    )
    downloader._refs = fake_refs
    resolver = build_tiered_ref_resolver(
        downloader=downloader,
        freshness_policy=RefFreshnessPolicy.CURRENT_REMOTE,
    )
    assert isinstance(resolver, TieredRefResolver)
    dep = _dep(ref="release")

    first = resolver.resolve(dep)
    second = resolver.resolve(dep)

    assert first.ref_type is GitReferenceType.TAG
    assert second.ref_type is GitReferenceType.TAG
    assert first.resolved_commit == second.resolved_commit == SHA_B
    fake_refs.resolve_remote_ref.assert_called_once()
    fake_refs.resolve.assert_not_called()
    assert resolver.stats["remote_ref"] == 1
    assert resolver.stats["per_run_cache"] == 1


def test_current_policy_runs_exact_remote_after_commits_api(monkeypatch):
    monkeypatch.setenv("APM_TIERED_RESOLVER", "1")
    calls = []
    fake_refs = MagicMock()

    def commits_api(dep_ref, ref):
        calls.append("commits_api")
        return SHA_A

    def remote_ref(dep_ref, ref):
        calls.append("remote_ref")
        return ResolvedReference(
            original_ref=str(dep_ref),
            ref_type=GitReferenceType.BRANCH,
            resolved_commit=SHA_B,
            ref_name=ref,
        )

    fake_refs.resolve_commit_sha_for_ref.side_effect = commits_api
    fake_refs.resolve_remote_ref.side_effect = remote_ref
    downloader = MagicMock()
    downloader._refs = fake_refs
    resolver = build_tiered_ref_resolver(
        downloader=downloader,
        freshness_policy=RefFreshnessPolicy.CURRENT_REMOTE,
    )
    assert isinstance(resolver, TieredRefResolver)

    result = resolver.resolve(_dep())

    assert calls == ["commits_api", "remote_ref"]
    assert result.resolved_commit == SHA_B
    fake_refs.resolve.assert_not_called()


def test_current_policy_clone_fallback_runs_once_after_remote_miss(monkeypatch):
    monkeypatch.setenv("APM_TIERED_RESOLVER", "1")
    downloader = MagicMock()
    fake_refs = MagicMock()
    fake_refs.resolve_commit_sha_for_ref.return_value = SHA_A
    fake_refs.resolve_remote_ref.return_value = None
    fake_refs.resolve.return_value = ResolvedReference(
        original_ref="owner/repo#release",
        ref_type=GitReferenceType.TAG,
        resolved_commit=SHA_C,
        ref_name="release",
    )
    downloader._refs = fake_refs
    resolver = build_tiered_ref_resolver(
        downloader=downloader,
        freshness_policy=RefFreshnessPolicy.CURRENT_REMOTE,
    )
    assert isinstance(resolver, TieredRefResolver)

    result = resolver.resolve(_dep(ref="release"))

    assert result.ref_type is GitReferenceType.TAG
    assert result.resolved_commit == SHA_C
    fake_refs.resolve_commit_sha_for_ref.assert_called_once()
    fake_refs.resolve_remote_ref.assert_called_once()
    fake_refs.resolve.assert_called_once()
    assert resolver.stats["legacy_clone"] == 1


def test_current_policy_coalesces_equivalent_normalized_urls(monkeypatch):
    monkeypatch.setenv("APM_TIERED_RESOLVER", "1")
    downloader = MagicMock()
    fake_refs = MagicMock()
    fake_refs.resolve_commit_sha_for_ref.return_value = None
    fake_refs.resolve_remote_ref.return_value = ResolvedReference(
        original_ref="owner/repo#main",
        ref_type=GitReferenceType.BRANCH,
        resolved_commit=SHA_A,
        ref_name="main",
    )
    downloader._refs = fake_refs
    resolver = build_tiered_ref_resolver(
        downloader=downloader,
        freshness_policy=RefFreshnessPolicy.CURRENT_REMOTE,
    )
    assert isinstance(resolver, TieredRefResolver)
    implicit = DependencyReference(repo_url="owner/repo", reference="main")
    explicit = DependencyReference(
        repo_url="owner/repo",
        host="github.com",
        reference="main",
    )

    first = resolver.resolve(implicit)
    second = resolver.resolve(explicit)

    assert first.resolved_commit == second.resolved_commit == SHA_A
    fake_refs.resolve_remote_ref.assert_called_once()
