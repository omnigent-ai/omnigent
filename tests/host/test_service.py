"""Tests for per-user host service installation."""

from __future__ import annotations

import plistlib
import shutil
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


def test_launchd_status_reports_target_pid_and_autostart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(service.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(service.os, "getuid", lambda: 501)
    installed = service._service_for_current_platform()
    installed.path.parent.mkdir(parents=True)
    installed.path.write_bytes(
        service._launchd_payload(
            installed,
            command=[
                "/opt/omnigent/bin/python",
                "-m",
                "omnigent.host.service_entry",
                "--server",
                "https://example.com",
            ],
            environment={},
        )
    )

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[1] == "print":
            return subprocess.CompletedProcess(args, 0, "state = running\n    pid = 4242\n", "")
        assert args[1] == "print-disabled"
        return subprocess.CompletedProcess(args, 0, '"ai.omnigent.host" => false\n', "")

    monkeypatch.setattr(service.subprocess, "run", _run)

    status = service.user_host_service_status()

    assert status.installed is True
    assert status.configured_target == "https://example.com"
    assert status.executable == "/opt/omnigent/bin/python"
    assert status.manager_state == "running"
    assert status.manager_pid == 4242
    assert status.enabled is True
    assert status.definition_error is None


def test_systemd_status_reports_failed_local_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setattr(service.platform, "system", lambda: "Linux")
    installed = service._service_for_current_platform()
    installed.path.parent.mkdir(parents=True)
    installed.path.write_bytes(
        service._systemd_unit(
            command=[
                "/opt/$tools/python",
                "-m",
                "omnigent.host.service_entry",
                "--local",
            ],
            environment={},
        )
    )
    show = "\n".join(
        [
            "LoadState=loaded",
            "ActiveState=failed",
            "SubState=failed",
            "MainPID=0",
            "UnitFileState=enabled",
            "Result=exit-code",
        ]
    )
    monkeypatch.setattr(
        service.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, show, ""),
    )

    status = service.user_host_service_status()

    assert status.configured_target == "local"
    assert status.executable == "/opt/$tools/python"
    assert status.manager_state == "failed"
    assert status.enabled is True
    assert status.manager_error == "result: exit-code"
    assert status.log == "journalctl --user -u omnigent-host.service"


def test_service_status_degrades_on_foreign_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(service.platform, "system", lambda: "Darwin")
    path = tmp_path / "Library/LaunchAgents/ai.omnigent.host.plist"
    path.parent.mkdir(parents=True)
    path.write_text("not a plist")

    status = service.user_host_service_status(probe_manager=False)

    assert status.installed is True
    assert status.configured_target is None
    assert status.definition_error is not None


def test_service_status_degrades_on_truncated_xml_plist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(service.platform, "system", lambda: "Darwin")
    path = tmp_path / "Library/LaunchAgents/ai.omnigent.host.plist"
    path.parent.mkdir(parents=True)
    path.write_bytes(
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        b'"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        b'<plist version="1.0"><dict><key>Label</key>'
    )

    status = service.user_host_service_status(probe_manager=False)

    assert status.installed is True
    assert status.configured_target is None
    assert status.definition_error is not None


def test_service_status_reports_unsupported_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service.platform, "system", lambda: "Windows")

    status = service.user_host_service_status()

    assert status.supported is False
    assert status.manager_state == "unavailable"
    assert "macOS and Linux" in (status.manager_error or "")


def test_start_systemd_service_preserves_definition_and_autostart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setattr(service.platform, "system", lambda: "Linux")
    path = tmp_path / ".config/systemd/user/omnigent-host.service"
    path.parent.mkdir(parents=True)
    path.write_text("definition")
    statuses = iter(
        [
            service.HostServiceStatus(
                supported=True,
                kind="systemd_user",
                path=path,
                label=path.name,
                installed=True,
                configured_target="local",
                manager_state="stopped",
                enabled=True,
            ),
            service.HostServiceStatus(
                supported=True,
                kind="systemd_user",
                path=path,
                label=path.name,
                installed=True,
                configured_target="local",
                manager_state="running",
                manager_pid=4242,
                enabled=True,
            ),
        ]
    )
    monkeypatch.setattr(service, "user_host_service_status", lambda: next(statuses))
    calls: list[list[str]] = []
    monkeypatch.setattr(service, "_run_checked", lambda args: calls.append(list(args)))

    status = service.start_user_host_service(None)

    assert status.manager_state == "running"
    assert path.exists()
    assert calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "start", "omnigent-host.service"],
    ]


