"""Tests for sparse-cone checkout support in GitCache (perf #1433)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

import pytest

from apm_cli.cache.git_cache import (
    GitCache,
    _BloblessHydrationUnsupported,
    _partial_clone_fallback_warning,
    _partial_clone_filter_unsupported,
    _partial_clone_hydration_unsupported,
    _variant_key,
)
from apm_cli.cache.url_normalize import cache_shard_key


@pytest.fixture(autouse=True)
def _allow_bare_repos(monkeypatch):
    """Override safe.bareRepository so `git -C <bare>` works in test env."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "safe.bareRepository")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "all")


class TestVariantKey:
    """Variant key derivation must be deterministic and order-independent."""

    def test_empty_or_none_is_full(self):
        assert _variant_key(None) == "full"
        assert _variant_key([]) == "full"

    def test_sparse_paths_produce_sparse_prefix(self):
        v = _variant_key(["plugins/x"])
        assert v.startswith("sparse-")
        # 16 hex chars after the prefix
        assert len(v) == len("sparse-") + 16

    def test_order_independent(self):
        assert _variant_key(["a", "b"]) == _variant_key(["b", "a"])

    def test_distinct_sets_distinct_keys(self):
        assert _variant_key(["a"]) != _variant_key(["b"])
        assert _variant_key(["a", "b"]) != _variant_key(["a"])

    def test_deterministic_across_calls(self):
        v1 = _variant_key(["plugins/x", "tools/y"])
        v2 = _variant_key(["tools/y", "plugins/x"])
        assert v1 == v2


def test_partial_clone_warning_redacts_url_credentials() -> None:
    """A completed filter fallback warning never exposes URL credentials."""
    warning = _partial_clone_fallback_warning(
        "https://alice:" + "example-token" + "@github.com/acme/private"
    )
    rendered_url = next(token.rstrip(";") for token in warning.split() if "://" in token)
    parsed = urlparse(rendered_url)

    assert parsed.hostname == "github.com"
    assert parsed.username == "***"
    assert parsed.password is None


def test_auth_failure_is_not_classified_as_filter_rejection() -> None:
    """An authentication failure must reach the outer AuthResolver immediately."""
    failure = subprocess.CalledProcessError(
        128,
        ("git", "clone"),
        stderr="fatal: Authentication failed",
    )

    assert _partial_clone_filter_unsupported(failure) is False


def test_unadvertised_object_failure_is_classified_as_hydration_rejection() -> None:
    """Protocol servers that cannot hydrate promised blobs use full fallback."""
    failure = subprocess.CalledProcessError(
        128,
        ("git", "checkout"),
        stderr="fatal: Server does not allow request for unadvertised object deadbeef",
    )

    assert _partial_clone_hydration_unsupported(failure) is True


@pytest.mark.parametrize(
    "diagnostic",
    (
        "fatal: server does not support filter",
        "error: unknown option `filter=blob:none'",
        "error: unknown option 'filter=blob:none'",
    ),
)
def test_filter_rejection_is_classified_for_bounded_fallback(diagnostic: str) -> None:
    """Only explicit filter-capability failures permit a full-clone retry."""
    failure = subprocess.CalledProcessError(
        128,
        ("git", "clone"),
        stderr=diagnostic,
    )

    assert _partial_clone_filter_unsupported(failure) is True


