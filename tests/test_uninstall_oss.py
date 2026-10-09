from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
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

    result = _run_uninstall(
        home,
        "state",
        "--purge",
        "--yes",
        "--json",
        path=f"{fake_bin}:{os.environ.get('PATH', '')}",
    )

    assert result.returncode == 1
    assert state.exists()
    payload = json.loads(result.stdout)
    assert payload["backups"] == []


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


def _start_private_tmux_server(socket_path: Path) -> None:
    subprocess.run(
        ["tmux", "-S", str(socket_path), "new-session", "-d", "-s", "main", "sleep 300"],
        check=True,
        capture_output=True,
    )


def _tmux_server_alive(socket_path: Path) -> bool:
    return (
        subprocess.run(
            ["tmux", "-S", str(socket_path), "list-sessions"], capture_output=True
        ).returncode
        == 0
    )


def _kill_tmux_server(socket_path: Path) -> None:
    subprocess.run(["tmux", "-S", str(socket_path), "kill-server"], capture_output=True)


def _purge_env(tmp_path: Path, scratch_tmp: Path) -> tuple[str, dict[str, str]]:
    """PATH with a no-op ``uv`` so the wheel step passes, plus the scratch TMPDIR."""
    fake_bin, _ = _fake_uv(tmp_path)
    return f"{fake_bin}:{os.environ.get('PATH', '')}", {"TMPDIR": str(scratch_tmp)}


