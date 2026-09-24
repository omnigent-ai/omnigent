from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from click.testing import CliRunner

from omnigent import cli as cli_module
from omnigent.install_ledger import InstallLedger, new_ledger


def test_uninstall_cli_resolves_ledger_and_forwards_flags(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    script = tmp_path / "uninstall_oss.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    ledger = new_ledger(source="installer", strategy="install", deep=False)
    calls: list[tuple[list[str], str | None]] = []

    monkeypatch.setattr(cli_module, "_uninstall_script_path", lambda: script)

    def _ledger() -> InstallLedger:
        return ledger

    monkeypatch.setattr("omnigent.install_ledger.resolve_uninstall_ledger", _ledger)

    def _run(args, *, env, check):
        calls.append((list(args), env.get("OMNIGENT_UNINSTALL_LEDGER_SOURCE")))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(cli_module.subprocess, "run", _run)

    result = runner.invoke(
        cli_module.cli,
        ["uninstall", "all", "--purge", "--yes", "--json", "--purge-workspace"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            [str(script), "all", "--purge", "--purge-workspace", "--yes", "--json"],
            "installer",
        )
    ]


def test_uninstall_cli_refuses_without_install_signal(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr("omnigent.install_ledger.resolve_uninstall_ledger", lambda: None)

    result = runner.invoke(cli_module.cli, ["uninstall", "--json"])

    assert result.exit_code == 3
    assert "no Omnigent install detected" in result.output


def test_uninstall_cli_defaults_to_dry_run_without_destructive_flags(
    monkeypatch, tmp_path: Path
) -> None:
    runner = CliRunner()
    script = tmp_path / "uninstall_oss.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    ledger = new_ledger(source="installer", strategy="install", deep=False)
    calls: list[list[str]] = []

    monkeypatch.setattr(cli_module, "_uninstall_script_path", lambda: script)
    monkeypatch.setattr("omnigent.install_ledger.resolve_uninstall_ledger", lambda: ledger)

    def _run(args, *, env, check):
        del env, check
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(cli_module.subprocess, "run", _run)

    result = runner.invoke(cli_module.cli, ["uninstall"])

    assert result.exit_code == 0, result.output
    assert calls == [[str(script), "--dry-run"]]


def test_uninstall_cli_human_refusal_exits_three(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr("omnigent.install_ledger.resolve_uninstall_ledger", lambda: None)

    result = runner.invoke(cli_module.cli, ["uninstall"])

    assert result.exit_code == 3
    assert "No Omnigent install detected" in result.output


def test_uninstall_cli_uses_exclusive_manifest_and_cleans_temp_script(
    monkeypatch, tmp_path: Path
) -> None:
    runner = CliRunner()
    temp_script_dir = Path(tempfile.gettempdir()) / "omnigent-uninstall-test-cleanup"
    temp_script_dir.mkdir(exist_ok=True)
    script = temp_script_dir / "uninstall_oss.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    ledger = new_ledger(source="installer", strategy="install", deep=False)
    manifest_paths: list[Path] = []

    monkeypatch.setattr(cli_module, "_uninstall_script_path", lambda: script)
    monkeypatch.setattr("omnigent.install_ledger.resolve_uninstall_ledger", lambda: ledger)

    def _run(args, *, env, check):
        del args, check
        manifest_paths.append(Path(env["OMNIGENT_UNINSTALL_LEDGER_MANIFEST"]))
        assert manifest_paths[-1].name.startswith("omnigent-uninstall-ledger-")
        assert manifest_paths[-1].name.endswith(".tsv")
        assert str(os.getpid()) not in manifest_paths[-1].name
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(cli_module.subprocess, "run", _run)

    result = runner.invoke(cli_module.cli, ["uninstall", "--yes"])

    assert result.exit_code == 0, result.output
    assert manifest_paths and not manifest_paths[0].exists()
    assert not temp_script_dir.exists()


def test_uninstall_cli_manifest_carries_keychain_secrets_and_python_helper(
    monkeypatch, tmp_path: Path
) -> None:
    runner = CliRunner()
    script = tmp_path / "uninstall_oss.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    config_home = tmp_path / "config-home"
    config_home.mkdir()
    (config_home / "config.yaml").write_text(
        "providers:\n"
        "  anthropic:\n"
        "    kind: key\n"
        "    anthropic:\n"
        "      api_key_ref: keychain:anthropic\n"
        "  openai:\n"
        "    kind: key\n"
        "    openai:\n"
        "      api_key_ref: env:OPENAI_API_KEY\n"
        "cursor:\n"
        "  api_key_ref: keychain:cursor\n"
    )
    ledger = new_ledger(source="installer", strategy="install", deep=False)
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))
    monkeypatch.setattr(cli_module, "_uninstall_script_path", lambda: script)
    monkeypatch.setattr("omnigent.install_ledger.resolve_uninstall_ledger", lambda: ledger)
    seen: dict[str, object] = {}

    def _run(args, *, env, check):
        del args, check
        manifest = Path(env["OMNIGENT_UNINSTALL_LEDGER_MANIFEST"]).read_text()
        seen["rows"] = [line.split("\t") for line in manifest.splitlines()]
        seen["python"] = env.get("OMNIGENT_UNINSTALL_PYTHON")
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(cli_module.subprocess, "run", _run)

    result = runner.invoke(cli_module.cli, ["uninstall", "state", "--purge", "--yes"])

    assert result.exit_code == 0, result.output
    assert seen["python"] == sys.executable
    rows = seen["rows"]
    assert isinstance(rows, list)
    assert [row for row in rows if row[0] == "keychain_secret"] == [
        ["keychain_secret", "anthropic"],
        ["keychain_secret", "cursor"],
    ]


def test_internal_delete_keychain_secret_deletes_stored_secret(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    from omnigent.onboarding import secrets

    secrets.store_secret("anthropic", "test-key-value")
    runner = CliRunner()

    result = runner.invoke(cli_module.cli, ["_internal", "delete-keychain-secret", "anthropic"])

    assert result.exit_code == 0, result.output
    assert secrets.load_secret("anthropic") is None


def test_internal_delete_keychain_secret_reports_failure(monkeypatch) -> None:
    def _boom(name: str) -> None:
        raise RuntimeError("keyring exploded")

    monkeypatch.setattr("omnigent.onboarding.secrets.delete_secret", _boom)
    runner = CliRunner()

    result = runner.invoke(cli_module.cli, ["_internal", "delete-keychain-secret", "anthropic"])

    assert result.exit_code == 1
    assert "could not delete secret" in result.output
