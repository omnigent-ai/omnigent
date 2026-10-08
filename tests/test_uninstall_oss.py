from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from omnigent.install_ledger import sha256_text

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "uninstall_oss.sh"


def _run_uninstall(
    home: Path, *args: str, path: str | None = None, env_updates: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["OMNIGENT_DATA_DIR"] = str(home / ".omnigent")
    env["OMNIGENT_CONFIG_HOME"] = str(home / ".omnigent")
    env["PATH"] = path or env.get("PATH", "")
    if env_updates:
        env.update(env_updates)
    return subprocess.run(
        ["sh", str(SCRIPT), *args],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )


def _fake_uv(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    uv_log = tmp_path / "uv.log"
    uv = fake_bin / "uv"
    uv.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" > {uv_log}\nexit 0\n")
    uv.chmod(0o755)
    return fake_bin, uv_log


def _path_without_zstd(tmp_path: Path) -> str:
    fake_bin = tmp_path / "no-zstd-bin"
    fake_bin.mkdir()
    for command in (
        "awk",
        "basename",
        "cat",
        "date",
        "dirname",
        "du",
        "find",
        "grep",
        "gzip",
        "mktemp",
        "mkdir",
        "ps",
        "rm",
        "sed",
        "sh",
        "sleep",
        "tar",
        "tr",
        "uname",
    ):
        target = shutil.which(command)
        if target is not None:
            (fake_bin / command).symlink_to(target)
    return str(fake_bin)


def test_uninstall_script_removes_profile_block_and_runs_wheel_last(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    profile = home / ".zshrc"
    profile.write_text(
        "keep\n"
        "# >>> Omnigent installer >>>\n"
        'export PATH="/fake/bin:$PATH"\n'
        "# <<< Omnigent installer <<<\n"
        "keep2\n"
    )
    fake_bin, uv_log = _fake_uv(tmp_path)

    result = _run_uninstall(
        home, "--yes", "--json", path=f"{fake_bin}:{os.environ.get('PATH', '')}"
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["summary"]["done"] >= 2
    assert "Omnigent installer" not in profile.read_text()
    assert profile.read_text() == "keep\nkeep2\n"
    assert uv_log.read_text().strip() == "tool uninstall omnigent"
    assert list(home.glob(".zshrc.omnigent.bak.*"))


def test_uninstall_script_bare_command_is_dry_run(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    profile = home / ".zshrc"
    profile.write_text(
        "keep\n"
        "# >>> Omnigent installer >>>\n"
        'export PATH="/fake/bin:$PATH"\n'
        "# <<< Omnigent installer <<<\n"
        "keep2\n"
    )
    fake_bin, uv_log = _fake_uv(tmp_path)

    result = _run_uninstall(home, path=f"{fake_bin}:{os.environ.get('PATH', '')}")

    assert result.returncode == 0, result.stderr
    assert "reported: profile_block" in result.stdout
    assert "reported: wheel" in result.stdout
    assert "Preview only" in result.stdout
    assert profile.read_text().startswith("keep\n# >>> Omnigent installer >>>")
    assert not uv_log.exists()


def test_uninstall_script_purge_backs_up_state_and_keeps_workspace_without_gate(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    state = home / ".omnigent"
    workspace = home / "omnigent"
    state.mkdir(parents=True)
    workspace.mkdir(parents=True)
    (state / "config.yaml").write_text("x: y\n")
    (state / "installation_id").write_text("install-123\n")
    (workspace / "project.txt").write_text("work\n")

    result = _run_uninstall(home, "state", "--purge", "--yes", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert not state.exists()
    assert workspace.exists()
    assert payload["backups"]
    assert all(Path(backup).parent != state for backup in payload["backups"])
    assert any(action.get("gate") == "--purge-workspace" for action in payload["actions"])


def test_uninstall_script_purge_without_target_also_removes_cli(tmp_path: Path) -> None:
    home = tmp_path / "home"
    state = home / ".omnigent"
    state.mkdir(parents=True)
    (state / "installation_id").write_text("install-123\n")
    profile = home / ".zshrc"
    profile.write_text(
        "keep\n"
        "# >>> Omnigent installer >>>\n"
        'export PATH="/fake/bin:$PATH"\n'
        "# <<< Omnigent installer <<<\n"
        "keep2\n"
    )
    fake_bin, uv_log = _fake_uv(tmp_path)

    result = _run_uninstall(
        home, "--purge", "--yes", "--json", path=f"{fake_bin}:{os.environ.get('PATH', '')}"
    )

    assert result.returncode == 0, result.stderr
    assert not state.exists()
    assert "Omnigent installer" not in profile.read_text()
    assert uv_log.read_text().strip() == "tool uninstall omnigent"


def test_uninstall_script_purge_uses_unique_backup_paths_for_multiple_trees(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    state = home / ".omnigent"
    workspace = home / "omnigent"
    linux_desktop_dirs = (
        home / ".config" / "Omnigent",
        home / ".cache" / "Omnigent",
        home / ".local" / "state" / "Omnigent",
    )
    mac_desktop_dirs = (
        home / "Library" / "Application Support" / "Omnigent",
        home / "Library" / "Caches" / "Omnigent",
        home / "Library" / "Logs" / "Omnigent",
    )
    for directory in (state, workspace, *linux_desktop_dirs, *mac_desktop_dirs):
        directory.mkdir(parents=True)
        (directory / "data.txt").write_text("data\n")
    (state / "installation_id").write_text("install-123\n")

    result = _run_uninstall(
        home,
        "all",
        "--purge",
        "--purge-workspace",
        "--yes",
        "--json",
        path=_path_without_zstd(tmp_path),
        env_updates={
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_STATE_HOME": str(home / ".local" / "state"),
        },
    )

    assert result.returncode == 0, result.stderr
    backups = json.loads(result.stdout)["backups"]
    assert len(backups) == 5
    assert len(backups) == len(set(backups))
    assert all(Path(backup).exists() for backup in backups)


def test_uninstall_script_refuses_tampered_profile_and_skips_wheel(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    original_block = (
        "# >>> Omnigent installer >>>\n"
        'export PATH="/fake/bin:$PATH"\n'
        "# <<< Omnigent installer <<<\n"
    )
    profile = home / ".zshrc"
    profile.write_text(original_block.replace("/fake/bin", "/tampered/bin"))
    manifest = tmp_path / "manifest.tsv"
    manifest.write_text(
        "\t".join(
            ["profile_block", str(profile), sha256_text(original_block), "recorded", "certain"]
        )
        + "\n"
    )
    fake_bin, uv_log = _fake_uv(tmp_path)
    env = os.environ.copy()
    env["OMNIGENT_UNINSTALL_LEDGER_MANIFEST"] = str(manifest)
    env["OMNIGENT_UNINSTALL_LEDGER_SOURCE"] = "backfill"
    env["HOME"] = str(home)
    env["OMNIGENT_DATA_DIR"] = str(home / ".omnigent")
    env["PATH"] = f"{fake_bin}:{os.environ.get('PATH', '')}"

    result = subprocess.run(
        ["sh", str(SCRIPT), "--yes", "--json"],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )

    assert result.returncode == 3
    payload = json.loads(result.stdout)
    assert any(action["gate"] == "--force" for action in payload["actions"])
    assert "tampered" in profile.read_text()
    assert not uv_log.exists()


def test_uninstall_script_external_config_requires_gate_then_removes_json_key(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "harness.json"
    config.write_text('{"mcp_servers": {"omnigent": {"url": "x"}, "other": {}}}\n')
    manifest = tmp_path / "manifest.tsv"
    manifest.write_text(
        "\t".join(
            [
                "external_config",
                str(config),
                "mcp_servers.omnigent",
                "json",
                "",
                "observed",
                "certain",
            ]
        )
        + "\n"
    )
    env = os.environ.copy()
    env["OMNIGENT_UNINSTALL_LEDGER_MANIFEST"] = str(manifest)
    env["OMNIGENT_UNINSTALL_LEDGER_SOURCE"] = "backfill"
    env["HOME"] = str(home)
    env["OMNIGENT_DATA_DIR"] = str(home / ".omnigent")

    skipped = subprocess.run(
        ["sh", str(SCRIPT), "--yes", "--json"],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )
    assert skipped.returncode == 0
    assert "omnigent" in config.read_text()
    assert any(
        action["gate"] == "--modify-external-config"
        for action in json.loads(skipped.stdout)["actions"]
    )

    removed = subprocess.run(
        ["sh", str(SCRIPT), "--yes", "--json", "--modify-external-config"],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )

    assert removed.returncode == 0, removed.stderr
    payload = json.loads(config.read_text())
    assert "omnigent" not in payload["mcp_servers"]
    assert "other" in payload["mcp_servers"]


def test_uninstall_script_toml_config_and_launch_agent_reporting(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        '[mcp_servers.omnigent]\ncommand = "omnigent"\n\n[mcp_servers.other]\ncommand = "other"\n'
    )
    launch_agent = tmp_path / "ai.omnigent.plist"
    launch_agent.write_text("plist\n")
    manifest = tmp_path / "manifest.tsv"
    manifest.write_text(
        "\t".join(
            [
                "external_config",
                str(config),
                "mcp_servers.omnigent",
                "toml",
                "",
                "observed",
                "certain",
            ]
        )
        + "\n"
        + "\t".join(
            ["launch_agent", "launchd", str(launch_agent), "ai.omnigent", "observed", "high"]
        )
        + "\n"
    )
    env = os.environ.copy()
    env["OMNIGENT_UNINSTALL_LEDGER_MANIFEST"] = str(manifest)
    env["HOME"] = str(home)
    env["OMNIGENT_DATA_DIR"] = str(home / ".omnigent")

    result = subprocess.run(
        ["sh", str(SCRIPT), "--dry-run", "--json", "--modify-external-config"],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    actions = json.loads(result.stdout)["actions"]
    assert any(action["artifact"] == "launch_agent" for action in actions)
    assert any("would remove" in action["detail"] for action in actions)

    removed = subprocess.run(
        ["sh", str(SCRIPT), "--yes", "--json", "--modify-external-config"],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )

    assert removed.returncode == 0, removed.stderr
    assert "mcp_servers.omnigent" not in config.read_text()
    assert "mcp_servers.other" in config.read_text()


def test_uninstall_script_unloads_launch_agent_before_stopping_host_pid(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    state = home / ".omnigent"
    state.mkdir(parents=True)
    launch_agent = tmp_path / "ai.omnigent.plist"
    launch_agent.write_text("plist\n")
    manifest = tmp_path / "manifest.tsv"
    manifest.write_text(
        "\t".join(
            ["launch_agent", "launchd", str(launch_agent), "ai.omnigent", "observed", "high"]
        )
        + "\n"
    )
    proc = subprocess.Popen(["sleep", "60"])
    (state / "host.pid").write_text(f"{proc.pid}\nlocal\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "launchctl").write_text("#!/bin/sh\nexit 0\n")
    (fake_bin / "launchctl").chmod(0o755)

    try:
        result = _run_uninstall(
            home,
            "--yes",
            "--json",
            path=f"{fake_bin}:{os.environ.get('PATH', '')}",
            env_updates={
                "OMNIGENT_UNINSTALL_LEDGER_MANIFEST": str(manifest),
                "OMNIGENT_UNINSTALL_LEDGER_SOURCE": "installer",
            },
        )

        assert result.returncode == 0, result.stderr
        proc.wait(timeout=5)
        assert not launch_agent.exists()
        actions = json.loads(result.stdout)["actions"]
        launch_index = next(
            index for index, action in enumerate(actions) if action["artifact"] == "launch_agent"
        )
        process_index = next(
            index for index, action in enumerate(actions) if action["artifact"] == "process"
        )
        assert launch_index < process_index
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_uninstall_script_external_json_preserves_key_order(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "harness.json"
    config.write_text('{"z": 1, "mcp_servers": {"other": {}, "omnigent": {}}, "a": 2}\n')
    manifest = tmp_path / "manifest.tsv"
    manifest.write_text(
        "\t".join(
            [
                "external_config",
                str(config),
                "mcp_servers.omnigent",
                "json",
                "",
                "observed",
                "certain",
            ]
        )
        + "\n"
    )

    result = _run_uninstall(
        home,
        "--yes",
        "--json",
        "--modify-external-config",
        env_updates={
            "OMNIGENT_UNINSTALL_LEDGER_MANIFEST": str(manifest),
            "OMNIGENT_UNINSTALL_LEDGER_SOURCE": "backfill",
        },
    )

    assert result.returncode == 0, result.stderr
    text = config.read_text()
    assert text.index('"z"') < text.index('"mcp_servers"') < text.index('"a"')
    assert "omnigent" not in text


def test_uninstall_script_toml_removes_nested_subtables(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        '[mcp_servers.omnigent]\ncommand = "omnigent"\n\n'
        '[mcp_servers.omnigent.env]\nFOO = "bar"\n\n'
        '[mcp_servers.other]\ncommand = "other"\n'
    )
    manifest = tmp_path / "manifest.tsv"
    manifest.write_text(
        "\t".join(
            [
                "external_config",
                str(config),
                "mcp_servers.omnigent",
                "toml",
                "",
                "observed",
                "certain",
            ]
        )
        + "\n"
    )

    result = _run_uninstall(
        home,
        "--yes",
        "--json",
        "--modify-external-config",
        env_updates={
            "OMNIGENT_UNINSTALL_LEDGER_MANIFEST": str(manifest),
            "OMNIGENT_UNINSTALL_LEDGER_SOURCE": "backfill",
        },
    )

    assert result.returncode == 0, result.stderr
    text = config.read_text()
    assert "mcp_servers.omnigent" not in text
    assert "mcp_servers.other" in text


def test_uninstall_script_refuses_without_install_signal(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()

    result = _run_uninstall(home, "--dry-run", "--json", path="/bin:/usr/bin")

    assert result.returncode == 3
    payload = json.loads(result.stdout)
    assert payload["exit_code"] == 3
    assert any(action["artifact"] == "anchor" for action in payload["actions"])


def test_uninstall_script_removes_fish_profile_blocks(tmp_path: Path) -> None:
    home = tmp_path / "home"
    fish_conf = home / ".config" / "fish" / "config.fish"
    fish_confd = home / ".config" / "fish" / "conf.d" / "omnigent.fish"
    fish_conf.parent.mkdir(parents=True)
    fish_confd.parent.mkdir(parents=True)
    block = (
        "# >>> Omnigent installer >>>\n"
        "set -gx PATH /fake/bin $PATH\n"
        "# <<< Omnigent installer <<<\n"
    )
    fish_conf.write_text(f"keep\n{block}keep2\n")
    fish_confd.write_text(f"before\n{block}after\n")

    result = _run_uninstall(home, "--yes", "--json", path="/bin:/usr/bin")

    assert result.returncode == 0, result.stderr
    assert fish_conf.read_text() == "keep\nkeep2\n"
    assert fish_confd.read_text() == "before\nafter\n"


def test_uninstall_script_purge_no_backup_removes_state_without_archive(tmp_path: Path) -> None:
    home = tmp_path / "home"
    state = home / ".omnigent"
    state.mkdir(parents=True)
    (state / "installation_id").write_text("install-123\n")

    result = _run_uninstall(
        home, "state", "--purge", "--no-backup", "--yes", "--json", path="/bin:/usr/bin"
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert not state.exists()
    assert payload["backups"] == []


def test_uninstall_script_purge_uses_gzip_when_zstd_missing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    state = home / ".omnigent"
    state.mkdir(parents=True)
    (state / "installation_id").write_text("install-123\n")
    (state / "config.yaml").write_text("x: y\n")

    result = _run_uninstall(
        home, "state", "--purge", "--yes", "--json", path=_path_without_zstd(tmp_path)
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["backups"]
    assert payload["backups"][0].endswith(".tar.gz")


def test_uninstall_script_purge_namespaces_xdg_state_backups(tmp_path: Path) -> None:
    home = tmp_path / "home"
    state_home = tmp_path / "xdg-state"
    state = home / ".omnigent"
    state.mkdir(parents=True)
    (state / "installation_id").write_text("install-123\n")
    (state / "config.yaml").write_text("x: y\n")

    result = _run_uninstall(
        home,
        "state",
        "--purge",
        "--yes",
        "--json",
        path="/bin:/usr/bin",
        env_updates={"XDG_STATE_HOME": str(state_home)},
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["backups"]
    assert Path(payload["backups"][0]).parent == state_home / "omnigent-backups"


def test_uninstall_script_keeps_state_when_zstd_backup_tar_fails(tmp_path: Path) -> None:
    home = tmp_path / "home"
    state = home / ".omnigent"
    state.mkdir(parents=True)
    (state / "installation_id").write_text("install-123\n")
    (state / "config.yaml").write_text("x: y\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "zstd").write_text("#!/bin/sh\nexit 0\n")
    (fake_bin / "tar").write_text("#!/bin/sh\nexit 1\n")
    (fake_bin / "zstd").chmod(0o755)
    (fake_bin / "tar").chmod(0o755)

    manifest = tmp_path / "manifest.tsv"
    manifest.write_text("keychain_secret\tanthropic\n")
    helper, log = _fake_secret_helper(tmp_path)

    result = _run_uninstall(
        home,
        "state",
        "--purge",
        "--yes",
        "--json",
        path=f"{fake_bin}:{os.environ.get('PATH', '')}",
        env_updates={
            "OMNIGENT_UNINSTALL_LEDGER_MANIFEST": str(manifest),
            "OMNIGENT_UNINSTALL_PYTHON": str(helper),
        },
    )

    assert result.returncode == 1
    assert state.exists()
    payload = json.loads(result.stdout)
    assert payload["backups"] == []
    # Keychain deletes are irreversible, so they wait for a successful backup.
    assert not log.exists()
    assert _keychain_actions(payload) == []
    assert [a["detail"] for a in payload["actions"] if a["artifact"] == "state"] == [
        "backup failed; state and keychain secrets kept"
    ]


def test_uninstall_script_stops_live_pid_from_state_run_dir(tmp_path: Path) -> None:
    home = tmp_path / "home"
    state = home / ".omnigent"
    run_dir = state / "run"
    run_dir.mkdir(parents=True)
    (state / "installation_id").write_text("install-123\n")
    proc = subprocess.Popen(["sleep", "60"])
    try:
        (run_dir / "daemon.pid").write_text(f"{proc.pid}\n")
        result = _run_uninstall(home, "state", "--dry-run", "--json")
        assert result.returncode == 0
        assert proc.poll() is None

        result = _run_uninstall(home, "state", "--yes", "--json")
        assert result.returncode == 0, result.stderr
        proc.wait(timeout=5)
        assert any(
            action["artifact"] == "process" and action["status"] == "done"
            for action in json.loads(result.stdout)["actions"]
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_uninstall_script_rerun_is_idempotent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    state = home / ".omnigent"
    state.mkdir(parents=True)
    (state / "installation_id").write_text("install-123\n")
    profile = home / ".zshrc"
    profile.write_text(
        "keep\n"
        "# >>> Omnigent installer >>>\n"
        'export PATH="/fake/bin:$PATH"\n'
        "# <<< Omnigent installer <<<\n"
    )
    fake_bin, _ = _fake_uv(tmp_path)
    path = f"{fake_bin}:{os.environ.get('PATH', '')}"

    first = _run_uninstall(home, "--yes", "--json", path=path)
    second = _run_uninstall(home, "--yes", "--json", path=path)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert "Omnigent installer" not in profile.read_text()


def _state_with_install_signal(home: Path) -> Path:
    state = home / ".omnigent"
    state.mkdir(parents=True)
    (state / "installation_id").write_text("install-123\n")
    return state


def _fake_secret_helper(
    tmp_path: Path, *, exit_code: int = 0, stdout: str = "removed", stderr: str = ""
) -> tuple[Path, Path]:
    """A stand-in for OMNIGENT_UNINSTALL_PYTHON that logs its arguments."""
    log = tmp_path / "helper.log"
    helper = tmp_path / "fake-python"
    # Swallow stdin like an interactive unlock prompt would; the script must not
    # hand the helper its own secrets list.
    lines = ["#!/bin/sh", "cat >/dev/null", f"printf '%s\\n' \"$*\" >>'{log}'"]
    if stdout:
        lines.append(f"printf '%s\\n' '{stdout}'")
    if stderr:
        lines.append(f"printf '%s\\n' '{stderr}' >&2")
    lines.append(f"exit {exit_code}")
    helper.write_text("\n".join(lines) + "\n")
    helper.chmod(0o755)
    return helper, log


def _keychain_actions(payload: dict) -> list[tuple[str, str, str]]:
    return [
        (action["path"], action["status"], action["detail"])
        for action in payload["actions"]
        if action["artifact"] == "keychain_secret"
    ]


def _state_actions(payload: dict) -> list[tuple[str, str | None]]:
    return [
        (action["status"], action["gate"])
        for action in payload["actions"]
        if action["artifact"] == "state"
    ]


def _purge_with_manifest(
    tmp_path: Path,
    manifest_rows: str,
    helper: Path | None,
    *flags: str,
    with_workspace: bool = False,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    home = tmp_path / "home"
    state = _state_with_install_signal(home)
    if with_workspace:
        (home / "omnigent").mkdir()
        (home / "omnigent" / "project.txt").write_text("work\n")
    manifest = tmp_path / "manifest.tsv"
    manifest.write_text(manifest_rows)
    result = _run_uninstall(
        home,
        "state",
        "--purge",
        "--json",
        *flags,
        env_updates={
            "OMNIGENT_UNINSTALL_LEDGER_MANIFEST": str(manifest),
            "OMNIGENT_UNINSTALL_PYTHON": str(helper) if helper else "",
        },
    )
    return result, state


def test_uninstall_script_purge_deletes_keychain_secrets_from_manifest(tmp_path: Path) -> None:
    helper, log = _fake_secret_helper(tmp_path)
    result, state = _purge_with_manifest(
        tmp_path, "keychain_secret\tanthropic\nkeychain_secret\topenrouter\n", helper, "--yes"
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert [(name, status) for name, status, _ in _keychain_actions(payload)] == [
        ("anthropic", "done"),
        ("openrouter", "done"),
    ]
    assert log.read_text().splitlines() == [
        "-m omnigent _internal delete-keychain-secret -- anthropic",
        "-m omnigent _internal delete-keychain-secret -- openrouter",
    ]
    assert not state.exists()
    kinds = [action["artifact"] for action in payload["actions"]]
    assert kinds.index("backup") < kinds.index("keychain_secret") < kinds.index("state")


@pytest.mark.parametrize(
    ("row", "display", "helper_arg"),
    [
        ("keychain_secret\ttab%09here\ttab here\n", "tab here", "tab%09here"),
        ("keychain_secret\tanthropic\tan\x01thropic\n", "anthropic", "anthropic"),
    ],
    ids=["encoded-name", "control-character"],
)
def test_uninstall_script_purge_transports_names_and_keeps_json_valid(
    tmp_path: Path, row: str, display: str, helper_arg: str
) -> None:
    helper, log = _fake_secret_helper(tmp_path)
    result, _ = _purge_with_manifest(tmp_path, row, helper, "--yes")

    assert result.returncode == 0, result.stderr
    actions = _keychain_actions(json.loads(result.stdout))
    assert [(name, status) for name, status, _ in actions] == [(display, "done")]
    assert log.read_text().splitlines() == [
        f"-m omnigent _internal delete-keychain-secret -- {helper_arg}"
    ]


def test_uninstall_script_purge_reports_keychain_secrets_without_helper(tmp_path: Path) -> None:
    result, state = _purge_with_manifest(tmp_path, "keychain_secret\tanthropic\n", None, "--yes")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    actions = _keychain_actions(payload)
    assert [(name, status) for name, status, _ in actions] == [("anthropic", "reported")]
    assert "left in the OS keychain" in actions[0][2]
    assert _state_actions(payload) == [("skipped", "--force")]
    assert payload["exit_code"] == 1
    assert state.exists()


def test_uninstall_script_purge_failed_keychain_delete_keeps_state(tmp_path: Path) -> None:
    helper, _ = _fake_secret_helper(
        tmp_path,
        exit_code=1,
        stderr=(
            "Error: could not delete secret anthropic: OS keyring inaccessible (KeyringLocked)\n"
            "Run omnigent stop to clear stale hosts, then retry."
        ),
    )
    result, state = _purge_with_manifest(
        tmp_path,
        "keychain_secret\tanthropic\n",
        helper,
        "--yes",
        "--purge-workspace",
        with_workspace=True,
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    actions = _keychain_actions(payload)
    assert [(name, status) for name, status, _ in actions] == [("anthropic", "failed")]
    # The helper's own error wins over the generic hint that follows it.
    assert actions[0][2] == (
        "failed to remove from the OS keychain (service omnigent): "
        "could not delete secret anthropic: OS keyring inaccessible (KeyringLocked)"
    )
    assert _state_actions(payload) == [("skipped", "--force")]
    assert state.exists()
    # The workspace is unrelated to the keychain retry, so it is still handled.
    assert not (state.parent / "omnigent").exists()


@pytest.mark.parametrize(
    ("helper_kwargs", "status"),
    [
        ({"exit_code": 1, "stderr": "Error: keyring exploded"}, "failed"),
        ({"stdout": "file-only KeyringLocked"}, "reported"),
    ],
    ids=["failed", "unresolved"],
)
def test_uninstall_script_purge_force_removes_state_but_keeps_nonzero_exit(
    tmp_path: Path, helper_kwargs: dict, status: str
) -> None:
    helper, _ = _fake_secret_helper(tmp_path, **helper_kwargs)
    result, state = _purge_with_manifest(
        tmp_path, "keychain_secret\tanthropic\n", helper, "--yes", "--force"
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert [(name, s) for name, s, _ in _keychain_actions(payload)] == [("anthropic", status)]
    assert payload["exit_code"] == 1
    assert not state.exists()


def test_uninstall_script_purge_treats_missing_helper_result_as_failure(tmp_path: Path) -> None:
    helper, _ = _fake_secret_helper(tmp_path, stdout="")
    result, state = _purge_with_manifest(tmp_path, "keychain_secret\tanthropic\n", helper, "--yes")

    assert result.returncode == 1
    actions = _keychain_actions(json.loads(result.stdout))
    assert [(name, status) for name, status, _ in actions] == [("anthropic", "failed")]
    assert "no result" in actions[0][2]
    assert state.exists()


def test_uninstall_script_purge_skips_absent_keychain_secret(tmp_path: Path) -> None:
    helper, _ = _fake_secret_helper(tmp_path, stdout="absent")
    result, state = _purge_with_manifest(tmp_path, "keychain_secret\tanthropic\n", helper, "--yes")

    assert result.returncode == 0, result.stderr
    actions = _keychain_actions(json.loads(result.stdout))
    assert [(name, status) for name, status, _ in actions] == [("anthropic", "skipped")]
    assert "no entry in the OS keychain" in actions[0][2]
    assert not state.exists()


@pytest.mark.parametrize(
    ("stdout", "fragment"),
    [
        ("file-only KeyringLocked", "was not reachable (KeyringLocked)"),
        (
            "partial PermissionError",
            "removed from the OS keychain (service omnigent); "
            "the file-backed store could not be read or updated (PermissionError)",
        ),
        (
            "unverified JSONDecodeError",
            "no entry in the OS keychain (service omnigent); "
            "the file-backed store could not be read or updated (JSONDecodeError)",
        ),
    ],
)
def test_uninstall_script_purge_keeps_state_for_unresolved_helper_outcomes(
    tmp_path: Path, stdout: str, fragment: str
) -> None:
    helper, _ = _fake_secret_helper(tmp_path, stdout=stdout)
    result, state = _purge_with_manifest(tmp_path, "keychain_secret\tanthropic\n", helper, "--yes")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    actions = _keychain_actions(payload)
    assert [(name, status) for name, status, _ in actions] == [("anthropic", "reported")]
    assert fragment in actions[0][2]
    assert _state_actions(payload) == [("skipped", "--force")]
    assert state.exists()


@pytest.mark.parametrize(
    ("with_helper", "fragment"),
    [(True, "would remove from the OS keychain"), (False, "would be left in the OS keychain")],
    ids=["helper", "no-helper"],
)
def test_uninstall_script_purge_dry_run_previews_keychain_secrets(
    tmp_path: Path, with_helper: bool, fragment: str
) -> None:
    helper, log = _fake_secret_helper(tmp_path)
    result, state = _purge_with_manifest(
        tmp_path, "keychain_secret\tanthropic\n", helper if with_helper else None, "--dry-run"
    )

    assert result.returncode == 0, result.stderr
    actions = _keychain_actions(json.loads(result.stdout))
    assert [(name, status) for name, status, _ in actions] == [("anthropic", "reported")]
    assert fragment in actions[0][2]
    assert not log.exists()
    assert state.exists()


def test_uninstall_script_purge_reports_discovery_error_and_keeps_state(tmp_path: Path) -> None:
    helper, log = _fake_secret_helper(tmp_path)
    result, state = _purge_with_manifest(
        tmp_path,
        "keychain_discovery_error\t[Errno 13] Permission denied: config.yaml\n",
        helper,
        "--yes",
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    actions = _keychain_actions(payload)
    assert [(name, status) for name, status, _ in actions] == [("config.yaml", "failed")]
    assert "Permission denied" in actions[0][2]
    assert not log.exists()
    assert state.exists()


def test_uninstall_script_standalone_purge_reports_config_keychain_refs(tmp_path: Path) -> None:
    home = tmp_path / "home"
    state = _state_with_install_signal(home)
    (state / "config.yaml").write_text(
        "providers:\n"
        "  anthropic:\n"
        "    anthropic:\n"
        "      api_key_ref: keychain:anthropic\n"
        "  eu:\n"
        "    openai:\n"
        '      api_key_ref: "keychain:gateway+eu"\n'
        "  hashed:\n"
        "    openai:\n"
        '      api_key_ref: "keychain:with#hash"\n'
        "cursor:\n"
        "  api_key_ref: keychain:cursor\n"
        "note: disabled-keychain:prod\n"
        "# api_key_ref: keychain:old\n"
    )

    result = _run_uninstall(
        home,
        "state",
        "--purge",
        "--yes",
        "--json",
        env_updates={
            "OMNIGENT_UNINSTALL_LEDGER_MANIFEST": "",
            "OMNIGENT_UNINSTALL_PYTHON": "",
        },
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    actions = _keychain_actions(payload)
    assert [(name, status) for name, status, _ in actions] == [
        ("anthropic", "reported"),
        ("cursor", "reported"),
        ("gateway+eu", "reported"),
        ("with#hash", "reported"),
    ]
    assert _state_actions(payload) == [("skipped", "--force")]
    assert state.exists()


def test_uninstall_script_standalone_purge_passes_raw_names_to_helper(tmp_path: Path) -> None:
    home = tmp_path / "home"
    state = _state_with_install_signal(home)
    (state / "config.yaml").write_text(
        "odd:\n  api_key_ref: keychain:pct%name\nctl:\n  api_key_ref: keychain:ctl\x01name\n"
    )
    helper, log = _fake_secret_helper(tmp_path)

    result = _run_uninstall(
        home,
        "state",
        "--purge",
        "--yes",
        "--json",
        env_updates={
            "OMNIGENT_UNINSTALL_LEDGER_MANIFEST": "",
            "OMNIGENT_UNINSTALL_PYTHON": str(helper),
        },
    )

    assert result.returncode == 0, result.stderr
    actions = _keychain_actions(json.loads(result.stdout))
    # The ledger drops the control character; the helper receives the exact name.
    assert [(name, status) for name, status, _ in actions] == [
        ("ctlname", "done"),
        ("pct%name", "done"),
    ]
    assert log.read_text().splitlines() == [
        "-m omnigent _internal delete-keychain-secret -- ctl\x01name",
        "-m omnigent _internal delete-keychain-secret -- pct%25name",
    ]
    assert not state.exists()


def _standalone_purge(home: Path, **env: str) -> subprocess.CompletedProcess[str]:
    return _run_uninstall(
        home,
        "state",
        "--purge",
        "--yes",
        "--json",
        env_updates={
            "OMNIGENT_UNINSTALL_LEDGER_MANIFEST": "",
            "OMNIGENT_UNINSTALL_PYTHON": "",
            **env,
        },
    )


def test_uninstall_script_standalone_purge_reports_unreadable_config(tmp_path: Path) -> None:
    home = tmp_path / "home"
    state = _state_with_install_signal(home)
    # A directory in config.yaml's place fails every read, regardless of the user.
    (state / "config.yaml").mkdir()

    result = _standalone_purge(home)

    assert result.returncode == 1
    actions = _keychain_actions(json.loads(result.stdout))
    assert [(name, status) for name, status, _ in actions] == [("config.yaml", "failed")]
    assert state.exists()


def test_uninstall_script_standalone_purge_reads_the_effective_config_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    state = _state_with_install_signal(home)
    (state / "config.yaml").write_text("cursor:\n  api_key_ref: keychain:statehome\n")
    config_home = tmp_path / "config-home"
    config_home.mkdir()
    (config_home / "config.yaml").write_text("cursor:\n  api_key_ref: keychain:confighome\n")

    result = _standalone_purge(home, OMNIGENT_CONFIG_HOME=str(config_home))

    assert result.returncode == 1
    actions = _keychain_actions(json.loads(result.stdout))
    assert [(name, status) for name, status, _ in actions] == [("confighome", "reported")]


def test_uninstall_script_standalone_purge_flags_names_it_cannot_parse(tmp_path: Path) -> None:
    home = tmp_path / "home"
    state = _state_with_install_signal(home)
    (state / "config.yaml").write_text(
        "providers:\n"
        "  anthropic:\n"
        "    anthropic:\n"
        "      api_key_ref: keychain:anthropic\n"
        "odd:\n"
        '  api_key_ref: "keychain:has space"\n'
    )

    result = _standalone_purge(home)

    assert result.returncode == 1
    actions = _keychain_actions(json.loads(result.stdout))
    assert [(name, status) for name, status, _ in actions] == [
        ("config.yaml", "failed"),
        ("anthropic", "reported"),
    ]
    assert "could not parse" in actions[0][2]
    assert state.exists()


def test_uninstall_script_purge_reports_each_secret_separately(tmp_path: Path) -> None:
    log = tmp_path / "helper.log"
    helper = tmp_path / "fake-python"
    helper.write_text(
        "#!/bin/sh\ncat >/dev/null\n"
        f"printf '%s\\n' \"$*\" >>'{log}'\n"
        'case "$*" in *anthropic) echo removed ;; *) echo "file-only KeyringLocked" ;; esac\n'
    )
    helper.chmod(0o755)
    result, state = _purge_with_manifest(
        tmp_path, "keychain_secret\tanthropic\nkeychain_secret\topenrouter\n", helper, "--yes"
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert [(name, status) for name, status, _ in _keychain_actions(payload)] == [
        ("anthropic", "done"),
        ("openrouter", "reported"),
    ]
    assert _state_actions(payload) == [("skipped", "--force")]
    assert state.exists()
