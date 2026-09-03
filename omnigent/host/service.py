"""Install the Omnigent host as a per-user operating-system service."""

from __future__ import annotations

import os
import platform
import plistlib
import re
import shlex
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias
from xml.parsers.expat import ExpatError

from omnigent.process_logging import data_dir

LAUNCHD_LABEL = "ai.omnigent.host"
SYSTEMD_UNIT = "omnigent-host.service"
# launchctl bootout returns before the job is gone; poll for the unload to land.
_LAUNCHD_UNLOAD_TIMEOUT = 10.0
_LAUNCHD_UNLOAD_POLL_INTERVAL = 0.2
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
HostServiceManagerState: TypeAlias = Literal["running", "stopped", "failed", "unavailable"]


class HostServiceError(RuntimeError):
    """Raised when a host service cannot be installed or removed."""


@dataclass(frozen=True)
class HostService:
    """Description of the current platform's per-user host service."""

    kind: Literal["launchd", "systemd_user"]
    path: Path
    label: str
    log_path: Path | None = None


@dataclass(frozen=True)
class HostServiceStatus:
    """Installed definition and service-manager state for the user service."""

    supported: bool
    kind: str | None = None
    path: Path | None = None
    label: str | None = None
    installed: bool = False
    configured_target: str | None = None
    executable: str | None = None
    definition_error: str | None = None
    manager_state: HostServiceManagerState = "stopped"
    manager_pid: int | None = None
    enabled: bool | None = None
    manager_error: str | None = None
    log: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable service status payload."""
        return {
            "supported": self.supported,
            "kind": self.kind,
            "path": str(self.path) if self.path is not None else None,
            "label": self.label,
            "installed": self.installed,
            "configured_target": self.configured_target,
            "executable": self.executable,
            "definition_error": self.definition_error,
            "manager_state": self.manager_state,
            "manager_pid": self.manager_pid,
            "enabled": self.enabled,
            "manager_error": self.manager_error,
            "log": self.log,
        }


def _service_for_current_platform() -> HostService:
    """Return the current platform's per-user service description."""
    system = platform.system()
    if system == "Darwin":
        log_path = data_dir() / "logs" / "host" / "service.log"
        return HostService(
            kind="launchd",
            path=Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist",
            label=LAUNCHD_LABEL,
            log_path=log_path,
        )
    if system == "Linux":
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        return HostService(
            kind="systemd_user",
            path=config_home / "systemd" / "user" / SYSTEMD_UNIT,
            label=SYSTEMD_UNIT,
        )
    raise HostServiceError(
        f"Host services are supported on macOS and Linux, not {system or 'this platform'}."
    )


def _service_command_details(command: Sequence[object]) -> tuple[str, str]:
    """Return the executable and target from an Omnigent service command."""
    if not all(isinstance(part, str) for part in command):
        raise ValueError("service command arguments must be strings")
    parts = [part for part in command if isinstance(part, str)]
    if len(parts) < 4 or parts[1:3] != ["-m", "omnigent.host.service_entry"]:
        raise ValueError("service command is not an Omnigent host service entry point")
    mode = parts[3:]
    if mode == ["--local"]:
        return parts[0], "local"
    if len(mode) == 2 and mode[0] == "--server" and mode[1]:
        return parts[0], mode[1]
    raise ValueError("service command must select exactly one local or remote target")


def _read_launchd_definition(path: Path) -> tuple[str, str]:
    """Read the executable and target from an Omnigent launchd plist."""
    payload = plistlib.loads(path.read_bytes())
    if not isinstance(payload, dict) or payload.get("Label") != LAUNCHD_LABEL:
        raise ValueError("launchd definition has an unexpected label")
    command = payload.get("ProgramArguments")
    if not isinstance(command, list):
        raise ValueError("launchd definition has no ProgramArguments array")
    return _service_command_details(command)


def _read_systemd_definition(path: Path) -> tuple[str, str]:
    """Read the executable and target from an Omnigent systemd unit."""
    exec_value: str | None = None
    for line in path.read_text().splitlines():
        if line.startswith("ExecStart="):
            exec_value = line.removeprefix("ExecStart=")
            break
    if exec_value is None:
        raise ValueError("systemd definition has no ExecStart command")
    command = [part.replace("$$", "$").replace("%%", "%") for part in shlex.split(exec_value)]
    return _service_command_details(command)


