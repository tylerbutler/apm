"""APM dependency management commands with lazy compatibility exports."""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [  # noqa: RUF022
    # CLI commands
    "deps",
    "list_packages",
    "tree",
    "clean",
    "update",
    "info",
    "why",
    # Utility functions (used by tests)
    "_is_nested_under_package",
    "_count_primitives",
    "_count_package_files",
    "_count_workflows",
    "_get_detailed_context_counts",
    "_get_package_display_info",
    "_get_detailed_package_info",
]

_CLI_EXPORTS = frozenset({"clean", "deps", "info", "list_packages", "tree", "update"})
_UTILITY_EXPORTS = frozenset(
    {
        "_count_package_files",
        "_count_primitives",
        "_count_workflows",
        "_get_detailed_context_counts",
        "_get_detailed_package_info",
        "_get_package_display_info",
        "_is_nested_under_package",
    }
)


def __getattr__(name: str) -> Any:
    """Load compatibility exports without importing the full deps command eagerly."""
    if name in _CLI_EXPORTS:
        module = import_module("apm_cli.commands.deps.cli")
    elif name in _UTILITY_EXPORTS:
        module = import_module("apm_cli.commands.deps._utils")
    elif name == "why":
        module = import_module("apm_cli.commands.deps.why")
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(module, name)
    globals()[name] = value
    return value
