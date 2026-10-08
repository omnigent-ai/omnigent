from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import keyring
import keyring.errors
import pytest
from click.testing import CliRunner

from omnigent import cli as cli_module
from omnigent.install_ledger import InstallLedger, new_ledger
from omnigent.onboarding.secrets import SecretDeletion


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


def _uninstall_manifest_rows(
    monkeypatch, tmp_path: Path, config: str | bytes | None, *args: str
) -> tuple[list[list[str]], object]:
    """Run ``uninstall`` with a stub script; return the manifest rows and helper path.

    ``config`` seeds ``config.yaml``; ``None`` makes it an unreadable directory.
    """
    runner = CliRunner()
    script = tmp_path / "uninstall_oss.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    config_home = tmp_path / "config-home"
    config_home.mkdir()
    config_file = config_home / "config.yaml"
    if config is None:
        config_file.mkdir()
    elif isinstance(config, bytes):
        config_file.write_bytes(config)
    else:
        config_file.write_text(config)
    ledger = new_ledger(source="installer", strategy="install", deep=False)
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))
    monkeypatch.setattr(cli_module, "_uninstall_script_path", lambda: script)
    monkeypatch.setattr("omnigent.install_ledger.resolve_uninstall_ledger", lambda: ledger)
    seen: dict[str, object] = {}

    def _run(run_args, *, env, check):
        del run_args, check
        manifest = Path(env["OMNIGENT_UNINSTALL_LEDGER_MANIFEST"]).read_text()
        seen["rows"] = [line.split("\t") for line in manifest.splitlines()]
        seen["python"] = env.get("OMNIGENT_UNINSTALL_PYTHON")
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(cli_module.subprocess, "run", _run)

    result = runner.invoke(
        cli_module.cli, list(args or ("uninstall", "state", "--purge", "--yes"))
    )

    assert result.exit_code == 0, result.output
    rows = seen["rows"]
    assert isinstance(rows, list)
    return rows, seen["python"]


def _keychain_rows(rows: list[list[str]]) -> list[list[str]]:
    return [row for row in rows if row[0].startswith("keychain_")]


def test_uninstall_cli_manifest_carries_keychain_secrets_and_python_helper(
    monkeypatch, tmp_path: Path
) -> None:
    rows, python = _uninstall_manifest_rows(
        monkeypatch,
        tmp_path,
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
        "odd:\n"
        '  api_key_ref: "keychain:tab\\there"\n'
        "odder:\n"
        '  api_key_ref: "keychain:esc\\u001bhere"\n',
    )

    assert python == sys.executable
    # A delimiter inside a name must neither split its row nor leak into another;
    # the display column keeps the report readable and free of control characters.
    assert _keychain_rows(rows) == [
        ["keychain_secret", "anthropic", "anthropic"],
        ["keychain_secret", "cursor", "cursor"],
        ["keychain_secret", "esc%1Bhere", "esc here"],
        ["keychain_secret", "tab%09here", "tab here"],
    ]


def test_uninstall_cli_manifest_survives_recursive_yaml_aliases(
    monkeypatch, tmp_path: Path
) -> None:
    rows, _ = _uninstall_manifest_rows(
        monkeypatch, tmp_path, "loop: &loop\n  - *loop\n  - keychain:anthropic\n"
    )

    assert _keychain_rows(rows) == [["keychain_secret", "anthropic", "anthropic"]]


@pytest.mark.parametrize(
    "config",
    [
        None,
        b"\xff\xfe not utf-8",
        "[" * 2000 + "]" * 2000,
        "providers:\n"
        "  anthropic: {\n"
        "    api_key_ref: keychain:anthropic\n"
        '    other: "keychain:has space"\n',
    ],
    ids=["unreadable", "non-utf8", "too-deep", "malformed-yaml"],
)
def test_uninstall_cli_manifest_reports_undiscoverable_config_for_purge(
    monkeypatch, tmp_path: Path, config: str | bytes | None
) -> None:
    rows, _ = _uninstall_manifest_rows(monkeypatch, tmp_path, config)

    # Never guess names from a config that cannot be read exactly.
    keychain_rows = _keychain_rows(rows)
    assert [row[0] for row in keychain_rows] == ["keychain_discovery_error"]
    assert len(keychain_rows[0]) == 2 and keychain_rows[0][1]


def test_uninstall_cli_manifest_discovers_from_config_home_not_data_dir(
    monkeypatch, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data-dir"
    data_dir.mkdir()
    (data_dir / "config.yaml").write_text("cursor:\n  api_key_ref: keychain:statehome\n")
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(data_dir))

    rows, _ = _uninstall_manifest_rows(
        monkeypatch, tmp_path, "cursor:\n  api_key_ref: keychain:confighome\n"
    )

    assert _keychain_rows(rows) == [["keychain_secret", "confighome", "confighome"]]


