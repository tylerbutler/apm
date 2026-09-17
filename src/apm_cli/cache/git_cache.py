"""Persistent content-addressable git cache.

Two-tier structure:
- ``git/db_v1/<shard>__p/`` -- blobless bare git repositories
- ``git/db_v1/<shard>/`` -- legacy full bare git repositories
- ``git/checkouts_v1/<shard>/<sha>/`` -- per-SHA working copies

Cache keys are derived from normalized repository URLs (see
:mod:`url_normalize`). Checkouts are keyed by resolved SHA, never
by mutable ref strings.

Resolution flow:
1. If lockfile provides SHA for this dep -> use directly
2. If ref looks like full SHA (40 hex chars) -> use as-is
3. Else ``git ls-remote <url> <ref>`` to resolve ref -> SHA

On every cache HIT:
- Run integrity check (verify HEAD == expected SHA)
- Mismatch -> evict shard, fall through to fresh fetch, log warning
- Refresh the SHA directory's access timestamp after successful validation

Concurrency:
- Per-shard file locks (via filelock) for atomic operations
- Atomic landing protocol for safe concurrent installs
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
from pathlib import Path

from ..utils.git_sparse import apply_sparse_cone, repair_dangling_cone_symlinks
from ..utils.path_security import ensure_path_within
from .integrity import verify_checkout_sha
from .locking import atomic_land, cleanup_incomplete, shard_lock, stage_path
from .paths import get_git_checkouts_path, get_git_db_path
from .url_normalize import cache_shard_key

_log = logging.getLogger(__name__)

# Full SHA pattern: 40 hex characters
_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_FALLBACK_REFSPECS = (
    "+refs/heads/*:refs/remotes/apm-fallback/*",
    "+refs/tags/*:refs/tags/*",
)


class CachePruneError(OSError):
    """Incomplete prune with completed-removal count and per-entry failures."""

    def __init__(self, pruned: int, failures: list[tuple[Path, OSError]]) -> None:
        self.pruned = pruned
        self.failures = tuple(failures)
        super().__init__(f"Pruned {pruned} SHA group(s); {len(failures)} failed.")


def _safe_git_args() -> list[str]:
    """Return hardening ``-c`` args prepended to every git subprocess.

    - The canonical no-hooks arguments disable any hook script that a
      malicious upstream might ship, so clone and checkout stay inert.
    - ``submodule.recurse=false`` prevents any subcommand from
      recursing into attacker-controlled submodule URLs.
    - ``core.autocrlf=false`` disables host autocrlf conversion so
      LF-committed blobs are not rewritten as CRLF on checkout.
      ``-c`` outranks host system / global config and
      ``GIT_CONFIG_KEY_n`` snapshots from ``git_network_env``, which
      otherwise win over a repo-local pin (apm#2971). This pin does
      not override ``core.eol`` or ``.gitattributes`` ``eol=crlf`` /
      ``text=auto`` requests.

    These flags are scoped per-invocation via ``-c`` and never mutate
    the user's gitconfig. The cache layer is the single source of
    truth for git subprocess invocation -- callers must use this
    helper rather than ad-hoc ``git`` argv construction.
    """
    from ..utils.git_env import git_long_paths_args, git_no_hooks_args

    return [
        *git_long_paths_args(),
        *git_no_hooks_args(),
        "-c",
        "submodule.recurse=false",
        "-c",
        "core.autocrlf=false",
    ]


# Blobless bare-cache flavor suffix (perf #1433 follow-up).
# New cache misses use ``<shard>__p`` cloned with ``--filter=blob:none``.
# Full and sparse checkout variants share that bare, while an existing
# legacy ``<shard>`` full bare remains reusable without migration.


def _checkout_pins_autocrlf_false(checkout_dir: Path) -> bool:
    """Return whether the checkout's local gitconfig pins ``core.autocrlf=false``.

    Pre-fix shards materialized under host ``core.autocrlf=true`` omit this
    pin and may contain CRLF working-tree bytes. Cache hits rematerialize
    those shards so existing Windows caches heal without ``apm cache clean``.
    """
    config = checkout_dir / ".git" / "config"
    if not config.is_file():
        return False
    try:
        text = config.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    section = None
    for raw in text.splitlines():
        line = raw.split(";", 1)[0].split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            inner = line[1:-1].strip()
            section = inner.split(" ", 1)[0].strip().lower()
            continue
        if section != "core" or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip().lower() != "autocrlf":
            continue
        normalized = value.strip().strip("\"'").lower()
        return normalized in {"false", "0", "no", "off"}
    return False


_PARTIAL_BARE_SUFFIX = "__p"
_BLOBLESS_DISABLED_MARKER = "apm-hydration-unsupported"


class _BloblessHydrationUnsupported(RuntimeError):
    """The remote accepted filtering but cannot hydrate promised blobs."""


def _partial_clone_filter_unsupported(exc: subprocess.CalledProcessError) -> bool:
    """Return whether Git diagnosed an unsupported partial-clone filter."""
    details: list[str] = []
    for value in (exc.stderr, exc.stdout):
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if value:
            details.append(str(value).lower())
    diagnostic = " ".join(details)
    return any(
        signal in diagnostic
        for signal in (
            "does not support filter",
            "filtering not recognized by server",
            "filter capability",
            "filter 'blob:none' not supported",
            "unknown option `filter=blob:none'",
            "unknown option 'filter=blob:none'",
        )
    )


def _partial_clone_hydration_unsupported(exc: subprocess.CalledProcessError) -> bool:
    """Return whether Git rejected fetching an unadvertised promised blob."""
    details: list[str] = []
    for value in (exc.stderr, exc.stdout):
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if value:
            details.append(str(value).lower())
    return "server does not allow request for unadvertised object" in " ".join(details)


def _partial_clone_fallback_warning(url: str) -> str:
    """Build a sanitized warning for a completed full-clone fallback."""
    return (
        f"Partial clone unavailable for {_sanitize_url(url)}; "
        "cached a full bare clone instead. Server may not support filter v2."
    )


def _variant_key(sparse_paths: list[str] | None) -> str:
    """Return the on-disk variant segment for a checkout shard.

    Layout (perf #1433):
      - ``full`` -- full-tree checkout (sparse_paths is None / empty).
      - ``sparse-<hash16>`` -- sparse-cone checkout where ``<hash16>`` is
        the first 16 hex chars of sha256(json.dumps(sorted(paths))).
        Two consumers requesting the same set of paths share a shard;
        different sets get separate shards. We do NOT promote a full
        checkout to also satisfy a sparse subset -- that complicates
        eviction for negligible benefit (each sparse shard is ~subdir
        size, so duplication cost is small).
    """
    if not sparse_paths:
        return "full"
    # Deduplicate AND sort so callers passing [a,a] or [a,b]+[b,a]
    # all collapse to the same variant key (the "set of paths"
    # semantics the docstring promises).
    payload = json.dumps(sorted(set(sparse_paths)), separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"sparse-{digest}"


class GitCache:
    """Content-addressable git cache with integrity verification.

    Args:
        cache_root: Root cache directory (from :func:`get_cache_root`).
        refresh: If True, force revalidation even on cache hit.
    """

    def __init__(self, cache_root: Path, *, refresh: bool = False) -> None:
        self._cache_root = cache_root
        self._refresh = refresh
        self._db_root = get_git_db_path(cache_root)
        self._checkouts_root = get_git_checkouts_path(cache_root)

        # Ensure bucket directories exist
        self._db_root.mkdir(parents=True, exist_ok=True)
        self._checkouts_root.mkdir(parents=True, exist_ok=True)
        os.chmod(str(self._db_root), 0o700)
        os.chmod(str(self._checkouts_root), 0o700)

        # Clean up any stale incomplete operations from previous crashes
        cleanup_incomplete(self._db_root)
        cleanup_incomplete(self._checkouts_root)

    def get_checkout(
        self,
        url: str,
        ref: str | None,
        *,
        locked_sha: str | None = None,
        env: dict[str, str] | None = None,
        sparse_paths: list[str] | None = None,
    ) -> Path:
        """Return path to a cached checkout for the given repo+ref.

        Args:
            url: Repository URL (any supported form).
            ref: Git ref (branch, tag, SHA) or None for default branch.
            locked_sha: If provided (from lockfile), skip resolution and
                use this SHA directly.
            env: Environment dict for git subprocesses.
            sparse_paths: If non-empty, materialize only these top-level
                directories using ``git sparse-checkout --cone``. The
                shard is keyed by ``(sha, sparse_paths_variant)`` so
                full and sparse variants of the same SHA coexist.

        Returns:
            Path to the checkout directory (guaranteed to contain valid
            git working copy at the expected SHA).
        """
        shard_key = cache_shard_key(url)
        sha = self._resolve_sha(url, ref, locked_sha=locked_sha, env=env)
        variant = _variant_key(sparse_paths)

        checkout_dir = self._checkouts_root / shard_key / sha / variant

        # Cache hit path (skip if refresh requested)
        if not self._refresh and checkout_dir.is_dir():
            sha_ok = verify_checkout_sha(checkout_dir, sha)
            if sha_ok and _checkout_pins_autocrlf_false(checkout_dir):
                _log.debug("Cache HIT: %s @ %s [%s]", _sanitize_url(url), sha[:12], variant)
                with shard_lock(checkout_dir):
                    self._seal_checkout_remote(checkout_dir, env=env)
                    finalized = self._finalize_sparse_checkout(
                        url,
                        checkout_dir,
                        sparse_paths,
                        env=env,
                    )
                    return self._record_checkout_access(finalized)
            elif not sha_ok:
                # Integrity failure -- evict
                _log.warning(
                    "[!] Evicting corrupt cache entry: %s @ %s [%s]",
                    _sanitize_url(url),
                    sha[:12],
                    variant,
                )
                self._evict_checkout(checkout_dir)
            # SHA-valid unpinned trees stay until ``_create_checkout`` holds
            # ``shard_lock`` and emits the rematerialize log. A concurrent
            # consumer may still be reading the old checkout.

        # Cache miss: new repositories use one blobless bare for both full and
        # sparse variants. Existing legacy full bares remain reusable.
        bare_dir = self._ensure_bare_repo(url, shard_key, sha, env=env, partial=True)
        promisor_url = url if self._bare_uses_blobless_filter(bare_dir, env=env) else None
        try:
            return self._create_checkout(
                url,
                shard_key,
                sha,
                env=env,
                sparse_paths=sparse_paths,
                promisor_url=promisor_url,
                bare_dir=bare_dir,
            )
        except _BloblessHydrationUnsupported:
            self._disable_blobless_bare(bare_dir)
            fallback_bare = self._ensure_bare_repo(
                url,
                shard_key,
                sha,
                env=env,
                partial=False,
            )
            result = self._create_checkout(
                url,
                shard_key,
                sha,
                env=env,
                sparse_paths=sparse_paths,
                promisor_url=None,
                bare_dir=fallback_bare,
            )
            from ..utils.console import _rich_warning

            _rich_warning(_partial_clone_fallback_warning(url))
            return result

    def find_cached_bare(self, url: str) -> Path | None:
        """Return the preferred existing bare for *url* without network I/O."""
        shard_key = cache_shard_key(url)
        partial_dir = self._db_root / f"{shard_key}{_PARTIAL_BARE_SUFFIX}"
        legacy_dir = self._db_root / shard_key
        candidates = (
            (legacy_dir, partial_dir)
            if self._blobless_bare_disabled(partial_dir)
            else (partial_dir, legacy_dir)
        )
        for candidate in candidates:
            ensure_path_within(candidate, self._db_root)
            if candidate.is_dir():
                return candidate
        return None

    def _record_checkout_access(self, checkout_dir: Path) -> Path:
        """Record successful reuse of a finalized checkout under its shard lock."""
        # Pruning ages the shared SHA root, not individual checkout variants.
        try:
            os.utime(checkout_dir.parent, None)
        except PermissionError as exc:
            _log.warning(
                "[!] Cannot update Git cache recency for %s: %s. "
                "Continuing with validated checkout; cache prune may evict it. "
                "Check cache permissions or set APM_CACHE_DIR to a writable directory.",
                checkout_dir.parent,
                exc,
            )
        return checkout_dir

    def _finalize_sparse_checkout(
        self,
        url: str,
        checkout_dir: Path,
        sparse_paths: list[str] | None,
        *,
        env: dict[str, str] | None,
    ) -> Path:
        """Repair and validate a sparse checkout before any cache return."""
        if not sparse_paths:
            return checkout_dir
        from ..utils.git_env import get_git_executable, git_promisor_env, git_subprocess_env

        git_exe = get_git_executable()
        subprocess_env = git_subprocess_env(env)
        dangling = repair_dangling_cone_symlinks(
            git_exe,
            checkout_dir,
            list(sparse_paths),
            env=subprocess_env,
            extra_git_args=_safe_git_args(),
            repair_env_factory=lambda: git_promisor_env(
                url,
                env,
                worktree=checkout_dir,
            ),
        )
        if dangling is not None:
            _log.info(
                "Sparse-cone checkout of %s left a dangling symlink at %s; "
                "widened to a full checkout so it resolves (#2707).",
                checkout_dir,
                dangling,
            )
        return checkout_dir

    def _resolve_sha(
        self,
        url: str,
        ref: str | None,
        *,
        locked_sha: str | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        """Resolve a ref to a full SHA.

        Priority:
        1. locked_sha from lockfile (trusted, no network)
        2. ref already looks like a full SHA
        3. git ls-remote to resolve ref -> SHA
        """
        if locked_sha and _SHA_RE.match(locked_sha):
            return locked_sha.lower()

        if ref and _SHA_RE.match(ref):
            return ref.lower()

        # Need to resolve via ls-remote
        return self._ls_remote_resolve(url, ref, env=env)

    def _ls_remote_resolve(
        self,
        url: str,
        ref: str | None,
        *,
        env: dict[str, str] | None = None,
    ) -> str:
        """Resolve a ref to SHA via git ls-remote.

        Args:
            url: Repository URL.
            ref: Ref to resolve (branch, tag, or None for HEAD).
            env: Environment for subprocess.

        Returns:
            40-char lowercase hex SHA.

        Raises:
            RuntimeError: If resolution fails.
        """
        from ..utils.git_env import git_remote_refs

        # auth-delegated: cache-layer ref resolution runs after lockfile
        # already pinned the commit; no PAT->bearer fallback applies here
        # (env is sanitized, no embedded creds).
        try:
            result = git_remote_refs(
                url,
                *((ref,) if ref else ()),
                timeout=30,
                env=env,
                git_args=_safe_git_args(),
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise RuntimeError(
                f"Failed to resolve ref '{ref}' for {_sanitize_url(url)}: {exc}"
            ) from exc

        if result.returncode != 0:
            raise RuntimeError(
                f"git ls-remote failed for {_sanitize_url(url)}: "
                f"{_sanitize_url(result.stderr.strip())}"
            )

        # Parse ls-remote output: first column is SHA
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t", 1)
            if len(parts) >= 1 and _SHA_RE.match(parts[0]):
                sha = parts[0].lower()
                # If no ref specified, return HEAD (first line)
                if not ref:
                    return sha
                # Match exact ref or refs/heads/ref or refs/tags/ref
                if len(parts) == 2:
                    remote_ref = parts[1]
                    if remote_ref in (
                        ref,
                        f"refs/heads/{ref}",
                        f"refs/tags/{ref}",
                    ):
                        return sha
        # If we have any SHA from output, use the first one
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t", 1)
            if len(parts) >= 1 and _SHA_RE.match(parts[0]):
                return parts[0].lower()

        raise RuntimeError(f"Could not resolve ref '{ref}' for {_sanitize_url(url)}")

    def _ensure_bare_repo(
        self,
        url: str,
        shard_key: str,
        sha: str,
        *,
        env: dict[str, str] | None = None,
        partial: bool = False,
    ) -> Path:
        """Ensure a bare repo clone exists for the given shard, fetching if needed.

        Args:
            partial: If True, clone with ``--filter=blob:none`` into a
                ``<shard>__p`` when no compatible bare exists. Existing
                ``<shard>__p`` and legacy ``<shard>`` bares remain reusable.
                A filter rejection falls back to a full clone in the selected
                directory.

        Returns the path to the bare repo directory.
        """
        from ..utils.git_env import get_git_executable, git_clone_env, git_no_templates_args

        partial_dir = self._db_root / f"{shard_key}{_PARTIAL_BARE_SUFFIX}"
        legacy_dir = self._db_root / shard_key
        partial_disabled = self._blobless_bare_disabled(partial_dir)
        if partial and partial_dir.is_dir() and not partial_disabled:
            bare_dir = partial_dir
        elif legacy_dir.is_dir():
            bare_dir = legacy_dir
        else:
            bare_dir = partial_dir if partial and not partial_disabled else legacy_dir
        use_filter = partial and not partial_disabled and bare_dir == partial_dir
        # Containment guard: defends against pathological shard_key
        # values bypassing the cache root.
        ensure_path_within(bare_dir, self._db_root)
        lock = shard_lock(bare_dir)

        # Acquire the shard lock BEFORE the existence probe so that two
        # concurrent processes hitting a cold shard cannot both perform
        # a full network clone (one would lose the atomic_land race
        # later, but only after wasting bandwidth + wall time).
        with lock:
            if bare_dir.is_dir():
                # Repo exists -- check if we have the required SHA
                if self._bare_has_sha(bare_dir, sha, env=env):
                    return bare_dir
                # Need to fetch the SHA (lock already held; call the
                # inner helper that does NOT re-acquire).
                self._fetch_into_bare_locked(bare_dir, url, sha, env=env)
                return bare_dir

            # Cold miss: clone bare repo
            git_exe = get_git_executable()
            staged = stage_path(bare_dir)
            ensure_path_within(staged, self._db_root)
            staged.mkdir(parents=True, exist_ok=True)
            os.chmod(str(staged), 0o700)

            subprocess_env = git_clone_env(url, env, staged, bare=True)
            clone_args = [
                git_exe,
                *_safe_git_args(),
                "clone",
                *git_no_templates_args(),
                "--bare",
                "--origin=origin",
                "--no-tags",
                "--no-recurse-submodules",
            ]
            if use_filter:
                # Promisor partial clone: trees + commits only. Blobs
                # arrive lazily via the remote when the consumer needs
                # them. Github / modern GHES / ADO support this; older
                # servers reject it and we retry without --filter.
                # --no-tags above skips fetching tag objects (release
                # tags can sum to MBs on monorepos); the cache is
                # SHA-keyed and never resolves via tags.
                clone_args += ["--filter=blob:none"]
            clone_args += [url, str(staged)]
            try:
                # Full bare clone (or partial when requested above). The
                # full path extracts file contents at checkout time, so
                # all blobs must be present locally. The partial path
                # relies on the consumer being configured as a promisor
                # so missing blobs trigger an on-demand fetch.
                subprocess.run(
                    clone_args,
                    capture_output=True,
                    text=True,
                    timeout=300,
                    env=subprocess_env,
                    stdin=subprocess.DEVNULL,
                    check=True,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
                # Partial clone fallback: some servers reject --filter
                # (old Gerrit / pre-2.20 GHE). Retry once without it so
                # we never block on this optimization. The resulting
                # bare is full; future sparse consumers find all blobs
                # locally and skip lazy fetch (degrades to baseline,
                # no behavior change for the user).
                fallback_done = False
                if (
                    use_filter
                    and isinstance(exc, subprocess.CalledProcessError)
                    and _partial_clone_filter_unsupported(exc)
                ):
                    from ..utils.file_ops import robust_rmtree

                    robust_rmtree(staged, ignore_errors=True)
                    staged.mkdir(parents=True, exist_ok=True)
                    os.chmod(str(staged), 0o700)
                    try:
                        subprocess.run(
                            [
                                git_exe,
                                *_safe_git_args(),
                                "clone",
                                *git_no_templates_args(),
                                "--bare",
                                "--origin=origin",
                                "--no-tags",
                                "--no-recurse-submodules",
                                url,
                                str(staged),
                            ],
                            capture_output=True,
                            text=True,
                            timeout=300,
                            env=subprocess_env,
                            stdin=subprocess.DEVNULL,
                            check=True,
                        )
                        fallback_done = True
                        from ..utils.console import _rich_warning

                        _rich_warning(_partial_clone_fallback_warning(url))
                    except (
                        subprocess.CalledProcessError,
                        subprocess.TimeoutExpired,
                        OSError,
                    ) as exc2:
                        from ..utils.file_ops import robust_rmtree

                        robust_rmtree(staged, ignore_errors=True)
                        raise RuntimeError(
                            f"Failed to clone {_sanitize_url(url)} "
                            f"(partial fallback also failed): {exc2}"
                        ) from exc2
                if not fallback_done:
                    # Clean up staged on failure
                    from ..utils.file_ops import robust_rmtree

                    robust_rmtree(staged, ignore_errors=True)
                    raise RuntimeError(f"Failed to clone {_sanitize_url(url)}: {exc}") from exc

            # Atomic land (lock is already held; pass it through so the
            # rename completes under the same critical section).
            if not atomic_land(staged, bare_dir, lock):
                # Another process won between our staging and rename
                # (possible only on lock-acquisition timeout fallthrough);
                # verify it has our SHA.
                if not self._bare_has_sha(bare_dir, sha, env=env):
                    self._fetch_into_bare_locked(bare_dir, url, sha, env=env)

            return bare_dir

    def _create_checkout(
        self,
        url: str,
        shard_key: str,
        sha: str,
        *,
        env: dict[str, str] | None = None,
        sparse_paths: list[str] | None = None,
        promisor_url: str | None = None,
        bare_dir: Path | None = None,
    ) -> Path:
        """Create a checkout at the specified SHA from the bare repo.

        Uses ``git clone --local --shared`` from the bare repo for
        efficiency (no network, hardlinks objects).

        Sparse-cone (perf #1433):
            When ``sparse_paths`` is non-empty, ``git sparse-checkout
            init --cone`` + ``set <paths...>`` runs BEFORE the SHA
            checkout, so the working tree contains only the requested
            top-level directories. The shard lives at
            ``checkouts_v1/<shard>/<sha>/sparse-<hash>/`` so it
            coexists with a possible full-tree shard at
            ``.../<sha>/full/`` for the same SHA.

        Partial-clone promisor (perf #1433 follow-up):
            When ``promisor_url`` is set, the bare lives at
            ``<shard>__p`` (cloned with ``--filter=blob:none``).
            Checkout receives the upstream URL and promisor settings only
            through process-scoped Git config, so required blobs can hydrate
            without persisting a network-capable remote in the checkout.

        Concurrency / write-deduplication
        ---------------------------------
        Acquires the shard lock BEFORE staging any work. On lock entry
        we re-probe the final shard and short-circuit if another
        process populated it while we were waiting on the lock.  This
        collapses N racing installs of the same SHA from N concurrent
        ``git clone`` operations to ~1: only the lock winner pays the
        clone cost; all losers see a populated shard the moment they
        get the lock and return immediately. Critical for CI matrix
        builds where multiple jobs hit the same uncached repo.
        """
        from ..utils.git_env import (
            get_git_executable,
            git_no_templates_args,
            git_promisor_env,
            git_subprocess_env,
        )

        if bare_dir is None:
            bare_dir = self.find_cached_bare(url)
            if bare_dir is None:
                bare_shard = shard_key + (_PARTIAL_BARE_SUFFIX if promisor_url else "")
                bare_dir = self._db_root / bare_shard
        ensure_path_within(bare_dir, self._db_root)
        variant = _variant_key(sparse_paths)
        # New layout: <shard>/<sha>/<variant>/. The <sha> level is the
        # SHA dir (parent to the variant). The <variant> level is what
        # the lock + atomic_land target so different variants of the
        # same SHA do not race each other.
        sha_parent = self._checkouts_root / shard_key / sha
        ensure_path_within(sha_parent, self._checkouts_root)
        sha_parent.mkdir(parents=True, exist_ok=True)
        os.chmod(str(sha_parent), 0o700)

        final_dir = sha_parent / variant
        ensure_path_within(final_dir, self._checkouts_root)
        lock = shard_lock(final_dir)

        # Acquire the lock BEFORE doing any work so that a concurrent
        # install of the same shard does not duplicate the clone work.
        # The lock winner clones; every other process re-probes after
        # the lock and short-circuits.
        with lock:
            # Write-dedup re-probe: another process may have populated
            # this shard while we were waiting. Verify integrity to
            # rule out a poisoned half-write (atomic_land guards
            # against that, but we re-check defensively).
            existing_ok = final_dir.is_dir() and verify_checkout_sha(final_dir, sha)
            if existing_ok and _checkout_pins_autocrlf_false(final_dir):
                _log.debug(
                    "Write-dedup HIT under lock: %s @ %s [%s]",
                    _sanitize_url(url),
                    sha[:12],
                    variant,
                )
                self._seal_checkout_remote(final_dir, env=env)
                return self._record_checkout_access(
                    self._finalize_sparse_checkout(url, final_dir, sparse_paths, env=env)
                )
            if existing_ok:
                _log.info(
                    "[*] Rematerializing git checkout missing core.autocrlf=false pin: "
                    "%s @ %s [%s]",
                    _sanitize_url(url),
                    sha[:12],
                    variant,
                )
                self._evict_checkout(final_dir)
                if final_dir.exists():
                    raise RuntimeError(
                        "Failed to rematerialize unpinned git checkout "
                        f"for {_sanitize_url(url)} @ {sha[:12]}"
                    )

            staged = stage_path(final_dir)
            ensure_path_within(staged, self._checkouts_root)
            staged.mkdir(parents=True, exist_ok=True)
            os.chmod(str(staged), 0o700)

            git_exe = get_git_executable()
            subprocess_env = git_subprocess_env(env)

            try:
                # Clone from the local bare repo without persisting the
                # upstream URL or promisor settings in the checkout.
                subprocess.run(
                    [
                        git_exe,
                        *_safe_git_args(),
                        "clone",
                        *git_no_templates_args(),
                        "--local",
                        "--shared",
                        "--no-checkout",
                        "--no-recurse-submodules",
                        str(bare_dir),
                        str(staged),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env=subprocess_env,
                    stdin=subprocess.DEVNULL,
                    check=True,
                )
                # Persist the pin so cache hits can recognize post-fix shards.
                # Checkout itself still needs ``-c core.autocrlf=false`` from
                # ``_safe_git_args`` because env-frozen host config outranks
                # this local value.
                subprocess.run(
                    [
                        git_exe,
                        *_safe_git_args(),
                        "-C",
                        str(staged),
                        "config",
                        "core.autocrlf",
                        "false",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    env=subprocess_env,
                    stdin=subprocess.DEVNULL,
                    check=True,
                )
                self._seal_checkout_remote(staged, env=env)
                if promisor_url:
                    subprocess_env = git_promisor_env(
                        promisor_url,
                        env,
                        worktree=staged,
                    )
                if sparse_paths:
                    # Sparse-cone setup BEFORE checkout. Failures raise
                    # (not silently fallen back to full checkout) because
                    # a silent fallback would re-introduce the disk
                    # bloat this code path exists to avoid (#1433).
                    apply_sparse_cone(
                        git_exe,
                        staged,
                        list(sparse_paths),
                        env=subprocess_env,
                        extra_git_args=_safe_git_args(),
                    )
                # Checkout the specific SHA
                subprocess.run(
                    [
                        git_exe,
                        *_safe_git_args(),
                        "-C",
                        str(staged),
                        "checkout",
                        sha,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env=subprocess_env,
                    stdin=subprocess.DEVNULL,
                    check=True,
                )
                if sparse_paths:
                    # Correctness repair, not a failure fallback (#2707):
                    # if the cone left a dangling symlink (target outside
                    # the requested paths), widen to a full checkout so
                    # it resolves. Only fires when the narrow cone would
                    # otherwise ship a broken checkout.
                    self._finalize_sparse_checkout(
                        url,
                        staged,
                        sparse_paths,
                        env=env,
                    )
            except (RuntimeError, ValueError):
                from ..utils.file_ops import robust_rmtree

                robust_rmtree(staged, ignore_errors=True)
                raise
            except subprocess.CalledProcessError as exc:
                from ..utils.file_ops import robust_rmtree

                robust_rmtree(staged, ignore_errors=True)
                if promisor_url and _partial_clone_hydration_unsupported(exc):
                    raise _BloblessHydrationUnsupported from exc
                raise RuntimeError(
                    f"Failed to create checkout for {_sanitize_url(url)} @ {sha[:12]}: {exc}"
                ) from exc
            except (subprocess.TimeoutExpired, OSError) as exc:
                from ..utils.file_ops import robust_rmtree

                robust_rmtree(staged, ignore_errors=True)
                raise RuntimeError(
                    f"Failed to create checkout for {_sanitize_url(url)} @ {sha[:12]}: {exc}"
                ) from exc

            # We hold the shard lock, so atomic_land's re-acquire is a
            # reentrant no-op (filelock supports same-process recursion).
            if not atomic_land(staged, final_dir, lock):
                # Another process landed first between our re-probe and
                # the rename (only possible if our lock dropped, which
                # it didn't); verify integrity and the autocrlf pin.
                if not (
                    verify_checkout_sha(final_dir, sha) and _checkout_pins_autocrlf_false(final_dir)
                ):
                    self._evict_checkout(final_dir)
                    raise RuntimeError(
                        f"Race condition: concurrent checkout failed integrity "
                        f"for {_sanitize_url(url)} @ {sha[:12]}"
                    )
            return final_dir

    def _seal_checkout_remote(
        self,
        checkout_dir: Path,
        *,
        env: dict[str, str] | None,
    ) -> None:
        """Remove persisted remotes so later commands cannot fetch implicitly."""
        config_path = checkout_dir / ".git" / "config"
        if not config_path.is_file():
            return
        try:
            config_text = config_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"Failed to inspect cached checkout configuration: {exc}") from exc
        if not re.search(r'(?m)^\[remote "origin"\]\s*$', config_text):
            return

        from ..utils.git_env import get_git_executable, git_subprocess_env

        result = subprocess.run(
            [
                get_git_executable(),
                *_safe_git_args(),
                "-C",
                str(checkout_dir),
                "remote",
                "remove",
                "origin",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env=git_subprocess_env(env),
            stdin=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("Failed to seal cached checkout remote configuration")

    @staticmethod
    def _blobless_bare_disabled(bare_dir: Path) -> bool:
        """Return whether hydration proved unusable for this blobless bare."""
        return (bare_dir / _BLOBLESS_DISABLED_MARKER).is_file()

    def _disable_blobless_bare(self, bare_dir: Path) -> None:
        """Persist a local marker so future requests use the full bare."""
        ensure_path_within(bare_dir, self._db_root)
        with shard_lock(bare_dir):
            marker = bare_dir / _BLOBLESS_DISABLED_MARKER
            marker.write_text("1\n", encoding="ascii")

    def _bare_has_sha(self, bare_dir: Path, sha: str, *, env: dict[str, str] | None = None) -> bool:
        """Check if the bare repo contains the specified commit."""
        from ..utils.git_env import get_git_executable, git_subprocess_env

        git_exe = get_git_executable()
        subprocess_env = git_subprocess_env(env)
        subprocess_env["GIT_NO_LAZY_FETCH"] = "1"
        try:
            result = subprocess.run(
                [git_exe, *_safe_git_args(), "--git-dir", str(bare_dir), "cat-file", "-t", sha],
                capture_output=True,
                text=True,
                timeout=10,
                env=subprocess_env,
                stdin=subprocess.DEVNULL,
            )
            return result.returncode == 0 and "commit" in result.stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            return False

    def _bare_uses_blobless_filter(
        self,
        bare_dir: Path,
        *,
        env: dict[str, str] | None = None,
    ) -> bool:
        """Return whether *bare_dir* is an active blobless partial clone."""
        from ..utils.git_env import get_git_executable, git_subprocess_env

        result = subprocess.run(
            [
                get_git_executable(),
                *_safe_git_args(),
                "--git-dir",
                str(bare_dir),
                "config",
                "--get-regexp",
                r"^remote\..*\.partialclonefilter$",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env=git_subprocess_env(env),
            stdin=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0 and any(
            line.rsplit(maxsplit=1)[-1] == "blob:none"
            for line in result.stdout.splitlines()
            if line.strip()
        )

    def _fetch_into_bare(
        self,
        bare_dir: Path,
        url: str,
        sha: str,
        *,
        env: dict[str, str] | None = None,
    ) -> None:
        """Fetch a specific SHA into an existing bare repo (acquires lock)."""
        lock = shard_lock(bare_dir)
        with lock:
            if self._bare_has_sha(bare_dir, sha, env=env):
                return
            self._fetch_into_bare_locked(bare_dir, url, sha, env=env)

    def _fetch_into_bare_locked(
        self,
        bare_dir: Path,
        url: str,
        sha: str,
        *,
        env: dict[str, str] | None = None,
    ) -> None:
        """Fetch a specific SHA into a bare repo. Caller MUST hold the shard lock."""
        from ..utils.git_env import get_git_executable, git_network_env

        git_exe = get_git_executable()
        subprocess_env = git_network_env(url, env, git_dir=bare_dir)
        # Preserve the filter only when the bare actually accepted it. A
        # filter-rejecting host can leave a full clone in the ``__p`` path.
        is_partial = self._bare_uses_blobless_filter(bare_dir, env=env)
        fetch_args = [git_exe, *_safe_git_args(), "--git-dir", str(bare_dir), "fetch"]
        if is_partial:
            fetch_args += ["--filter=blob:none"]
        fetch_args += [url, sha]
        try:
            subprocess.run(
                fetch_args,
                capture_output=True,
                text=True,
                timeout=120,
                env=subprocess_env,
                stdin=subprocess.DEVNULL,
                check=True,
            )
        except subprocess.CalledProcessError:
            # Some servers do not allow fetching by SHA. Broaden the explicit
            # validated remote without consulting any configured sibling remote.
            fallback_fetch_args = [
                git_exe,
                *_safe_git_args(),
                "--git-dir",
                str(bare_dir),
                "fetch",
            ]
            if is_partial:
                fallback_fetch_args += ["--filter=blob:none"]
            fallback_fetch_args += [url, *_FALLBACK_REFSPECS]
            subprocess.run(
                fallback_fetch_args,
                capture_output=True,
                text=True,
                timeout=120,
                env=subprocess_env,
                stdin=subprocess.DEVNULL,
                check=True,
            )

    def _evict_checkout(self, checkout_dir: Path) -> None:
        """Safely remove a corrupt checkout shard."""
        from ..utils.file_ops import robust_rmtree

        try:
            robust_rmtree(checkout_dir, ignore_errors=True)
        except Exception as exc:
            _log.debug("Failed to evict checkout %s: %s", checkout_dir, exc)

    def get_cache_stats(self) -> dict[str, int]:
        """Return cache statistics for ``apm cache info``.

        Returns:
            Dict with keys: db_count, checkout_count, total_size_bytes.
        """
        db_count = 0
        checkout_count = 0
        total_size = 0

        if self._db_root.is_dir():
            for entry in os.scandir(str(self._db_root)):
                if entry.is_dir(follow_symlinks=False) and not entry.name.endswith(".lock"):
                    db_count += 1
                    total_size += _dir_size(Path(entry.path))

        if self._checkouts_root.is_dir():
            for shard_entry in os.scandir(str(self._checkouts_root)):
                if shard_entry.is_dir(follow_symlinks=False):
                    for sha_entry in os.scandir(shard_entry.path):
                        if sha_entry.is_dir(follow_symlinks=False):
                            checkout_count += 1
                            total_size += _dir_size(Path(sha_entry.path))

        return {
            "db_count": db_count,
            "checkout_count": checkout_count,
            "total_size_bytes": total_size,
        }

    def clean_all(self) -> list[str]:
        """Remove db and checkouts, returning details of every incomplete removal."""
        from .cleanup import clean_cache_buckets

        return clean_cache_buckets((self._db_root, self._checkouts_root))

    def prune(self, *, max_age_days: int = 30) -> int:
        """Remove checkout entries older than *max_age_days*.

        Uses mtime of the shared SHA directory as the access indicator.
        Successfully reusing any checkout variant refreshes that timestamp.

        Returns:
            Number of SHA groups successfully removed.

        Raises:
            ValueError: If max_age_days is negative.
            CachePruneError: Some entries could not be inspected or removed.
                Other stale entries are still attempted. Completed removals
                and partially deleted entries are not rolled back.
        """
        import time

        from ..utils.file_ops import robust_rmtree

        if max_age_days < 0:
            raise ValueError("max_age_days must be nonnegative; use 0 or a positive number of days")

        cutoff = time.time() - (max_age_days * 86400)
        pruned = 0
        failures: list[tuple[Path, OSError]] = []

        if not self._checkouts_root.is_dir():
            return 0

        for shard_entry in os.scandir(str(self._checkouts_root)):
            if not shard_entry.is_dir(follow_symlinks=False):
                continue
            for sha_entry in os.scandir(shard_entry.path):
                if not sha_entry.is_dir(follow_symlinks=False):
                    continue
                try:
                    stat = sha_entry.stat(follow_symlinks=False)
                    if stat.st_mtime < cutoff:
                        robust_rmtree(Path(sha_entry.path))
                        pruned += 1
                except OSError as exc:
                    failures.append((Path(sha_entry.path), exc))

        if failures:
            raise CachePruneError(pruned, failures)
        return pruned


def _dir_size(path: Path) -> int:
    """Calculate total size of a directory (non-recursive symlink-safe)."""
    total = 0
    try:
        for root, _dirs, files in os.walk(str(path)):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    st = os.lstat(fp)
                    total += st.st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _sanitize_url(value: str) -> str:
    """Delegate Git diagnostic redaction to its canonical owner."""
    from ..utils.git_env import redact_git_diagnostic

    return redact_git_diagnostic(value)
