"""Import-boundary and lifecycle contracts for the lazy root CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from click.testing import CliRunner

import apm_cli.cli as cli_module
from apm_cli.commands.registry import (
    command_names,
    get_command_entry,
    public_command_names,
)

ROOT = Path(__file__).resolve().parents[2]


def _subprocess_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment["APM_E2E_TESTS"] = "1"
    environment["NO_COLOR"] = "1"
    return environment


def _run_cli_probe(*args: str, frozen: bool = False) -> dict[str, Any]:
    script = """
import json
import sys
from pathlib import Path
from click.testing import CliRunner

if sys.argv[1] == "frozen":
    sys.frozen = True
    sys._MEIPASS = str(Path.cwd())
args = sys.argv[2:]
from apm_cli.cli import cli
result = CliRunner().invoke(cli, args)
command_modules = sorted(
    name
    for name in sys.modules
    if name.startswith("apm_cli.commands.")
    and name != "apm_cli.commands.registry"
)
print(json.dumps({
    "exit_code": result.exit_code,
    "output": result.output,
    "command_modules": command_modules,
    "tls_module_loaded": "apm_cli.core.tls_trust" in sys.modules,
}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, "frozen" if frozen else "source", *args],
        cwd=ROOT,
        env=_subprocess_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_importing_cli_loads_only_static_command_registry() -> None:
    script = """
import json
import sys
import apm_cli.cli
print(json.dumps({
    "command_modules": sorted(
        name
        for name in sys.modules
        if name.startswith("apm_cli.commands.")
        and name != "apm_cli.commands.registry"
    ),
    "git_env_loaded": "apm_cli.utils.git_env" in sys.modules,
    "tls_module_loaded": "apm_cli.core.tls_trust" in sys.modules,
}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=_subprocess_environment(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "command_modules": [],
        "git_env_loaded": False,
        "tls_module_loaded": False,
    }


def test_version_exits_without_commands_or_tls() -> None:
    result = _run_cli_probe("--version")

    assert result["exit_code"] == 0
    assert "Agent Package Manager (APM) CLI" in result["output"]
    assert result["command_modules"] == []
    assert result["tls_module_loaded"] is False


def test_root_help_is_complete_without_commands_or_tls() -> None:
    result = _run_cli_probe("--help")

    assert result["exit_code"] == 0
    assert result["command_modules"] == []
    assert result["tls_module_loaded"] is False
    lines = result["output"].splitlines()
    commands_start = lines.index("Commands:") + 1
    commands_end = lines.index("", commands_start)
    rendered_names = {
        line.strip().split(maxsplit=1)[0]
        for line in lines[commands_start:commands_end]
        if line.strip()
    }
    assert rendered_names == set(public_command_names())


def test_invalid_command_preserves_click_error_without_commands_or_tls() -> None:
    result = _run_cli_probe("not-a-command")

    assert result["exit_code"] == 2
    assert "No such command 'not-a-command'." in result["output"]
    assert result["command_modules"] == []
    assert result["tls_module_loaded"] is False


def test_hidden_and_public_alias_targets_are_explicit() -> None:
    info = get_command_entry("info")
    search = get_command_entry("search")

    assert info is not None
    assert info.alias_for == "view"
    assert info.hidden is True
    assert search is not None
    assert search.alias_for == "marketplace.search"
    assert search.hidden is False

    info_result = CliRunner().invoke(cli_module.cli, ["info", "--help"])
    search_result = CliRunner().invoke(cli_module.cli, ["search", "--help"])

    assert info_result.exit_code == 0
    assert "Usage: cli info " in info_result.output
    assert search_result.exit_code == 0
    assert "Usage: cli search " in search_result.output


def test_selected_command_import_is_cached_once() -> None:
    script = """
import json
from click.testing import CliRunner
import apm_cli.cli as cli_module

target = "apm_cli.commands.list_cmd"
calls = []
original = cli_module.import_module
def traced(name):
    calls.append(name)
    return original(name)
cli_module.import_module = traced
first = CliRunner().invoke(cli_module.cli, ["list", "--help"])
second = CliRunner().invoke(cli_module.cli, ["list", "--help"])
print(json.dumps({
    "first": first.exit_code,
    "second": second.exit_code,
    "target_calls": calls.count(target),
}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=_subprocess_environment(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result == {"first": 0, "second": 0, "target_calls": 1}


def test_root_lifecycle_initializes_tls_only_for_selected_command(monkeypatch) -> None:
    from apm_cli.core import tls_trust

    calls: list[str] = []
    monkeypatch.setattr(
        tls_trust,
        "configure_process_tls_trust",
        lambda: calls.append("configure") or True,
    )
    monkeypatch.setattr(
        tls_trust,
        "log_tls_trust_status",
        lambda: calls.append("log"),
    )
    monkeypatch.setattr(cli_module, "_check_and_notify_updates", lambda: None)

    runner = CliRunner()
    assert runner.invoke(cli_module.cli, ["--version"]).exit_code == 0
    assert runner.invoke(cli_module.cli, ["--help"]).exit_code == 0
    assert calls == []

    assert runner.invoke(cli_module.cli, ["list", "--help"]).exit_code == 0
    assert calls == ["configure", "log"]


def test_lazy_command_loading_works_in_frozen_contract() -> None:
    result = _run_cli_probe("view", "--help", frozen=True)

    assert result["exit_code"] == 0
    assert "Usage: cli view " in result["output"]
    assert "apm_cli.commands.view" in result["command_modules"]


def test_registry_names_are_unique_and_stably_sorted() -> None:
    names = command_names()

    assert names == tuple(sorted(names))
    assert len(names) == len(set(names))
