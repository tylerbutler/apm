"""Run-scoped ownership for one parsed project lockfile."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockFile


def _normalized_path(path: Path | None) -> Path:
    """Return a stable absolute path without requiring the file to exist."""
    if path is None:
        return (Path.cwd() / "apm.lock.yaml").absolute()
    return path.expanduser().absolute()


@dataclass
class LockfileSnapshot:
    """One invocation's current parsed lockfile, including known absence."""

    path: Path
    lockfile: LockFile | None

    def __post_init__(self) -> None:
        self.path = _normalized_path(self.path)

    @classmethod
    def load(cls, path: Path | None) -> LockfileSnapshot:
        """Read one lockfile at a standalone invocation boundary."""
        from apm_cli.deps.lockfile import LockFile

        normalized = _normalized_path(path)
        return cls(path=normalized, lockfile=LockFile.read(normalized))

    @classmethod
    def supplied(cls, path: Path, lockfile: LockFile | None) -> LockfileSnapshot:
        """Wrap a caller-supplied value, where ``None`` means known absent."""
        return cls(path=path, lockfile=lockfile)

    @classmethod
    def resolve(
        cls,
        path: Path | None,
        snapshot: LockfileSnapshot | None,
    ) -> LockfileSnapshot:
        """Reuse a supplied snapshot or load one for a standalone caller."""
        if snapshot is None:
            return cls.load(path)
        snapshot.require_path(path)
        return snapshot

    def require_path(self, path: Path | None) -> None:
        """Reject accidental reuse for a different project lockfile."""
        normalized = _normalized_path(path)
        if self.path != normalized:
            raise ValueError(
                f"Lockfile snapshot path mismatch: expected {self.path}, received {normalized}"
            )

    def replace(self, lockfile: LockFile | None) -> None:
        """Publish the current in-memory state after a successful write."""
        self.lockfile = lockfile