def test_stop_launchd_service_preserves_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(service.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(service.os, "getuid", lambda: 503)
    path = tmp_path / "Library/LaunchAgents/ai.omnigent.host.plist"
    path.parent.mkdir(parents=True)
    path.write_text("definition")
    statuses = iter(
        [
            service.HostServiceStatus(
                supported=True,
                kind="launchd",
                path=path,
                label="ai.omnigent.host",
                installed=True,
                configured_target="https://example.com",
                manager_state="running",
                manager_pid=4242,
                enabled=True,
            ),
            service.HostServiceStatus(
                supported=True,
                kind="launchd",
                path=path,
                label="ai.omnigent.host",
                installed=True,
                configured_target="https://example.com",
                manager_state="stopped",
                enabled=True,
            ),
        ]
    )
    monkeypatch.setattr(service, "user_host_service_status", lambda: next(statuses))
    calls: list[list[str]] = []

    def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 1 if args[1] == "print" else 0, "", "")

    monkeypatch.setattr(service, "_run_best_effort", _run)

    status = service.stop_user_host_service("https://example.com")

    assert status.manager_state == "stopped"
    assert path.exists()
    assert calls == [
        ["launchctl", "bootout", "gui/503/ai.omnigent.host"],
        ["launchctl", "print", "gui/503/ai.omnigent.host"],
    ]


def test_enable_launchd_user_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(service.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(service.os, "getuid", lambda: 501)
    python = tmp_path / "bin/python"
    python.parent.mkdir()
    python.write_text("#!/bin/sh\n")
    python.chmod(0o700)
    monkeypatch.setattr(service.sys, "executable", str(python))
    calls = _capture_runs(monkeypatch)

    installed = service.enable_user_host_service(
        "https://example.com",
        environment={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )

    payload = plistlib.loads(installed.path.read_bytes())
    assert installed.path == tmp_path / "Library/LaunchAgents/ai.omnigent.host.plist"
    assert payload["Label"] == "ai.omnigent.host"
    assert payload["ProgramArguments"] == [
        str(python),
        "-m",
        "omnigent.host.service_entry",
        "--server",
        "https://example.com",
    ]
    assert payload["EnvironmentVariables"]["PATH"] == "/usr/bin:/bin"
    assert payload["KeepAlive"] == {"SuccessfulExit": False}
    assert "ProcessType" not in payload
    assert calls == [
        ["launchctl", "print", "gui/501/ai.omnigent.host"],
        ["launchctl", "print-disabled", "gui/501"],
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


def test_enable_systemd_user_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(service.platform, "system", lambda: "Linux")
    python = tmp_path / "bin/python"
    python.parent.mkdir()
    python.write_text("#!/bin/sh\n")
    python.chmod(0o700)
    monkeypatch.setattr(service.sys, "executable", str(python))
    calls = _capture_runs(monkeypatch)

    installed = service.enable_user_host_service(
        None,
        environment={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )

    unit = installed.path.read_text()
    assert installed.path == tmp_path / "xdg/systemd/user/omnigent-host.service"
    assert 'Environment="HOME=' in unit
    assert f'ExecStart="{python}" "-m" "omnigent.host.service_entry" "--local"' in unit
    assert "Restart=on-failure" in unit
    assert "RestartPreventExitStatus=78 143" in unit
    assert calls[0][:4] == ["systemctl", "--user", "show", "omnigent-host.service"]
    assert calls[1:] == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "omnigent-host.service"],
    ]


def test_enable_systemd_rolls_back_definition_and_callback_on_activation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setattr(service.platform, "system", lambda: "Linux")
    python = tmp_path / "bin/python"
    python.parent.mkdir()
    python.write_text("#!/bin/sh\n")
    python.chmod(0o700)
    monkeypatch.setattr(service.sys, "executable", str(python))
    path = tmp_path / ".config/systemd/user/omnigent-host.service"
    monkeypatch.setattr(
        service,
        "user_host_service_status",
        lambda: service.HostServiceStatus(
            supported=True,
            kind="systemd_user",
            path=path,
            label=path.name,
            installed=False,
            manager_state="stopped",
        ),
    )
    checked: list[list[str]] = []

    def _checked(args: list[str]) -> None:
        checked.append(list(args))
        if "enable" in args:
            raise service.HostServiceError("activation failed")

    rollback_calls: list[list[str]] = []
    monkeypatch.setattr(service, "_run_checked", _checked)
    monkeypatch.setattr(
        service,
        "_run_best_effort",
        lambda args: (
            rollback_calls.append(list(args)) or subprocess.CompletedProcess(args, 0, "", "")
        ),
    )
    monkeypatch.setattr(service, "_record_service", lambda installed: None)
    monkeypatch.setattr(service, "_forget_service", lambda installed: None)
    retired: list[str] = []
    restored: list[str] = []

    with pytest.raises(service.HostServiceError, match="activation failed"):
        service.enable_user_host_service(
            None,
            environment={"HOME": str(tmp_path)},
            before_activate=lambda: retired.append("retired"),
            on_rollback=lambda: restored.append("restored"),
        )

    assert retired == ["retired"]
    assert restored == ["restored"]
    assert not path.exists()
    assert checked == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "omnigent-host.service"],
    ]
    assert rollback_calls == [
        ["systemctl", "--user", "disable", "--now", "omnigent-host.service"],
        ["systemctl", "--user", "daemon-reload"],
    ]