@pytest.mark.parametrize("dependency_count", (1, 10))
def test_sparse_cache_hit_defers_promisor_network_environment(
    tmp_path: Path,
    dependency_count: int,
) -> None:
    """A clean sparse hit performs no Git configuration probe."""
    cache = GitCache(tmp_path / "cache")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    env = {"PATH": os.environ["PATH"]}
    validated_env = {"VALIDATED": "1"}

    with (
        patch(
            "apm_cli.utils.git_env.git_promisor_env",
            return_value=validated_env,
        ) as validate,
        patch(
            "apm_cli.cache.git_cache.repair_dangling_cone_symlinks",
            return_value=None,
        ) as repair,
    ):
        for _ in range(dependency_count):
            result = cache._finalize_sparse_checkout(
                "https://git.example.com/acme/repo",
                checkout,
                ["skills/acme"],
                env=env,
            )

    assert result == checkout
    validate.assert_not_called()
    assert repair.call_count == dependency_count
    assert repair.call_args.kwargs["env"]["PATH"] == env["PATH"]
    repair_env_factory = repair.call_args.kwargs["repair_env_factory"]
    assert repair_env_factory() == validated_env
    validate.assert_called_once_with(
        "https://git.example.com/acme/repo",
        env,
        worktree=checkout,
    )


def _build_local_bare_repo(tmp_path: Path) -> tuple[Path, str]:
    """Create a local git repo with multiple top-level subdirs and a bare clone.

    Returns (bare_path, head_sha).
    """
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.email", "t@e"], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.name", "t"], check=True)

    for sub in ("alpha", "beta", "gamma"):
        d = work / sub
        d.mkdir()
        (d / "file.txt").write_text(f"{sub}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-q", "-m", "init"], check=True)
    sha = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    bare = tmp_path / "bare.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True)
    return bare, sha


class TestGetCheckoutLayout:
    """get_checkout must land checkouts at <shard>/<sha>/<variant>/."""

    def test_full_variant_layout(self, tmp_path: Path):
        bare, sha = _build_local_bare_repo(tmp_path)
        cache_root = tmp_path / "cache"
        cache = GitCache(cache_root)

        url = bare.as_uri()
        result = cache.get_checkout(url, "main", locked_sha=sha)
        assert result.name == "full"
        assert result.parent.name == sha
        assert (result / "alpha" / "file.txt").is_file()
        assert (result / "beta" / "file.txt").is_file()
        assert (result / "gamma" / "file.txt").is_file()

    def test_sparse_variant_layout_only_requested_subdir(self, tmp_path: Path):
        bare, sha = _build_local_bare_repo(tmp_path)
        cache_root = tmp_path / "cache"
        cache = GitCache(cache_root)

        url = bare.as_uri()
        result = cache.get_checkout(url, "main", locked_sha=sha, sparse_paths=["alpha"])
        assert result.name.startswith("sparse-")
        assert result.parent.name == sha
        assert (result / "alpha" / "file.txt").is_file()
        # Sparse-cone excludes other top-level dirs:
        assert not (result / "beta").exists()
        assert not (result / "gamma").exists()

    def test_full_and_sparse_coexist(self, tmp_path: Path):
        bare, sha = _build_local_bare_repo(tmp_path)
        cache_root = tmp_path / "cache"
        cache = GitCache(cache_root)

        url = bare.as_uri()
        full = cache.get_checkout(url, "main", locked_sha=sha)
        sparse = cache.get_checkout(url, "main", locked_sha=sha, sparse_paths=["alpha"])
        # Both live under same SHA parent, different variant subdirs.
        assert full.parent == sparse.parent
        assert full != sparse
        assert full.is_dir()
        assert sparse.is_dir()

    def test_two_distinct_sparse_sets_separate_shards(self, tmp_path: Path):
        bare, sha = _build_local_bare_repo(tmp_path)
        cache_root = tmp_path / "cache"
        cache = GitCache(cache_root)

        url = bare.as_uri()
        a = cache.get_checkout(url, "main", locked_sha=sha, sparse_paths=["alpha"])
        b = cache.get_checkout(url, "main", locked_sha=sha, sparse_paths=["beta"])
        assert a != b
        assert a.parent == b.parent
        assert (a / "alpha").is_dir()
        assert not (a / "beta").exists()
        assert (b / "beta").is_dir()
        assert not (b / "alpha").exists()


