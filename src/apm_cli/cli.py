"""Command-line interface for Agent Package Manager (APM).

Thin wiring layer  -- all command logic lives in ``apm_cli.commands.*`` modules.
"""

import ctypes
import logging
import os
import sys
import warnings
from copy import copy
from importlib import import_module
from typing import Any

import click
from click.utils import make_default_short_help

from apm_cli.commands.registry import (
    command_names,
    get_command_entry,
)

_CLI_EPILOG = (
    "\b\n"
    "Common workflows:\n"
    "  apm init                       Scaffold a new project\n"
    "  apm install                    Install dependencies from apm.yml\n"
    "  apm install --frozen           Reproduce lockfile exactly (CI-safe)\n"
    "  apm lock                       Resolve deps only (no deploy/delete)\n"
    "  apm outdated                   See what's drifted from upstream\n"
    "  apm update                     Refresh refs and rewrite the lockfile\n"
    "  apm audit --ci                 Validate lockfile integrity for CI gates\n"
    "  apm doctor                     Diagnose environment problems\n"
    "  apm run <script>               Execute a script from apm.yml"
)


class _LazyCommandGroup(click.Group):
    """Resolve top-level commands lazily from the canonical static registry."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._command_cache: dict[str, click.Command] = {}

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        ctx.meta["apm_raw_args"] = tuple(args)
        return super().parse_args(ctx, args)

    def list_commands(self, ctx: click.Context) -> list[str]:
        """Return complete command discovery without importing command modules."""
        return list(command_names())

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        """Import and cache only the selected command."""
        entry = get_command_entry(cmd_name)
        if entry is None:
            return None

        if not ctx.meta.get("apm_command_tls_initialized"):
            _initialize_command_tls()
            ctx.meta["apm_command_tls_initialized"] = True

        cached = self._command_cache.get(cmd_name)
        if cached is not None:
            return cached

        module = import_module(entry.module)
        command = getattr(module, entry.attribute)
        if not isinstance(command, click.Command):
            raise TypeError(
                f"Registered CLI target {entry.module}:{entry.attribute} is not a Click command"
            )
        if entry.clone:
            command = copy(command)
            command.name = entry.name
            command.params = list(command.params)
            command.hidden = entry.hidden
        self._command_cache[cmd_name] = command
        return command

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        """Render root command help directly from static registry metadata."""
        entries = [
            entry
            for name in self.list_commands(ctx)
            if (entry := get_command_entry(name)) is not None and not entry.hidden
        ]
        if not entries:
            return

        limit = formatter.width - 6 - max(len(entry.name) for entry in entries)
        rows = [
            (
                entry.name,
                entry.short_help.strip()
                if entry.short_help
                else make_default_short_help(entry.help, limit),
            )
            for entry in entries
        ]
        with formatter.section("Commands"):
            formatter.write_dl(rows)


def _print_version(
    ctx: click.Context,
    param: click.Parameter | None,
    value: bool,
) -> None:
    """Load the lightweight version renderer only for ``--version``."""
    from apm_cli.version import print_version

    print_version(ctx, param, value)


def _check_and_notify_updates() -> None:
    """Load update notification helpers only for a valid command invocation."""
    from apm_cli.commands._helpers import _check_and_notify_updates as notify

    notify()


def _initialize_command_tls() -> None:
    """Initialize process TLS once before importing a selected command."""
    from apm_cli.core.tls_trust import configure_process_tls_trust

    configure_process_tls_trust()


def _log_command_tls_status() -> None:
    """Log the selected trust source after root logging is configured."""
    from apm_cli.core.tls_trust import log_tls_trust_status

    log_tls_trust_status()


def _configure_logging(verbose: bool = False) -> None:
    """Configure stdlib logging for the ``apm_cli`` package.

    Two mechanisms activate debug-level output (either is sufficient):

    * ``--verbose`` / ``-v`` flag on the ``apm`` command.
    * ``APM_LOG_LEVEL=DEBUG`` environment variable (accepts any stdlib
      level name: DEBUG, INFO, WARNING, ERROR, CRITICAL).

    When neither is set the ``apm_cli`` logger defaults to WARNING so
    routine runs stay silent.  A :class:`~apm_cli.core.auth.SecretRedactionFilter`
    is always installed on the ``apm_cli`` logger to strip token-bearing
    exception strings from debug records regardless of which mechanism
    activated debug mode.
    """
    env_level_str = os.environ.get("APM_LOG_LEVEL", "").strip().upper()
    env_level: int | None = (
        getattr(logging, env_level_str, None) if env_level_str.isalpha() else None
    )

    if verbose:
        level = logging.DEBUG
    elif env_level is not None:
        level = env_level
    else:
        level = logging.WARNING

    logging.basicConfig(
        level=level,
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    apm_logger = logging.getLogger("apm_cli")
    apm_logger.setLevel(level)

    # Install secret-redaction filter (idempotent: skip if already present).
    from apm_cli.core.auth import SecretRedactionFilter

    handlers = {
        *logging.getLogger().handlers,
        *apm_logger.handlers,
    }
    for handler in handlers:
        if not any(isinstance(f, SecretRedactionFilter) for f in handler.filters):
            handler.addFilter(SecretRedactionFilter())


@click.group(
    cls=_LazyCommandGroup,
    help="Agent Package Manager (APM): The package manager for AI-Native Development",
    epilog=_CLI_EPILOG,
)
@click.option(
    "--version",
    is_flag=True,
    callback=_print_version,
    expose_value=False,
    is_eager=True,
    help="Show version and exit.",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    default=False,
    help="Enable debug-level logging (equivalent to APM_LOG_LEVEL=DEBUG).",
)
@click.pass_context
def cli(ctx: click.Context, verbose: bool) -> None:
    """Main entry point for the APM CLI."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    from apm_cli.core.output_mode import configure_output_mode, detect_output_mode

    output_mode = detect_output_mode(ctx.meta.get("apm_raw_args", sys.argv[1:]))
    configure_output_mode(output_mode)
    ctx.obj["output_mode"] = output_mode

    if verbose:
        # Upgrade to DEBUG when the flag is set; env-var path runs in main().
        _configure_logging(verbose=True)
    _log_command_tls_status()

    # Suppress only the agents-target deprecation warning so CLI users see
    # the formatted logger.warning() in the install phase, not a double print.
    # Scoped to AgentsTargetDeprecationWarning to avoid masking future
    # DeprecationWarnings from apm_cli modules.
    from apm_cli.core.target_detection import AgentsTargetDeprecationWarning

    warnings.filterwarnings("ignore", category=AgentsTargetDeprecationWarning)

    # Check for updates only for known commands; skip on invalid input to fail fast.
    # Discovery must not initialize caches or contact the update service.
    raw_args = ctx.meta.get("apm_raw_args", sys.argv[1:])
    discovering = ctx.invoked_subcommand == "discover" or (
        ctx.invoked_subcommand == "init" and "--discover" in raw_args
    )
    if (
        not ctx.resilient_parsing
        and not discovering
        and ctx.invoked_subcommand is not None
        and ctx.command.get_command(ctx, ctx.invoked_subcommand) is not None
    ):
        _check_and_notify_updates()


