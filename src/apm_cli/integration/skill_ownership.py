"""Run-scoped ownership index for deployed skills."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockFile


class SkillOwnershipIndex:
    """Own lockfile-derived and same-run skill ownership decisions.

    The index preserves the existing last-entry-wins behavior while providing
    O(1) lookup by deployed-path leaf name and by normalized native skill path.
    """

    def __init__(self) -> None:
        self._leaf_owners: dict[str, str | None] = {}
        self._deployed_skill_owners: dict[str, str | None] = {}

    @staticmethod
    def normalize_deployed_path(deployed_path: str) -> str:
        """Normalize one lockfile deployed path for ownership lookup."""
        return deployed_path.replace("\\", "/").rstrip("/")

    @staticmethod
    def _is_native_skill_path(normalized_path: str) -> bool:
        """Return whether a deployed path belongs to a native skills tree."""
        return "/skills/" in normalized_path

    @classmethod
    def from_lockfile(cls, lockfile: LockFile | None) -> SkillOwnershipIndex:
        """Build the ownership index in one pass over an already parsed lockfile."""
        index = cls()
        if lockfile is None:
            return index
        for dependency in lockfile.get_package_dependencies():
            owner = dependency.get_unique_key()
            for deployed_path in dependency.deployed_files:
                index.claim(deployed_path, owner)
        return index

    @classmethod
    def from_ownership_maps(
        cls,
        leaf_owners: dict[str, str | None],
        deployed_skill_owners: dict[str, str | None],
    ) -> SkillOwnershipIndex:
        """Build an index from compatibility maps without deriving ownership."""
        index = cls()
        index._leaf_owners.update(leaf_owners)
        index._deployed_skill_owners.update(
            {
                cls.normalize_deployed_path(path): owner
                for path, owner in deployed_skill_owners.items()
            }
        )
        return index

    @classmethod
    def load(cls, project_root: Path) -> SkillOwnershipIndex:
        """Compatibility loader for standalone integrator callers."""
        from apm_cli.deps.lockfile import LockFile, get_lockfile_path

        return cls.from_lockfile(LockFile.read(get_lockfile_path(project_root)))

    @classmethod
    def load_for_cleanup(cls, project_root: Path) -> SkillOwnershipIndex:
        """Load ownership for cleanup, returning an empty index on unreadable state."""
        try:
            return cls.load(project_root)
        except (FileNotFoundError, OSError, KeyError, ValueError, TypeError, AttributeError) as exc:
            logging.getLogger(__name__).debug(
                "Could not read lockfile for ownership check: %s",
                exc,
            )
            return cls()

    def claim(self, deployed_path: str, owner: str | None) -> None:
        """Claim one successfully materialized or accepted deployed path."""
        normalized = self.normalize_deployed_path(deployed_path)
        if not normalized:
            return
        self._leaf_owners[normalized.rsplit("/", 1)[-1]] = owner
        if self._is_native_skill_path(normalized):
            self._deployed_skill_owners[normalized] = owner

    def owner_for_leaf(self, leaf_name: str) -> str | None:
        """Return the last owner of one deployed-path leaf name."""
        return self._leaf_owners.get(leaf_name)

    def owner_for_deployed_skill_path(self, deployed_path: str) -> str | None:
        """Return the owner of one exact normalized native skill path."""
        normalized = self.normalize_deployed_path(deployed_path)
        return self._deployed_skill_owners.get(normalized)

    def owns_deployed_skill_path(self, deployed_path: str) -> bool:
        """Return whether one exact normalized native skill path is claimed."""
        normalized = self.normalize_deployed_path(deployed_path)
        return normalized in self._deployed_skill_owners

    def deployed_skill_paths(self) -> set[str]:
        """Return all exact native skill paths currently claimed by the index."""
        return set(self._deployed_skill_owners)

    def owned_agent_skill_names(self) -> set[str]:
        """Return claimed top-level names under ``.agents/skills``."""
        prefix = ".agents/skills/"
        names: set[str] = set()
        for deployed_path in self._deployed_skill_owners:
            if not deployed_path.startswith(prefix):
                continue
            name = deployed_path[len(prefix) :].split("/", 1)[0]
            if name:
                names.add(name)
        return names

    def ownership_maps(self) -> tuple[dict[str, str | None], dict[str, str | None]]:
        """Return compatibility copies of the leaf and deployed-path maps."""
        return dict(self._leaf_owners), dict(self._deployed_skill_owners)

    def native_leaf_owners(self) -> dict[str, str | None]:
        """Return a compatibility leaf-name view of native skill ownership."""
        return {
            path.rsplit("/", 1)[-1]: owner for path, owner in self._deployed_skill_owners.items()
        }
