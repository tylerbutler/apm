"""Version management for APM CLI."""

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import click

# Build-time constants (will be injected during build)
# This avoids TOML parsing overhead during runtime
__BUILD_VERSION__ = None
__BUILD_SHA__ = None
_console: Any | None = None


def get_version() -> str:
    """
    Get the current version efficiently.

    First tries build-time constant, then installed package metadata,
    then falls back to pyproject.toml parsing (for development).

    Returns:
        str: Version string
    """
    # Use build-time constant if available (fastest path - for PyInstaller binaries)
    if __BUILD_VERSION__:
        return __BUILD_VERSION__

    # Try to get version from installed package metadata (for pip installations)
    # Skip this in frozen/PyInstaller environments to avoid import issues
    if not getattr(sys, "frozen", False):
        try:
            # Python 3.8+ has importlib.metadata
            if sys.version_info >= (3, 8):  # noqa: UP036
                from importlib.metadata import PackageNotFoundError, version
            else:
                from importlib_metadata import PackageNotFoundError, version

            return version("apm-cli")
        except (ImportError, PackageNotFoundError):
            pass

    # Fallback to reading from pyproject.toml (for development/source installations)
    try:
        # Handle PyInstaller bundle vs development
        if getattr(sys, "frozen", False):
            # Running in PyInstaller bundle
            pyproject_path = Path(sys._MEIPASS) / "pyproject.toml"
        else:
            # Running in development
            pyproject_path = Path(__file__).parent.parent.parent / "pyproject.toml"

        if pyproject_path.exists():
            # Simple regex parsing instead of full TOML library
            with open(pyproject_path, encoding="utf-8") as f:
                content = f.read()

            # Look for version = "x.y.z" pattern (including PEP 440 prereleases)
            import re

            match = re.search(r'version\s*=\s*["\']([^"\']+)["\']', content)
            if match:
                version_str = match.group(1)
                # Validate PEP 440 version patterns: x.y.z or x.y.z{a|b|rc}N
                if re.match(r"^\d+\.\d+\.\d+(a\d+|b\d+|rc\d+)?$", version_str):
                    return version_str
    except Exception:
        pass

    return "unknown"


def get_build_sha() -> str:
    """Get the short git commit SHA for the current build.

    Uses the build-time constant when available (shipped binaries),
    otherwise falls back to querying git at runtime (development).
    """
    if __BUILD_SHA__:
        return __BUILD_SHA__

    # Fallback: query git at runtime (development only)
    if not getattr(sys, "frozen", False):
        import subprocess

        try:
            from apm_cli.utils.git_env import get_git_executable

            repo_root = Path(__file__).parent.parent.parent
            result = subprocess.run(
                [get_git_executable(), "rev-parse", "--short", "HEAD"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
    return ""


def _get_console() -> Any | None:
    """Return a lazily created Rich console for version output."""
    global _console
    if _console is not None:
        return _console
    try:
        from rich.console import Console

        _console = Console()
    except ImportError:
        return None
    return _console


def _fallback_title() -> tuple[str, str]:
    """Return legacy title and reset styles for non-Rich environments."""
    try:
        from colorama import Fore, Style
        from colorama import init as colorama_init

        colorama_init(autoreset=True)
        return Fore.CYAN + Style.BRIGHT, Style.RESET_ALL
    except ImportError:
        return "", ""


def _echo_dim(message: str) -> None:
    """Render one dim version-detail line with a plain Click fallback."""
    import click

    console = _get_console()
    if console is not None:
        console.print(message, style="dim")
    else:
        click.echo(message)


def print_version(
    ctx: "click.Context",
    param: "click.Parameter | None",
    value: bool,
) -> None:
    """Print the source or frozen version contract and exit."""
    if not value or ctx.resilient_parsing:
        return

    import click

    version_str = get_version()
    sha = get_build_sha()
    if sha:
        version_str += f" ({sha})"

    console = _get_console()
    if console is not None:
        try:
            console.print(
                f"[bold cyan]Agent Package Manager (APM) CLI[/bold cyan] version {version_str}"
            )
        except Exception:
            title, reset = _fallback_title()
            click.echo(f"{title}Agent Package Manager (APM) CLI{reset} version {version_str}")
    else:
        title, reset = _fallback_title()
        click.echo(f"{title}Agent Package Manager (APM) CLI{reset} version {version_str}")

    try:
        from apm_cli.core.experimental import is_enabled

        if is_enabled("verbose_version"):
            import platform

            python_ver = platform.python_version()
            plat = f"{sys.platform}-{platform.machine()}"
            install_path = str(Path(__file__).resolve().parent)

            _echo_dim(f"  {'Python:':<14}{python_ver}")
            _echo_dim(f"  {'Platform:':<14}{plat}")
            _echo_dim(f"  {'Install path:':<14}{install_path}")
    except Exception:
        # Experimental metadata must never break the baseline version contract.
        pass

    ctx.exit()


# For backward compatibility
__version__ = get_version()
