"""Tests for per-user host service installation."""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from omnigent.host import service


def _capture_runs(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        returncode = 1 if args[:2] == ["launchctl", "print"] else 0
        return subprocess.CompletedProcess(args, returncode, "", "")

    monkeypatch.setattr(service.subprocess, "run", _run)
    monkeypatch.setattr(service, "_record_service", lambda installed: None)
    monkeypatch.setattr(service, "_forget_service", lambda installed: None)
    return calls


def test_enable_launchd_user_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(service.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(service.os, "getuid", lambda: 501)
    monkeypatch.setattr(service.sys, "executable", "/opt/omnigent/bin/python")
    calls = _capture_runs(monkeypatch)

    installed = service.enable_user_host_service(
        "https://example.com",
        environment={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )

    payload = plistlib.loads(installed.path.read_bytes())
    assert installed.path == tmp_path / "Library/LaunchAgents/ai.omnigent.host.plist"
    assert payload["Label"] == "ai.omnigent.host"
    assert payload["ProgramArguments"] == [
        "/opt/omnigent/bin/python",
        "-m",
        "omnigent.host.service_entry",
        "--server",
        "https://example.com",
    ]
    assert payload["EnvironmentVariables"]["PATH"] == "/usr/bin:/bin"
    assert payload["KeepAlive"] == {"SuccessfulExit": False}
    assert "ProcessType" not in payload
    assert calls == [
        ["launchctl", "bootout", "gui/501/ai.omnigent.host"],
        [
            "launchctl",
            "bootstrap",
            "gui/501",
            str(installed.path),
        ],
    ]
    assert installed.path.stat().st_mode & 0o777 == 0o600


def test_disable_launchd_user_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(service.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(service.os, "getuid", lambda: 502)
    path = tmp_path / "Library/LaunchAgents/ai.omnigent.host.plist"
    path.parent.mkdir(parents=True)
    path.write_text("old")
    calls = _capture_runs(monkeypatch)

    removed = service.disable_user_host_service()

    assert removed.path == path
    assert not path.exists()
    assert calls == [
        ["launchctl", "bootout", "gui/502/ai.omnigent.host"],
        ["launchctl", "print", "gui/502/ai.omnigent.host"],
    ]


def test_disable_launchd_tolerates_stale_print_during_async_unload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale ``launchctl print`` right after ``bootout`` must not leak the plist.

    ``bootout`` unloads asynchronously, so ``print`` can still report the job
    for a moment; disable must retry instead of aborting before the unlink.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(service.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(service.os, "getuid", lambda: 502)
    monkeypatch.setattr(service, "_LAUNCHD_UNLOAD_POLL_INTERVAL", 0.0)
    path = tmp_path / "Library/LaunchAgents/ai.omnigent.host.plist"
    path.parent.mkdir(parents=True)
    path.write_text("old")
    calls: list[list[str]] = []
    print_answers = iter([0, 113])  # stale window, then the job is gone

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        returncode = next(print_answers) if args[:2] == ["launchctl", "print"] else 0
        return subprocess.CompletedProcess(args, returncode, "", "")

    monkeypatch.setattr(service.subprocess, "run", _run)
    monkeypatch.setattr(service, "_forget_service", lambda installed: None)

    removed = service.disable_user_host_service()

    assert removed.path == path
    assert not path.exists()
    assert calls == [
        ["launchctl", "bootout", "gui/502/ai.omnigent.host"],
        ["launchctl", "print", "gui/502/ai.omnigent.host"],
        ["launchctl", "print", "gui/502/ai.omnigent.host"],
    ]


def test_disable_launchd_removes_plist_even_when_job_never_unloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuinely stuck job still must not retain the RunAtLoad plist.

    The error is surfaced, but only after the definition is removed — a
    surviving plist would silently restore the service at the next login.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(service.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(service.os, "getuid", lambda: 502)
    monkeypatch.setattr(service, "_LAUNCHD_UNLOAD_TIMEOUT", 0.0)
    monkeypatch.setattr(service, "_LAUNCHD_UNLOAD_POLL_INTERVAL", 0.0)
    path = tmp_path / "Library/LaunchAgents/ai.omnigent.host.plist"
    path.parent.mkdir(parents=True)
    path.write_text("old")
    forgotten: list[service.HostService] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(service.subprocess, "run", _run)
    monkeypatch.setattr(service, "_forget_service", forgotten.append)

    with pytest.raises(service.HostServiceError, match="still running"):
        service.disable_user_host_service()

    assert not path.exists()
    assert forgotten, "the uninstall ledger entry must still be cleared"


def test_enable_systemd_user_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(service.platform, "system", lambda: "Linux")
    monkeypatch.setattr(service.sys, "executable", "/opt/omnigent/bin/python")
    calls = _capture_runs(monkeypatch)

    installed = service.enable_user_host_service(
        None,
        environment={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )

    unit = installed.path.read_text()
    assert installed.path == tmp_path / "xdg/systemd/user/omnigent-host.service"
    assert 'Environment="HOME=' in unit
    assert (
        'ExecStart="/opt/omnigent/bin/python" "-m" "omnigent.host.service_entry" "--local"'
    ) in unit
    assert "Restart=on-failure" in unit
    assert "RestartPreventExitStatus=78 143" in unit
    assert calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "omnigent-host.service"],
    ]


def test_systemd_unit_escapes_specifiers_and_literal_dollars() -> None:
    unit = service._systemd_unit(
        command=["/opt/$tools/python", "--server", "https://example.com/%h/$target"],
        environment={"CONFIG": "$HOME/%h"},
    ).decode()

    assert 'Environment="CONFIG=$HOME/%%h"' in unit
    assert (
        'ExecStart="/opt/$$tools/python" "--server" "https://example.com/%%h/$$target"'
    ) in unit


def test_disable_systemd_user_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setattr(service.platform, "system", lambda: "Linux")
    path = tmp_path / ".config/systemd/user/omnigent-host.service"
    path.parent.mkdir(parents=True)
    path.write_text("old")
    calls = _capture_runs(monkeypatch)

    removed = service.disable_user_host_service()

    assert removed.path == path
    assert not path.exists()
    assert calls == [
        ["systemctl", "--user", "disable", "--now", "omnigent-host.service"],
        ["systemctl", "--user", "daemon-reload"],
    ]


def test_host_service_rejects_unsupported_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service.platform, "system", lambda: "Windows")

    with pytest.raises(service.HostServiceError, match="macOS and Linux"):
        service.enable_user_host_service(None, environment={})


def test_service_entry_maps_fatal_host_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.cli import cli
    from omnigent.host import HOST_FATAL_EXIT_CODE, service_entry

    monkeypatch.setattr(sys, "argv", ["service-entry", "--local"])

    def _fatal(**kwargs: object) -> None:
        raise SystemExit(HOST_FATAL_EXIT_CODE)

    monkeypatch.setattr(cli, "main", _fatal)

    assert service_entry.main() == 0