def _read_service_definition(service: HostService) -> tuple[str | None, str | None, str | None]:
    """Return configured target, executable, and a safe parse error."""
    if not service.path.exists():
        return None, None, None
    try:
        if service.kind == "launchd":
            executable, target = _read_launchd_definition(service.path)
        else:
            executable, target = _read_systemd_definition(service.path)
    except (OSError, ValueError, plistlib.InvalidFileException, ExpatError) as exc:
        return None, None, str(exc)
    return target, executable, None


def _run_probe(args: Sequence[str]) -> tuple[subprocess.CompletedProcess[str] | None, str | None]:
    """Run a bounded read-only service-manager probe."""
    try:
        return (
            subprocess.run(
                list(args),
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            ),
            None,
        )
    except FileNotFoundError:
        return None, f"service manager {args[0]!r} was not found"
    except subprocess.TimeoutExpired:
        return None, f"service manager probe timed out: {' '.join(args)}"
    except OSError as exc:
        return None, f"service manager probe failed: {exc}"


def _launchd_manager_status(
    service: HostService,
) -> tuple[HostServiceManagerState, int | None, bool | None, str | None]:
    """Probe one launchd user agent without changing it."""
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{service.label}"
    result, error = _run_probe(["launchctl", "print", target])
    if result is None:
        return "unavailable", None, None, error

    enabled: bool | None = service.path.exists()
    disabled_result, _ = _run_probe(["launchctl", "print-disabled", domain])
    if disabled_result is not None and disabled_result.returncode == 0:
        match = re.search(
            rf'"?{re.escape(service.label)}"?\s*=>\s*(true|false)',
            disabled_result.stdout,
            flags=re.IGNORECASE,
        )
        if match is not None:
            enabled = match.group(1).lower() != "true"

    if result.returncode != 0:
        return "stopped", None, enabled, None
    pid_match = re.search(r"^\s*pid\s*=\s*(\d+)\s*$", result.stdout, flags=re.MULTILINE)
    pid = int(pid_match.group(1)) if pid_match is not None else None
    state_match = re.search(r"^\s*state\s*=\s*(\S+)\s*$", result.stdout, flags=re.MULTILINE)
    state = state_match.group(1).lower() if state_match is not None else ""
    exit_match = re.search(
        r"^\s*last exit code\s*=\s*(-?\d+)\s*$",
        result.stdout,
        flags=re.MULTILINE,
    )
    exit_code = int(exit_match.group(1)) if exit_match is not None else 0
    if pid is not None or state == "running":
        return "running", pid, enabled, None
    if exit_code != 0:
        return "failed", None, enabled, f"last exit code: {exit_code}"
    return "stopped", None, enabled, None


def _systemd_manager_status(
    service: HostService,
) -> tuple[HostServiceManagerState, int | None, bool | None, str | None]:
    """Probe one systemd user service without changing it."""
    args = [
        "systemctl",
        "--user",
        "show",
        service.label,
        "--no-pager",
        "--property=LoadState",
        "--property=ActiveState",
        "--property=SubState",
        "--property=MainPID",
        "--property=UnitFileState",
        "--property=Result",
    ]
    result, error = _run_probe(args)
    if result is None:
        return "unavailable", None, None, error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if "connect" in detail.lower() or "bus" in detail.lower():
            return "unavailable", None, None, detail or "systemd user manager is unavailable"
        return "stopped", None, False if service.path.exists() else None, None

    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    pid_value = values.get("MainPID", "")
    pid = int(pid_value) if pid_value.isdigit() and int(pid_value) > 0 else None
    unit_state = values.get("UnitFileState")
    enabled = (
        unit_state in {"enabled", "enabled-runtime", "linked", "linked-runtime", "alias"}
        if unit_state
        else None
    )
    active = values.get("ActiveState", "")
    result_name = values.get("Result", "")
    if active == "active":
        return "running", pid, enabled, None
    if active == "failed" or result_name not in {"", "success"}:
        detail = result_name or values.get("SubState") or "failed"
        return "failed", pid, enabled, f"result: {detail}"
    return "stopped", pid, enabled, None