class TestPartialBareFlavor:
    """Full and sparse cache misses share one blobless bare."""

    def test_sparse_caller_uses_partial_bare_dir(self, tmp_path: Path):
        bare, sha = _build_local_bare_repo(tmp_path)
        cache_root = tmp_path / "cache"
        cache = GitCache(cache_root)

        url = bare.as_uri()
        cache.get_checkout(url, "main", locked_sha=sha, sparse_paths=["alpha"])

        # The partial-flavor bare lives at <shard>__p.
        bare_root = cache_root / "git" / "db_v1"
        partial_bares = [p for p in bare_root.iterdir() if p.is_dir() and p.name.endswith("__p")]
        assert len(partial_bares) == 1
        full_bares = [p for p in bare_root.iterdir() if p.is_dir() and not p.name.endswith("__p")]
        assert len(full_bares) == 0

    def test_full_caller_uses_partial_bare_dir(self, tmp_path: Path):
        bare, sha = _build_local_bare_repo(tmp_path)
        cache_root = tmp_path / "cache"
        cache = GitCache(cache_root)

        url = bare.as_uri()
        cache.get_checkout(url, "main", locked_sha=sha)

        bare_root = cache_root / "git" / "db_v1"
        partial_bares = [p for p in bare_root.iterdir() if p.is_dir() and p.name.endswith("__p")]
        assert len(partial_bares) == 1
        full_bares = [p for p in bare_root.iterdir() if p.is_dir() and not p.name.endswith("__p")]
        assert len(full_bares) == 0

    def test_full_and_sparse_callers_share_blobless_bare(self, tmp_path: Path):
        bare, sha = _build_local_bare_repo(tmp_path)
        cache_root = tmp_path / "cache"
        cache = GitCache(cache_root)

        url = bare.as_uri()
        cache.get_checkout(url, "main", locked_sha=sha)
        cache.get_checkout(url, "main", locked_sha=sha, sparse_paths=["alpha"])

        bare_root = cache_root / "git" / "db_v1"
        names = sorted(p.name for p in bare_root.iterdir() if p.is_dir())
        assert len(names) == 1
        assert sum(1 for n in names if n.endswith("__p")) == 1
        assert sum(1 for n in names if not n.endswith("__p")) == 0

    def test_existing_legacy_full_bare_remains_reusable(self, tmp_path: Path):
        bare, sha = _build_local_bare_repo(tmp_path)
        cache_root = tmp_path / "cache"
        cache = GitCache(cache_root)
        url = bare.as_uri()
        legacy = cache_root / "git" / "db_v1" / cache_shard_key(url)
        subprocess.run(["git", "clone", "-q", "--bare", url, str(legacy)], check=True)

        result = cache.get_checkout(url, "main", locked_sha=sha)

        assert (result / "alpha" / "file.txt").is_file()
        assert legacy.is_dir()
        assert not legacy.with_name(f"{legacy.name}__p").exists()

    def test_cached_bare_lookup_prefers_blobless_layout(self, tmp_path: Path):
        cache = GitCache(tmp_path / "cache")
        url = "https://git.example.com/acme/repo"
        shard = cache_shard_key(url)
        legacy = cache._db_root / shard
        blobless = cache._db_root / f"{shard}__p"
        legacy.mkdir()
        blobless.mkdir()

        assert cache.find_cached_bare(url) == blobless

    def test_cached_bare_lookup_prefers_legacy_after_hydration_rejection(self, tmp_path: Path):
        cache = GitCache(tmp_path / "cache")
        url = "https://git.example.com/acme/repo"
        shard = cache_shard_key(url)
        legacy = cache._db_root / shard
        blobless = cache._db_root / f"{shard}__p"
        legacy.mkdir()
        blobless.mkdir()
        cache._disable_blobless_bare(blobless)

        assert cache.find_cached_bare(url) == legacy

    def test_disabled_blobless_bare_creates_unfiltered_legacy_bare(
        self,
        tmp_path: Path,
        monkeypatch,
    ):
        cache = GitCache(tmp_path / "cache")
        url = "https://git.example.com/acme/repo"
        shard = cache_shard_key(url)
        blobless = cache._db_root / f"{shard}__p"
        blobless.mkdir()
        cache._disable_blobless_bare(blobless)
        clone_commands: list[list[str]] = []

        def fake_run(cmd, *args, **kwargs):
            argv = list(cmd)
            if "clone" in argv and "--bare" in argv:
                clone_commands.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr("apm_cli.cache.git_cache.subprocess.run", fake_run)
        monkeypatch.setattr("apm_cli.cache.git_cache.atomic_land", lambda *_args: True)

        result = cache._ensure_bare_repo(url, shard, "a" * 40, partial=True)

        assert result == cache._db_root / shard
        assert len(clone_commands) == 1
        assert "--filter=blob:none" not in clone_commands[0]

    def test_hydration_rejection_retries_with_legacy_full_bare(
        self,
        tmp_path: Path,
        monkeypatch,
    ):
        cache = GitCache(tmp_path / "cache")
        url = "https://git.example.com/acme/repo"
        sha = "a" * 40
        blobless = cache._db_root / f"{cache_shard_key(url)}__p"
        legacy = cache._db_root / cache_shard_key(url)
        blobless.mkdir()
        legacy.mkdir()
        checkout = tmp_path / "checkout"
        warnings: list[str] = []

        with (
            patch.object(cache, "_resolve_sha", return_value=sha),
            patch.object(cache, "_ensure_bare_repo", side_effect=[blobless, legacy]) as ensure,
            patch.object(cache, "_bare_uses_blobless_filter", return_value=True),
            patch.object(
                cache,
                "_create_checkout",
                side_effect=[_BloblessHydrationUnsupported(), checkout],
            ) as create,
            patch("apm_cli.utils.console._rich_warning", warnings.append),
        ):
            result = cache.get_checkout(url, "main")

        assert result == checkout
        assert cache._blobless_bare_disabled(blobless) is True
        assert ensure.call_count == 2
        assert ensure.call_args_list[1].kwargs["partial"] is False
        assert create.call_args_list[1].kwargs["bare_dir"] == legacy
        assert create.call_args_list[1].kwargs["promisor_url"] is None
        assert len(warnings) == 1

    @pytest.mark.parametrize("sparse_paths", (None, ["alpha"]))
    def test_promisor_config_is_not_persisted_on_consumer(
        self,
        tmp_path: Path,
        sparse_paths: list[str] | None,
    ):
        bare, sha = _build_local_bare_repo(tmp_path)
        cache_root = tmp_path / "cache"
        cache = GitCache(cache_root)

        url = bare.as_uri()
        result = cache.get_checkout(url, "main", locked_sha=sha, sparse_paths=sparse_paths)
        assert (result / "alpha" / "file.txt").is_file()

        # The checkout retains no remote. Upstream promisor settings are
        # supplied to the authenticated checkout process only.
        remotes = subprocess.run(
            ["git", "-C", str(result), "remote"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        assert remotes == []

    def test_promisor_setup_is_process_scoped(self, tmp_path: Path, monkeypatch):
        bare, sha = _build_local_bare_repo(tmp_path)
        cache = GitCache(tmp_path / "cache")

        real_run = subprocess.run
        checkout_envs: list[dict[str, str]] = []

        def _spy(cmd, *args, **kwargs):
            argv = [cmd] if isinstance(cmd, (str, bytes)) else list(cmd)
            if "checkout" in argv:
                checkout_envs.append(dict(kwargs["env"]))
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", _spy)

        url = bare.as_uri()
        cache.get_checkout(url, "main", locked_sha=sha, sparse_paths=["alpha"])

        assert len(checkout_envs) == 1
        config = {
            checkout_envs[0][f"GIT_CONFIG_KEY_{index}"]: checkout_envs[0][
                f"GIT_CONFIG_VALUE_{index}"
            ]
            for index in range(int(checkout_envs[0]["GIT_CONFIG_COUNT"]))
        }
        assert config["remote.apm-promisor.url"] == url
        assert config["remote.apm-promisor.promisor"] == "true"
        assert config["remote.apm-promisor.partialclonefilter"] == "blob:none"
        assert config["extensions.partialClone"] == "apm-promisor"

    def test_cache_hit_removes_legacy_persisted_promisor_remote(self, tmp_path: Path):
        bare, sha = _build_local_bare_repo(tmp_path)
        cache = GitCache(tmp_path / "cache")
        url = bare.as_uri()
        checkout = cache.get_checkout(url, "main", locked_sha=sha, sparse_paths=["alpha"])
        subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin", url], check=True)
        subprocess.run(
            ["git", "-C", str(checkout), "config", "remote.origin.promisor", "true"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(checkout),
                "config",
                "remote.origin.partialclonefilter",
                "blob:none",
            ],
            check=True,
        )

        reused = cache.get_checkout(url, "main", locked_sha=sha, sparse_paths=["alpha"])

        assert reused == checkout
        remotes = subprocess.run(
            ["git", "-C", str(reused), "remote"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        assert remotes == []

    def test_partial_clone_fallback_to_full_on_server_rejection(self, tmp_path: Path, monkeypatch):
        """Server rejects --filter=blob:none -> retry without filter succeeds.

        Older Gerrit / pre-2.20 GHE do not support filter v2. The cache
        must transparently degrade to a full bare clone (baseline
        behavior) rather than fail the install.
        """
        bare, sha = _build_local_bare_repo(tmp_path)
        cache_root = tmp_path / "cache"
        cache = GitCache(cache_root)

        import apm_cli.cache.git_cache as git_cache_mod

        real_run = subprocess.run
        rejected: list[list[str]] = []
        retried: list[list[str]] = []
        warnings: list[str] = []

        def fake_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and "--filter=blob:none" in cmd:
                rejected.append(list(cmd))
                raise subprocess.CalledProcessError(
                    128, cmd, output=b"", stderr=b"fatal: server does not support filter"
                )
            if (
                isinstance(cmd, list)
                and "clone" in cmd
                and "--bare" in cmd
                and "--filter=blob:none" not in cmd
            ):
                retried.append(list(cmd))
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(git_cache_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(
            "apm_cli.utils.console._rich_warning",
            warnings.append,
        )

        url = bare.as_uri()
        result = cache.get_checkout(url, "main", locked_sha=sha)

        assert rejected, "partial clone (with --filter) should have been attempted"
        assert retried, "fallback retry (without --filter) should have been issued"
        assert all("--filter=blob:none" not in c for c in retried)
        assert len(warnings) == 1
        assert "Partial clone unavailable" in warnings[0]
        assert "cached a full bare clone instead" in warnings[0]
        assert (result / "alpha" / "file.txt").is_file()
        fallback_bare = next(
            path for path in (cache_root / "git" / "db_v1").iterdir() if path.name.endswith("__p")
        )
        assert cache._bare_uses_blobless_filter(fallback_bare) is False

    def test_partial_clone_auth_failure_does_not_retry_full_clone(
        self,
        tmp_path: Path,
        monkeypatch,
    ):
        """Auth failure exits after one negotiation so AuthResolver can retry."""
        cache = GitCache(tmp_path / "cache")
        clone_commands: list[list[str]] = []

        def reject_auth(cmd, *args, **kwargs):
            argv = list(cmd)
            if "clone" in argv and "--bare" in argv:
                clone_commands.append(argv)
                raise subprocess.CalledProcessError(
                    128,
                    argv,
                    stderr="fatal: Authentication failed",
                )
            raise AssertionError(f"unexpected subprocess: {argv}")

        import apm_cli.cache.git_cache as git_cache_mod

        monkeypatch.setattr(git_cache_mod.subprocess, "run", reject_auth)

        with pytest.raises(RuntimeError, match="Failed to clone"):
            cache.get_checkout(
                "https://github.com/acme/private",
                "a" * 40,
                locked_sha="a" * 40,
            )

        assert len(clone_commands) == 1
        assert "--filter=blob:none" in clone_commands[0]


def _build_repo_with_out_of_cone_symlink_target(tmp_path: Path) -> tuple[Path, str]:
    """Repro shape for #2707: a symlink inside the cone, target outside it."""
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.email", "t@e"], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.name", "t"], check=True)

    cone_dir = work / "alpha" / "skill"
    cone_dir.mkdir(parents=True)
    (cone_dir / "ref.md").write_text("stand-in for a symlink entry\n")

    shared_dir = work / "shared"
    shared_dir.mkdir()
    (shared_dir / "ref.md").write_text("the real target content\n")

    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(work), "commit", "-q", "-m", "test: init fixture repo"], check=True
    )
    sha = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    bare = tmp_path / "bare-symlink.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True)
    return bare, sha


class TestDanglingSymlinkRepair:
    """#2707: GitCache's sparse-cone checkout must not ship a dangling symlink."""

    def test_dangling_symlink_in_cone_is_repaired(self, tmp_path: Path, monkeypatch):
        """A symlink whose target sits outside the requested cone must
        still resolve after ``get_checkout`` returns.

        This box can't create a real symlink (WinError 1314) and a real
        checkout here writes a mode-120000 entry as a plain file
        (``core.symlinks`` defaults to false on this filesystem), so the
        ``ref.md`` stand-in is monkeypatched to LOOK dangling the way a
        real symlink pointing at ``../../shared/ref.md`` would on a
        filesystem that honors ``core.symlinks`` -- same technique as
        tests/unit/utils/test_git_sparse.py and
        tests/unit/deps/test_bare_cache_sparse.py.
        """
        bare, sha = _build_repo_with_out_of_cone_symlink_target(tmp_path)
        cache_root = tmp_path / "cache"
        cache = GitCache(cache_root)

        checkout_dir = cache_root / "git" / "checkouts_v1"
        url = bare.as_uri()

        # The variant shard path isn't known until after get_checkout
        # resolves the sha/variant key, but the fake symlink's parent
        # (alpha/skill/ref.md) is deterministic once we know the shard
        # root, so match on the relative suffix instead.
        real_islink = os.path.islink
        rel_suffix = Path("alpha") / "skill" / "ref.md"

        def fake_islink(path):
            p = Path(path)
            return True if p.parts[-3:] == rel_suffix.parts else real_islink(path)

        def fake_exists(path):
            p = Path(path)
            if p.parts[-3:] == rel_suffix.parts:
                return (p.parents[2] / "shared" / "ref.md").exists()
            return os.path.lexists(path)

        monkeypatch.setattr(os.path, "islink", fake_islink)
        monkeypatch.setattr(os.path, "exists", fake_exists)
        monkeypatch.setattr(
            "apm_cli.utils.git_sparse._tracked_symlinks",
            lambda *args, **kwargs: [Path(args[1]) / "alpha" / "skill" / "ref.md"],
        )

        result = cache.get_checkout(url, "main", locked_sha=sha, sparse_paths=["alpha/skill"])

        assert checkout_dir in result.parents
        # The repair must have actually widened the tree: the previously
        # cone-excluded sibling holding the symlink's target now exists.
        assert (result / "shared" / "ref.md").is_file()
        assert (result / "shared" / "ref.md").read_text() == "the real target content\n"

    def test_no_dangling_symlink_cone_stays_narrow(self, tmp_path: Path):
        bare, sha = _build_repo_with_out_of_cone_symlink_target(tmp_path)
        cache_root = tmp_path / "cache"
        cache = GitCache(cache_root)

        url = bare.as_uri()
        result = cache.get_checkout(url, "main", locked_sha=sha, sparse_paths=["alpha/skill"])

        assert (result / "alpha" / "skill" / "ref.md").is_file()
        # No dangling symlink was ever reported (the stand-in is a plain
        # file), so the cone must stay narrow -- no repair should fire.
        assert not (result / "shared").exists()
