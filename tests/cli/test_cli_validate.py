"""The offline command's public output, exit codes and lightweight dispatch."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest
import tomllib
from click.testing import CliRunner

from omnigent.cli_validate import validate_command
from omnigent.entrypoint import isolate_offline_imports, main, offline_arguments
from omnigent.spec.offline import validate_path


def test_json_output_is_deterministic_and_matches_library(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("spec_version: 1\nexecutor: {type: agents_sdk}\n", encoding="utf-8")
    runner = CliRunner()
    args = [str(path), "--offline", "--json"]
    first = runner.invoke(validate_command, args)
    second = runner.invoke(validate_command, args)
    assert first.exit_code == second.exit_code == 0, first.output
    assert first.output == second.output
    assert first.stderr == ""
    assert json.loads(first.stdout) == validate_path(str(path)).to_dict()


@pytest.mark.parametrize(
    "args", [["--unknown=private-value", "--json"], ["one", "private-value", "--json"]]
)
def test_json_usage_errors_are_sanitized(args: list[str]) -> None:
    result = CliRunner().invoke(validate_command, args)
    assert result.exit_code == 2
    assert result.stderr == ""
    assert "private-value" not in result.output
    assert json.loads(result.stdout)["status"] == "invalid_invocation"


def test_invalid_content_exit_and_json(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("spec_version: 4", encoding="utf-8")
    result = CliRunner().invoke(validate_command, [str(path), "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["status"] == "invalid"


def test_default_path_offline_and_text_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.yaml").write_text(
        "spec_version: 1\nexecutor: {type: agents_sdk}", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(validate_command, [])
    assert result.exit_code == 0, result.output
    assert "runtime readiness is not verified" in result.output
    assert "SKIPPED CODE" in result.output


def test_wrapper_gate_still_applies(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMNIGENT_REQUIRE_WRAPPER", "1")
    monkeypatch.delenv("OMNIGENT_WRAPPER_BYPASS", raising=False)
    result = CliRunner().invoke(validate_command, [str(tmp_path), "--json"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["diagnostics"][0]["code"] == "WRAPPER_REQUIRED"


def test_help() -> None:
    result = CliRunner().invoke(validate_command, ["--help"])
    assert result.exit_code == 0
    assert "--offline" in result.output
    assert "--json" in result.output


@pytest.mark.parametrize(
    "argv", [["version"], ["run", "agent.yaml"], ["--help"], ["host"], ["--version", "validate"]]
)
def test_other_entrypoint_commands_are_delegated_unchanged(
    argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    called = []
    runtime = types.ModuleType("omnigent.cli")
    runtime.main = lambda: called.append(sys.argv.copy())
    monkeypatch.setitem(sys.modules, "omnigent.cli", runtime)
    monkeypatch.setattr(sys, "argv", ["omnigent", *argv])
    main()
    assert called == [["omnigent", *argv]]


def test_root_runtime_flags_do_not_trigger_startup() -> None:
    args = offline_arguments(["--profiling", "validate", "--json"])
    assert args == ["--profiling", "--json"]
    result = CliRunner().invoke(validate_command, args)
    assert result.exit_code == 2
    assert json.loads(result.stdout)["status"] == "invalid_invocation"


def test_console_aliases_use_the_lightweight_entrypoint() -> None:
    project = Path(__file__).resolve().parents[2] / "pyproject.toml"
    scripts = tomllib.loads(project.read_text(encoding="utf-8"))["project"]["scripts"]
    assert scripts["omnigent"] == scripts["omni"] == "omnigent.entrypoint:main"


def test_library_import_is_not_mistaken_for_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["consumer.py", "validate", "--json"])
    before = sys.path.copy()
    bytecode = sys.dont_write_bytecode
    isolate_offline_imports(package_init=True)
    assert sys.path == before
    assert sys.dont_write_bytecode == bytecode


def test_import_isolation_preserves_project_virtualenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["omnigent", "validate", "--json"])
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    site_packages = str(tmp_path / ".venv" / "lib" / "site-packages")
    monkeypatch.setattr(sys, "path", ["", str(tmp_path), site_packages])
    isolate_offline_imports(package_init=True)
    assert sys.path == [site_packages]
    assert sys.dont_write_bytecode