def user_host_service_status(*, probe_manager: bool = True) -> HostServiceStatus:
    """Inspect the current user's generated host-service definition and manager."""
    try:
        service = _service_for_current_platform()
    except HostServiceError as exc:
        return HostServiceStatus(
            supported=False,
            manager_state="unavailable",
            manager_error=str(exc),
        )

    target, executable, definition_error = _read_service_definition(service)
    manager_state: HostServiceManagerState = "stopped"
    manager_pid: int | None = None
    enabled: bool | None = service.path.exists()
    manager_error: str | None = None
    if probe_manager:
        if service.kind == "launchd":
            manager_state, manager_pid, enabled, manager_error = _launchd_manager_status(service)
        else:
            manager_state, manager_pid, enabled, manager_error = _systemd_manager_status(service)
    log = (
        str(service.log_path)
        if service.log_path is not None
        else f"journalctl --user -u {service.label}"
    )
    return HostServiceStatus(
        supported=True,
        kind=service.kind,
        path=service.path,
        label=service.label,
        installed=service.path.exists(),
        configured_target=target,
        executable=executable,
        definition_error=definition_error,
        manager_state=manager_state,
        manager_pid=manager_pid,
        enabled=enabled,
        manager_error=manager_error,
        log=log,
    )


def _service_command(server_url: str | None) -> list[str]:
    """Build the persistent service entry-point command."""
    mode = ["--server", server_url] if server_url else ["--local"]
    return [sys.executable, "-m", "omnigent.host.service_entry", *mode]


def _clean_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Validate environment values before persisting them in a service file."""
    cleaned: dict[str, str] = {}
    for key, value in environment.items():
        if not _ENV_NAME_RE.fullmatch(key):
            raise HostServiceError(f"Cannot persist invalid environment variable name {key!r}.")
        if "\x00" in value or "\n" in value or "\r" in value:
            raise HostServiceError(f"Cannot persist multiline environment variable {key!r}.")
        cleaned[key] = value
    return dict(sorted(cleaned.items()))


def _launchd_payload(
    service: HostService,
    *,
    command: Sequence[str],
    environment: Mapping[str, str],
) -> bytes:
    """Render a launchd user-agent plist."""
    assert service.log_path is not None
    payload = {
        "Label": service.label,
        "ProgramArguments": list(command),
        "EnvironmentVariables": dict(environment),
        "RunAtLoad": True,
        # service_entry maps permanent host failures to a successful exit.
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "StandardOutPath": str(service.log_path),
        "StandardErrorPath": str(service.log_path),
        # No ProcessType: the default (Standard) keeps runner/harness children
        # out of the background QoS band, which would starve their deadlines.
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def _systemd_quote(value: str, *, escape_dollar: bool = False) -> str:
    """Quote one systemd unit value without invoking a shell."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if escape_dollar:
        escaped = escaped.replace("$", "$$")
    return f'"{escaped}"'


def _systemd_unit(
    *,
    command: Sequence[str],
    environment: Mapping[str, str],
) -> bytes:
    """Render a systemd user service unit."""
    env_lines = [
        f"Environment={_systemd_quote(f'{key}={value}')}" for key, value in environment.items()
    ]
    lines = [
        "[Unit]",
        "Description=Omnigent host",
        "Wants=network-online.target",
        "After=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        *env_lines,
        "ExecStart=" + " ".join(_systemd_quote(part, escape_dollar=True) for part in command),
        "Restart=on-failure",
        "RestartPreventExitStatus=78 143",
        "RestartSec=10s",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines).encode()


def _atomic_write(path: Path, content: bytes) -> None:
    """Atomically write a private service definition."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        path.chmod(0o600)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _run_checked(args: Sequence[str]) -> None:
    """Run one service-manager command and surface a concise failure."""
    try:
        subprocess.run(
            list(args),
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise HostServiceError(f"Required service manager {args[0]!r} was not found.") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise HostServiceError(
            f"Service manager command failed ({' '.join(args)}){suffix}"
        ) from exc


def _run_best_effort(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run an idempotent service-manager cleanup command."""
    try:
        return subprocess.run(
            list(args),
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise HostServiceError(f"Required service manager {args[0]!r} was not found.") from exc


def _wait_for_launchd_unload(service_target: str) -> bool:
    """Wait for launchd to finish unloading a booted-out job.

    ``launchctl bootout`` unloads asynchronously, so a ``launchctl print``
    issued right after it can still report the job during the unload window.
    """
    deadline = time.monotonic() + _LAUNCHD_UNLOAD_TIMEOUT
    while True:
        if _run_best_effort(["launchctl", "print", service_target]).returncode != 0:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_LAUNCHD_UNLOAD_POLL_INTERVAL)