def _legacy_cli_colors() -> tuple[str, str, str]:
    """Return legacy fallback colors without importing command helpers."""
    from colorama import Fore, Style
    from colorama import init as colorama_init

    colorama_init(autoreset=True)
    return Fore.RED + Style.BRIGHT, Fore.YELLOW, Style.RESET_ALL


def _get_current_code_page() -> "Optional[int]":
    """Get current Windows console code page using WinAPI.

    Returns the code page number (e.g., 65001 for UTF-8, 950 for CP950).
    Returns None if detection fails or on non-Windows platforms.
    """
    if sys.platform != "win32":
        return None

    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        return kernel32.GetConsoleOutputCP()
    except Exception:
        return None


def _code_page_to_encoding_name(cp: int) -> str:
    """Map code page number to readable encoding name.

    Args:
        cp: Code page number (e.g., 950, 65001).

    Returns:
        Human-readable encoding name or fallback name.
    """
    cp_map = {
        65001: "UTF-8",
        950: "cp950 (Traditional Chinese)",
        936: "cp936 (Simplified Chinese)",
        932: "cp932 (Japanese)",
        949: "cp949 (Korean)",
        1252: "cp1252 (Western European)",
        1251: "cp1251 (Cyrillic)",
    }
    return cp_map.get(cp, f"cp{cp}")


def _try_switch_to_utf8() -> bool:
    """Try to switch console to UTF-8 (code page 65001).

    This function:
    1. Checks if console is already UTF-8.
    2. If not, attempts to switch using SetConsoleCP/SetConsoleOutputCP.
    3. Verifies success by re-checking the code page.

    Returns:
        True if already UTF-8 or successfully switched, False otherwise.
    """
    if sys.platform != "win32":
        return True

    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

        # Check current console code page
        current_cp = kernel32.GetConsoleOutputCP()
        if current_cp == 65001:
            return True  # Already UTF-8

        # Attempt to switch to UTF-8
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)

        # Verify success
        new_cp = kernel32.GetConsoleOutputCP()
        return new_cp == 65001
    except Exception:
        return False


def _warn_encoding_issue(failed_cp: int) -> None:
    """Warn user if console UTF-8 switch failed.

    Args:
        failed_cp: The code page that failed to switch from.
    """
    encoding_name = _code_page_to_encoding_name(failed_cp)
    _, warning, reset = _legacy_cli_colors()
    click.echo(
        f"\n{warning}Warning: Console is {encoding_name}, UTF-8 switch failed.{reset}\n",
        err=True,
    )
    click.echo(
        f"{warning}Display issues may occur. Suggestions:{reset}",
        err=True,
    )
    click.echo("  - Run: chcp 65001  (if available)", err=True)
    click.echo("  - Or use: Windows Terminal or VS Code terminal\n", err=True)


def _configure_encoding() -> None:
    """Configure stdout/stderr for full Unicode on Windows.

    The default Windows console encoding (cp1252 or cp950) cannot represent many
    Unicode characters used in APM output (box-drawing, check marks, arrows, etc.).

    This function:
    1. Attempts to switch console to UTF-8 (code page 65001) via WinAPI.
    2. Sets ``PYTHONIOENCODING`` for child processes.
    3. Reconfigures Python text-mode streams to UTF-8.
    4. Only warns if UTF-8 switch fails.

    On non-Windows platforms this is a no-op.
    """
    if sys.platform != "win32":
        return

    # 1. Try to switch console to UTF-8
    utf8_success = _try_switch_to_utf8()

    # 2. Help child processes / pipes default to UTF-8
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    # 3. Reconfigure Python streams to UTF-8
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                try:  # noqa: SIM105
                    stream.reconfigure(encoding="utf-8", errors="backslashreplace")
                except Exception:
                    pass

    # 4. Warn only if UTF-8 switch failed
    if not utf8_success:
        current_cp = _get_current_code_page()
        if current_cp and current_cp != 65001:
            _warn_encoding_issue(current_cp)


def main() -> None:
    """Main entry point for the CLI."""
    _configure_logging()  # honours APM_LOG_LEVEL env var; --verbose upgrades in cli()
    _configure_encoding()
    try:
        cli(obj={})
    except Exception as e:
        error, _, reset = _legacy_cli_colors()
        click.echo(f"{error}Error: {e}{reset}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
