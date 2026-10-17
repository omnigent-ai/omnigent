from __future__ import annotations

import os
import subprocess
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


def _stub_command(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


def test_uninstall_leaves_third_party_units_named_like_omnigent(
    monkeypatch, tmp_path: Path, capfd
) -> None:
    home = tmp_path / "home"
    launch_dir = home / "Library" / "LaunchAgents"
    systemd_dir = home / ".config" / "systemd" / "user"
    launch_dir.mkdir(parents=True)
    systemd_dir.mkdir(parents=True)
    third_party_plist = launch_dir / "com.example.omnigent-handoff.plist"
    third_party_plist.write_text("plist\n")
    third_party_unit = systemd_dir / "com.example.omnigent-handoff.service"
    third_party_unit.write_text("[Unit]\n")
    (home / ".zshrc").write_text(
        "# >>> Omnigent installer >>>\n"
        'export PATH="$HOME/.local/bin:$PATH"\n'
        "# <<< Omnigent installer <<<\n"
    )
    stubs = tmp_path / "bin"
    stubs.mkdir()
    launchctl_log = tmp_path / "launchctl.log"
    systemctl_log = tmp_path / "systemctl.log"
    _stub_command(stubs / "launchctl", f'printf \'%s\\n\' "$*" >> "{launchctl_log}"')
    _stub_command(stubs / "systemctl", f'printf \'%s\\n\' "$*" >> "{systemctl_log}"')
    _stub_command(stubs / "uv", "echo 'omnigent is not installed' >&2; exit 1")
    _stub_command(stubs / "tmux", "exit 1")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(home / ".omnigent"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("PATH", f"{stubs}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.chdir(home)

    result = CliRunner().invoke(cli_module.cli, ["uninstall", "--yes"])

    report = capfd.readouterr().out + result.output
    assert result.exit_code == 0, report
    assert "com.example.omnigent-handoff" not in report
    assert third_party_plist.exists()
    assert third_party_unit.exists()
    assert not launchctl_log.exists()
    assert not systemctl_log.exists()
