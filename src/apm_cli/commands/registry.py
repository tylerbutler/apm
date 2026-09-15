"""Canonical static registry for top-level APM CLI commands."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class CommandAlias:
    """One top-level alias exposed for a registered command."""

    name: str
    target: str
    attribute: str | None = None
    help: str = ""
    short_help: str | None = None
    hidden: bool = False
    clone: bool = False


@dataclass(frozen=True)
class CommandSpec:
    """Import and root-help metadata for one canonical top-level command."""

    name: str
    module: str
    attribute: str
    help: str
    short_help: str | None = None
    hidden: bool = False
    aliases: tuple[CommandAlias, ...] = ()


@dataclass(frozen=True)
class CommandEntry:
    """Resolved top-level command or alias metadata."""

    name: str
    module: str
    attribute: str
    help: str
    short_help: str | None
    hidden: bool
    alias_for: str | None = None
    clone: bool = False


COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec(
        name="approve",
        module="apm_cli.commands.approve",
        attribute="approve_cmd",
        help="Approve executable primitives (hooks, MCP, LSP, bin, canvas) for packages.",
    ),
    CommandSpec(
        name="audit",
        module="apm_cli.commands.audit",
        attribute="audit",
        help="Scan installed primitives for hidden Unicode, drift, and lockfile/policy violations",
    ),
    CommandSpec(
        name="cache",
        module="apm_cli.commands.cache",
        attribute="cache",
        help="Manage the local package cache",
    ),
    CommandSpec(
        name="compile",
        module="apm_cli.commands.compile",
        attribute="compile",
        help="Compile APM context into distributed AGENTS.md files",
    ),
    CommandSpec(
        name="config",
        module="apm_cli.commands.config",
        attribute="config",
        help="Configure APM CLI.",
    ),
    CommandSpec(
        name="deny",
        module="apm_cli.commands.approve",
        attribute="deny_cmd",
        help="Deny executable primitives for packages (a narrowing override).",
    ),
    CommandSpec(
        name="deps",
        module="apm_cli.commands.deps",
        attribute="deps",
        help="Manage APM package dependencies",
    ),
    CommandSpec(
        name="discover",
        module="apm_cli.commands.discover",
        attribute="discover",
        help="Alias for 'apm init --discover'.",
        hidden=True,
    ),
    CommandSpec(
        name="doctor",
        module="apm_cli.commands.doctor",
        attribute="doctor",
        help=(
            "Run environment diagnostics (git, network, auth, marketplace config). "
            "Reports a pass/fail table and exits non-zero if a critical check fails."
        ),
    ),
    CommandSpec(
        name="experimental",
        module="apm_cli.commands.experimental",
        attribute="experimental",
        help="Manage experimental feature flags",
    ),
    CommandSpec(
        name="find",
        module="apm_cli.commands.find",
        attribute="find",
        help="Trace a materialized file back to its contributing package(s).",
    ),
    CommandSpec(
        name="init",
        module="apm_cli.commands.init",
        attribute="init",
        help="Initialize a new APM project",
    ),
    CommandSpec(
        name="install",
        module="apm_cli.commands.install",
        attribute="install",
        help=(
            "Install APM, MCP, and LSP dependencies (supports APM packages, "
            "Claude skills (SKILL.md), and plugin collections (plugin.json); "
            "auto-creates apm.yml; use --allow-insecure for http:// packages)"
        ),
    ),
    CommandSpec(
        name="lifecycle",
        module="apm_cli.commands.lifecycle",
        attribute="lifecycle",
        help="Inspect, test, and scaffold lifecycle scripts.",
    ),
    CommandSpec(
        name="list",
        module="apm_cli.commands.list_cmd",
        attribute="list",
        help="List available scripts in the current project",
    ),
    CommandSpec(
        name="lock",
        module="apm_cli.commands.lock",
        attribute="lock",
        help="Resolve dependencies and write apm.lock.yaml without deploying or deleting files",
    ),
    CommandSpec(
        name="marketplace",
        module="apm_cli.commands.marketplace",
        attribute="marketplace",
        help="Manage marketplaces for discovery and governance",
        aliases=(
            CommandAlias(
                name="search",
                target="marketplace.search",
                attribute="search",
                help="Search plugins in a marketplace (QUERY@MARKETPLACE)",
            ),
        ),
    ),
    CommandSpec(
        name="mcp",
        module="apm_cli.commands.mcp",
        attribute="mcp",
        help="Discover, inspect, and install MCP servers",
    ),
    CommandSpec(
        name="outdated",
        module="apm_cli.commands.outdated",
        attribute="outdated",
        help="Show outdated locked dependencies",
    ),
    CommandSpec(
        name="pack",
        module="apm_cli.commands.pack",
        attribute="pack_cmd",
        help="Pack distributable artifacts from your APM project.",
    ),
    CommandSpec(
        name="plugin",
        module="apm_cli.commands.plugin",
        attribute="plugin",
        help="Scaffold and manage plugins (plugin-author workflows)",
    ),
    CommandSpec(
        name="policy",
        module="apm_cli.commands.policy",
        attribute="policy",
        help="Inspect and diagnose APM policy",
    ),
    CommandSpec(
        name="preview",
        module="apm_cli.commands.run",
        attribute="preview",
        help="Preview a script's compiled prompt files",
    ),
    CommandSpec(
        name="prune",
        module="apm_cli.commands.prune",
        attribute="prune",
        help=(
            "Remove APM packages absent from the resolved dependency graph and "
            "repair stale deployment owners"
        ),
    ),
    CommandSpec(
        name="publish",
        module="apm_cli.commands.publish",
        attribute="publish_cmd",
        help="Publish a package to a registry.",
    ),
    CommandSpec(
        name="run",
        module="apm_cli.commands.run",
        attribute="run",
        help="Run a script with parameters (experimental)",
    ),
    CommandSpec(
        name="runtime",
        module="apm_cli.commands.runtime",
        attribute="runtime",
        help="Manage AI runtimes (experimental)",
    ),
    CommandSpec(
        name="self-update",
        module="apm_cli.commands.self_update",
        attribute="self_update",
        help="Update the APM CLI binary itself to the latest version.",
    ),
    CommandSpec(
        name="targets",
        module="apm_cli.commands.targets",
        attribute="targets",
        help="Show resolved targets for the current project.",
    ),
    CommandSpec(
        name="uninstall",
        module="apm_cli.commands.uninstall",
        attribute="uninstall",
        help="Remove packages using manifest entries or direct locked keys from 'apm deps list'",
    ),
    CommandSpec(
        name="unpack",
        module="apm_cli.commands.pack",
        attribute="unpack_cmd",
        help=(
            "[Deprecated] Extract an APM bundle into the current project. "
            "Use 'apm install <bundle-path>' instead -- this command will be "
            "removed in a future release."
        ),
    ),
    CommandSpec(
        name="update",
        module="apm_cli.commands.update",
        attribute="update",
        help="Refresh APM dependencies to the latest matching refs",
    ),
    CommandSpec(
        name="view",
        module="apm_cli.commands.view",
        attribute="view",
        help="View package metadata or list remote versions.",
        short_help="View package metadata or list remote versions",
        aliases=(
            CommandAlias(
                name="info",
                target="view",
                help="View package metadata or list remote versions.",
                short_help="View package metadata or list remote versions",
                hidden=True,
                clone=True,
            ),
        ),
    ),
)


def _command_entries() -> tuple[CommandEntry, ...]:
    entries: list[CommandEntry] = []
    for spec in COMMAND_SPECS:
        entries.append(
            CommandEntry(
                name=spec.name,
                module=spec.module,
                attribute=spec.attribute,
                help=spec.help,
                short_help=spec.short_help,
                hidden=spec.hidden,
            )
        )
        entries.extend(
            CommandEntry(
                name=alias.name,
                module=spec.module,
                attribute=alias.attribute or spec.attribute,
                help=alias.help or spec.help,
                short_help=alias.short_help,
                hidden=alias.hidden,
                alias_for=alias.target,
                clone=alias.clone,
            )
            for alias in spec.aliases
        )
    return tuple(entries)


COMMAND_ENTRIES = _command_entries()
_COMMANDS_BY_NAME = MappingProxyType({entry.name: entry for entry in COMMAND_ENTRIES})
if len(_COMMANDS_BY_NAME) != len(COMMAND_ENTRIES):
    raise RuntimeError("CLI command registry contains duplicate top-level names")


def command_names() -> tuple[str, ...]:
    """Return every top-level command name in Click's stable display order."""
    return tuple(sorted(_COMMANDS_BY_NAME))


def public_command_names() -> tuple[str, ...]:
    """Return visible top-level command names in stable display order."""
    return tuple(name for name in command_names() if not _COMMANDS_BY_NAME[name].hidden)


def get_command_entry(name: str) -> CommandEntry | None:
    """Return static metadata for one top-level name."""
    return _COMMANDS_BY_NAME.get(name)


def command_modules() -> tuple[str, ...]:
    """Return the unique command modules referenced by the registry."""
    return tuple(sorted({entry.module for entry in COMMAND_ENTRIES}))


__all__ = [
    "COMMAND_ENTRIES",
    "COMMAND_SPECS",
    "CommandAlias",
    "CommandEntry",
    "CommandSpec",
    "command_modules",
    "command_names",
    "get_command_entry",
    "public_command_names",
]