def test_enable_systemd_restores_running_previous_service_on_ledger_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setattr(service.platform, "system", lambda: "Linux")
    old_python = tmp_path / "old/python"
    old_python.parent.mkdir()
    old_python.write_text("#!/bin/sh\n")
    old_python.chmod(0o700)
    new_python = tmp_path / "new/python"
    new_python.parent.mkdir()
    new_python.write_text("#!/bin/sh\n")
    new_python.chmod(0o700)
    monkeypatch.setattr(service.sys, "executable", str(new_python))
    path = tmp_path / ".config/systemd/user/omnigent-host.service"
    path.parent.mkdir(parents=True)
    previous = service._systemd_unit(
        command=[str(old_python), "-m", "omnigent.host.service_entry", "--local"],
        environment={"HOME": str(tmp_path)},
    )
    path.write_bytes(previous)
    monkeypatch.setattr(
        service,
        "user_host_service_status",
        lambda: service.HostServiceStatus(
            supported=True,
            kind="systemd_user",
            path=path,
            label=path.name,
            installed=True,
            configured_target="local",
            manager_state="running",
            manager_pid=4242,
            enabled=True,
        ),
    )
    checked: list[list[str]] = []
    rollback_calls: list[list[str]] = []
    monkeypatch.setattr(service, "_run_checked", lambda args: checked.append(list(args)))
    monkeypatch.setattr(
        service,
        "_run_best_effort",
        lambda args: (
            rollback_calls.append(list(args)) or subprocess.CompletedProcess(args, 0, "", "")
        ),
    )

    def _fail_record(installed: service.HostService) -> None:
        del installed
        raise service.HostServiceError("ledger failed")

    monkeypatch.setattr(service, "_record_service", _fail_record)

    with pytest.raises(service.HostServiceError, match="ledger failed"):
        service.enable_user_host_service(None, environment={"HOME": str(tmp_path)})

    assert path.read_bytes() == previous
    assert checked == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "omnigent-host.service"],
        ["systemctl", "--user", "restart", "omnigent-host.service"],
    ]
    assert rollback_calls == [
        ["systemctl", "--user", "stop", "omnigent-host.service"],
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "omnigent-host.service"],
        ["systemctl", "--user", "start", "omnigent-host.service"],
    ]


def test_status_reports_missing_configured_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setattr(service.platform, "system", lambda: "Linux")
    installed = service._service_for_current_platform()
    installed.path.parent.mkdir(parents=True)
    missing = tmp_path / "removed/python"
    installed.path.write_bytes(
        service._systemd_unit(
            command=[str(missing), "-m", "omnigent.host.service_entry", "--local"],
            environment={},
        )
    )

    status = service.user_host_service_status(probe_manager=False)

    assert status.executable_valid is False
    assert str(missing) in (status.executable_error or "")


def test_systemd_unit_escapes_specifiers_and_literal_dollars() -> None:
    unit = service._systemd_unit(
        command=["/opt/$tools/python", "--server", "https://example.com/%h/$target"],
        environment={"CONFIG": "$HOME/%h"},
    ).decode()

    assert 'Environment="CONFIG=$HOME/%%h"' in unit
    assert (
        'ExecStart="/opt/$$tools/python" "--server" "https://example.com/%%h/$$target"'
    ) in unit


def test_generated_systemd_unit_passes_native_verify(tmp_path: Path) -> None:
    systemd_analyze = shutil.which("systemd-analyze")
    if systemd_analyze is None:
        pytest.skip("systemd-analyze is not installed")
    path = tmp_path / "omnigent-host.service"
    path.write_bytes(
        service._systemd_unit(
            command=[sys.executable, "-m", "omnigent.host.service_entry", "--local"],
            environment={"HOME": str(tmp_path)},
        )
    )

    result = subprocess.run(
        [systemd_analyze, "verify", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_generated_launchd_plist_passes_native_lint(tmp_path: Path) -> None:
    plutil = shutil.which("plutil")
    if plutil is None:
        pytest.skip("plutil is not installed")
    installed = service.HostService(
        kind="launchd",
        path=tmp_path / "ai.omnigent.host.plist",
        label="ai.omnigent.host",
        log_path=tmp_path / "service.log",
    )
    installed.path.write_bytes(
        service._launchd_payload(
            installed,
            command=[sys.executable, "-m", "omnigent.host.service_entry", "--local"],
            environment={"HOME": str(tmp_path)},
        )
    )

    result = subprocess.run(
        [plutil, "-lint", str(installed.path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout


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