def test_uninstall_cli_skips_keychain_discovery_without_purge(monkeypatch, tmp_path: Path) -> None:
    rows, _ = _uninstall_manifest_rows(
        monkeypatch, tmp_path, b"\xff\xfe not utf-8", "uninstall", "cli", "--dry-run"
    )

    assert _keychain_rows(rows) == []


def test_internal_delete_keychain_secret_decodes_name_and_reports_result(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("OMNIGENT_DISABLE_KEYRING", raising=False)
    store = {("omnigent", "tab\there"): "test-key-value"}

    def delete_password(service: str, username: str) -> None:
        if store.pop((service, username), None) is None:
            raise keyring.errors.PasswordDeleteError("No such password!")

    monkeypatch.setattr(keyring, "delete_password", delete_password)
    monkeypatch.setattr(
        keyring, "get_password", lambda service, username: store.get((service, username))
    )
    runner = CliRunner()

    removed = runner.invoke(cli_module.cli, ["_internal", "delete-keychain-secret", "tab%09here"])
    absent = runner.invoke(cli_module.cli, ["_internal", "delete-keychain-secret", "tab%09here"])

    assert removed.exit_code == 0, removed.output
    assert removed.output.strip() == "removed"
    assert store == {}
    assert absent.exit_code == 0, absent.output
    assert absent.output.strip() == "absent"


def test_internal_delete_keychain_secret_accepts_dash_prefixed_names(monkeypatch) -> None:
    seen: list[str] = []

    def fake_delete(name: str) -> SecretDeletion:
        seen.append(name)
        return SecretDeletion(removed=True)

    monkeypatch.setattr("omnigent.onboarding.secrets.delete_secret", fake_delete)
    runner = CliRunner()

    result = runner.invoke(cli_module.cli, ["_internal", "delete-keychain-secret", "--", "-dash"])

    assert result.exit_code == 0, result.output
    assert seen == ["-dash"]


def test_internal_delete_keychain_secret_treats_disabled_keyring_as_unreachable(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    from omnigent.onboarding import secrets

    runner = CliRunner()
    missing = runner.invoke(cli_module.cli, ["_internal", "delete-keychain-secret", "anthropic"])
    secrets.store_secret("anthropic", "test-key-value")
    file_only = runner.invoke(cli_module.cli, ["_internal", "delete-keychain-secret", "anthropic"])

    # Disabling the keychain must not make a keychain-only secret look absent.
    assert missing.exit_code == 1, missing.output
    assert "OMNIGENT_DISABLE_KEYRING" in missing.output
    assert file_only.exit_code == 0, file_only.output
    assert file_only.output.strip() == "file-only OMNIGENT_DISABLE_KEYRING"


@pytest.mark.parametrize(
    ("outcome", "exit_code", "expected"),
    [
        (
            SecretDeletion(removed=True, keyring_error="KeyringLocked"),
            0,
            "file-only KeyringLocked",
        ),
        (
            SecretDeletion(removed=False, keyring_error="NoKeyringError"),
            1,
            "OS keyring inaccessible (NoKeyringError) and no file-backed secret",
        ),
        (
            SecretDeletion(
                removed=False, keyring_error="KeyringLocked", file_error="JSONDecodeError"
            ),
            1,
            "could not be read or updated (JSONDecodeError)",
        ),
        (SecretDeletion(removed=False, survives=True), 1, "still holds secret 'anthropic'"),
        (
            SecretDeletion(removed=True, keyring_error="KeyringLocked", survives=True),
            1,
            "refused to delete secret 'anthropic' and could not be read back (KeyringLocked)",
        ),
        (
            SecretDeletion(removed=False, file_error="JSONDecodeError"),
            0,
            "unverified JSONDecodeError",
        ),
        (SecretDeletion(removed=True, file_error="PermissionError"), 0, "partial PermissionError"),
    ],
)
def test_internal_delete_keychain_secret_reports_keyring_outcomes(
    monkeypatch, outcome: SecretDeletion, exit_code: int, expected: str
) -> None:
    monkeypatch.setattr("omnigent.onboarding.secrets.delete_secret", lambda name: outcome)
    runner = CliRunner()

    result = runner.invoke(cli_module.cli, ["_internal", "delete-keychain-secret", "anthropic"])

    assert result.exit_code == exit_code, result.output
    assert expected in result.output


def test_internal_delete_keychain_secret_surfaces_unexpected_errors(monkeypatch) -> None:
    def _boom(name: str) -> SecretDeletion:
        raise RuntimeError("backend exploded")

    monkeypatch.setattr("omnigent.onboarding.secrets.delete_secret", _boom)
    runner = CliRunner()

    result = runner.invoke(cli_module.cli, ["_internal", "delete-keychain-secret", "anthropic"])

    assert result.exit_code == 1
    assert isinstance(result.exception, RuntimeError)