def _tmux_actions(result: subprocess.CompletedProcess[str]) -> list[dict[str, object]]:
    return [
        action for action in json.loads(result.stdout)["actions"] if action["artifact"] == "tmux"
    ]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_until(predicate: Callable[[], bool], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not predicate():
        time.sleep(0.1)


def _start_private_tmux_server_with_sighup_child(socket_path: Path) -> int:
    """Start a private tmux server whose pane traps SIGHUP, returning its pane pid.

    Mirrors a native harness CLI (e.g. claude) that survives the SIGHUP
    ``tmux kill-server`` sends its panes, so only a process-group SIGKILL reaps
    it.
    """
    subprocess.run(
        [
            "tmux",
            "-S",
            str(socket_path),
            "new-session",
            "-d",
            "-s",
            "main",
            "trap '' HUP; sleep 300",
        ],
        check=True,
        capture_output=True,
    )
    panes = subprocess.run(
        ["tmux", "-S", str(socket_path), "list-panes", "-a", "-F", "#{pane_pid}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return int(panes.stdout.split()[0])


def _kill_pane_group_best_effort(pid: int) -> None:
    if pid <= 0:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.kill(pid, signal.SIGKILL)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux on PATH")
def test_uninstall_script_kills_managed_private_socket_terminal(tmp_path: Path) -> None:
    """Purging state also stops a session terminal left on a private tmux socket.

    Managed terminals run on ``$TMPDIR/omnigent-terminal-*/tmux.sock`` as
    session ``main``, which the default-socket ``omnigent:*`` sweep never sees.
    """
    home = tmp_path / "home"
    state = home / ".omnigent"
    state.mkdir(parents=True)
    (state / "installation_id").write_text("install-123\n")
    # Short scratch TMPDIR: tmp_path can overrun the unix socket path limit.
    scratch_tmp = Path(tempfile.mkdtemp(prefix="omnigent-un-"))
    terminal_dir = scratch_tmp / "omnigent-terminal-leaked"
    terminal_dir.mkdir()
    socket_path = terminal_dir / "tmux.sock"
    path, env_updates = _purge_env(tmp_path, scratch_tmp)
    try:
        _start_private_tmux_server(socket_path)
        assert _tmux_server_alive(socket_path)

        dry = _run_uninstall(
            home, "--purge", "--dry-run", "--json", path=path, env_updates=env_updates
        )
        assert dry.returncode == 0, dry.stderr
        assert _tmux_server_alive(socket_path)
        assert terminal_dir.is_dir()
        assert any(
            action["status"] == "reported" and action["path"] == str(terminal_dir)
            for action in _tmux_actions(dry)
        )

        result = _run_uninstall(
            home, "--purge", "--yes", "--json", path=path, env_updates=env_updates
        )
        assert result.returncode == 0, result.stderr
        assert not state.exists()
        assert not _tmux_server_alive(socket_path), (
            "private-socket tmux server survived `uninstall --purge`"
        )
        assert not terminal_dir.exists()
        assert any(
            action["status"] == "done" and "managed terminal" in str(action["detail"])
            for action in _tmux_actions(result)
        )
    finally:
        _kill_tmux_server(socket_path)
        shutil.rmtree(scratch_tmp, ignore_errors=True)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux on PATH")
def test_uninstall_script_keeps_managed_terminal_dir_when_kill_fails(tmp_path: Path) -> None:
    """A terminal whose teardown cannot be confirmed keeps its socket for retry.

    When kill-server fails and no tmux command can confirm the server stopped,
    removing the dir anyway would report success while the server and its harness
    child keep running with no way left to reach them.
    """
    home = tmp_path / "home"
    state = home / ".omnigent"
    state.mkdir(parents=True)
    (state / "installation_id").write_text("install-123\n")
    scratch_tmp = Path(tempfile.mkdtemp(prefix="omnigent-un-"))
    terminal_dir = scratch_tmp / "omnigent-terminal-stuck"
    terminal_dir.mkdir()
    socket_path = terminal_dir / "tmux.sock"
    path, env_updates = _purge_env(tmp_path, scratch_tmp)
    real_tmux = shutil.which("tmux")
    assert real_tmux is not None
    # A tmux client that fails kill-server and any liveness probe alike, as a
    # protocol mismatch does: no command proves the server stopped, so the sweep
    # must preserve the socket. _tmux_server_alive checks the real server itself.
    stubborn_tmux = tmp_path / "bin" / "tmux"
    stubborn_tmux.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do\n'
        '  case "$arg" in\n'
        "    kill-server | list-sessions | has-session)\n"
        '      echo "protocol version mismatch" >&2\n'
        "      exit 1 ;;\n"
        "  esac\n"
        "done\n"
        f'exec "{real_tmux}" "$@"\n'
    )
    stubborn_tmux.chmod(0o755)
    try:
        _start_private_tmux_server(socket_path)
        result = _run_uninstall(
            home, "--purge", "--yes", "--json", path=path, env_updates=env_updates
        )
        assert result.returncode == 1, result.stdout
        assert _tmux_server_alive(socket_path)
        assert socket_path.is_socket()
        assert any(
            action["status"] == "failed" and action["path"] == str(terminal_dir)
            for action in _tmux_actions(result)
        )
    finally:
        _kill_tmux_server(socket_path)
        shutil.rmtree(scratch_tmp, ignore_errors=True)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux on PATH")
def test_uninstall_script_skips_symlinked_managed_terminal_socket(tmp_path: Path) -> None:
    """A symlinked ``tmux.sock`` is never followed: it could point at another server."""
    home = tmp_path / "home"
    state = home / ".omnigent"
    state.mkdir(parents=True)
    (state / "installation_id").write_text("install-123\n")
    scratch_tmp = Path(tempfile.mkdtemp(prefix="omnigent-un-"))
    bystander_socket = scratch_tmp / "bystander" / "tmux.sock"
    bystander_socket.parent.mkdir()
    decoy_dir = scratch_tmp / "omnigent-terminal-decoy"
    decoy_dir.mkdir()
    (decoy_dir / "tmux.sock").symlink_to(bystander_socket)
    path, env_updates = _purge_env(tmp_path, scratch_tmp)
    try:
        _start_private_tmux_server(bystander_socket)
        result = _run_uninstall(
            home, "--purge", "--yes", "--json", path=path, env_updates=env_updates
        )
        assert result.returncode == 0, result.stdout
        assert _tmux_server_alive(bystander_socket)
        assert decoy_dir.is_dir()
        assert any(
            action["status"] == "skipped" and action["path"] == str(decoy_dir)
            for action in _tmux_actions(result)
        )
    finally:
        _kill_tmux_server(bystander_socket)
        shutil.rmtree(scratch_tmp, ignore_errors=True)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux on PATH")
def test_uninstall_script_kills_sighup_ignoring_harness_child(tmp_path: Path) -> None:
    """``uninstall --purge`` force-kills a harness child that outlived kill-server.

    tmux ``kill-server`` only SIGHUPs its panes, so a harness CLI that traps
    SIGHUP (like a native agent) survives it. The sweep must SIGKILL the pane's
    process group so no orphaned child is left behind.
    """
    home = tmp_path / "home"
    state = home / ".omnigent"
    state.mkdir(parents=True)
    (state / "installation_id").write_text("install-123\n")
    scratch_tmp = Path(tempfile.mkdtemp(prefix="omnigent-un-"))
    terminal_dir = scratch_tmp / "omnigent-terminal-harness"
    terminal_dir.mkdir()
    socket_path = terminal_dir / "tmux.sock"
    path, env_updates = _purge_env(tmp_path, scratch_tmp)
    pane_pid = -1
    try:
        pane_pid = _start_private_tmux_server_with_sighup_child(socket_path)
        assert _pid_alive(pane_pid)

        result = _run_uninstall(
            home, "--purge", "--yes", "--json", path=path, env_updates=env_updates
        )
        assert result.returncode == 0, result.stdout
        assert not _tmux_server_alive(socket_path)
        assert not terminal_dir.exists()
        _wait_until(lambda: not _pid_alive(pane_pid), timeout=10)
        assert not _pid_alive(pane_pid), "SIGHUP-ignoring harness child survived uninstall"
    finally:
        _kill_pane_group_best_effort(pane_pid)
        _kill_tmux_server(socket_path)
        shutil.rmtree(scratch_tmp, ignore_errors=True)


def test_shell_target_gone_markers_match_python() -> None:
    """The uninstall script's gone-server markers mirror terminal.py's.

    The shell copy of ``_tmux_reports_target_gone`` is hand-maintained; if it
    drifts from the Python tuple the sweep misclassifies a dead server as live
    (socket kept forever) or vice versa, so pin the two copies together.
    """
    from omnigent.inner.terminal import _TMUX_TARGET_GONE_STDERR_MARKERS

    body = SCRIPT.read_text().split("tmux_reports_target_gone() {", 1)[1].split("\n}", 1)[0]
    shell_prefixes = set(re.findall(r'"([^"]*)"\*', body))
    assert shell_prefixes == set(_TMUX_TARGET_GONE_STDERR_MARKERS) | {"error connecting to "}
    # The error-connecting case mirrors the Python endswith guard.
    assert "(no such file or directory)" in body