def _restore_file(path: Path, previous: bytes | None) -> None:
    """Restore a service definition after a manager command fails."""
    if previous is None:
        path.unlink(missing_ok=True)
    else:
        _atomic_write(path, previous)


def _enable_launchd(service: HostService, content: bytes) -> None:
    assert service.log_path is not None
    service.log_path.parent.mkdir(parents=True, exist_ok=True)
    previous = service.path.read_bytes() if service.path.exists() else None
    domain = f"gui/{os.getuid()}"
    _run_best_effort(["launchctl", "bootout", f"{domain}/{service.label}"])
    _atomic_write(service.path, content)
    try:
        _run_checked(["launchctl", "bootstrap", domain, str(service.path)])
    except HostServiceError:
        _restore_file(service.path, previous)
        if previous is not None:
            _run_best_effort(["launchctl", "bootstrap", domain, str(service.path)])
        raise


def _enable_systemd(service: HostService, content: bytes) -> None:
    previous = service.path.read_bytes() if service.path.exists() else None
    changed = previous != content
    _atomic_write(service.path, content)
    try:
        _run_checked(["systemctl", "--user", "daemon-reload"])
        _run_checked(["systemctl", "--user", "enable", "--now", service.label])
        if previous is not None and changed:
            _run_checked(["systemctl", "--user", "restart", service.label])
    except HostServiceError:
        _restore_file(service.path, previous)
        _run_best_effort(["systemctl", "--user", "daemon-reload"])
        raise


def _record_service(service: HostService) -> None:
    """Add the service to the uninstall ledger."""
    from omnigent.install_ledger import LaunchAgentEntry, record_launch_agent

    try:
        record_launch_agent(
            LaunchAgentEntry(
                kind=service.kind,
                path=str(service.path),
                label=service.label,
                source="recorded",
                confidence="certain",
            )
        )
    except OSError as exc:
        raise HostServiceError(
            f"The service was enabled, but its uninstall record could not be written: {exc}"
        ) from exc


def _forget_service(service: HostService) -> None:
    """Remove the service from install ledgers."""
    from omnigent.install_ledger import remove_launch_agent

    try:
        remove_launch_agent(kind=service.kind, label=service.label)
    except OSError as exc:
        raise HostServiceError(
            f"The service was disabled, but its uninstall record could not be updated: {exc}"
        ) from exc


def enable_user_host_service(
    server_url: str | None,
    *,
    environment: Mapping[str, str],
) -> HostService:
    """Install, enable, and start the current user's host service."""
    service = _service_for_current_platform()
    command = _service_command(server_url)
    clean_environment = _clean_environment(environment)
    if service.kind == "launchd":
        content = _launchd_payload(
            service,
            command=command,
            environment=clean_environment,
        )
        _enable_launchd(service, content)
    else:
        content = _systemd_unit(command=command, environment=clean_environment)
        _enable_systemd(service, content)
    _record_service(service)
    return service


def disable_user_host_service() -> HostService:
    """Stop, disable, and remove the current user's host service."""
    service = _service_for_current_platform()
    still_running = False
    if service.kind == "launchd":
        domain = f"gui/{os.getuid()}"
        service_target = f"{domain}/{service.label}"
        _run_best_effort(["launchctl", "bootout", service_target])
        still_running = not _wait_for_launchd_unload(service_target)
        # Unlink even when the job lingers: a retained RunAtLoad plist would
        # silently restore the service at the next login.
        service.path.unlink(missing_ok=True)
    else:
        disable_args = ["systemctl", "--user", "disable", "--now", service.label]
        if service.path.exists():
            _run_checked(disable_args)
        else:
            _run_best_effort(disable_args)
        service.path.unlink(missing_ok=True)
        _run_checked(["systemctl", "--user", "daemon-reload"])
    _forget_service(service)
    if still_running:
        raise HostServiceError(
            f"launchd service {service.label!r} is still running; its definition "
            "was removed, so it will not return at the next login."
        )
    return service
