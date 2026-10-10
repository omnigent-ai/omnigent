"""Host-service lifecycle across ``omnigent upgrade`` e2e.

``omnigent host enable`` installs a per-user service (launchd on macOS,
``systemd --user`` on Linux) that keeps the host running across logins.
``omnigent upgrade`` must stop that service before the installer replaces the
code and re-install it afterwards; otherwise the service keeps running the old
code until the next login while the CLI reports a successful update.

Drives the real ``omnigent host enable`` and ``omnigent upgrade`` CLI as
subprocesses, exactly as a user types them, against:

- a scripted ``systemctl`` / ``launchctl`` that records every invocation and
  runs a placeholder process standing in for the service process;
- a ``sitecustomize`` that makes ``omnigent.update_check`` see a uv-tool
  install (VCS or registry) whose installed commit/version lives in a state
  file, plus a fake ``uv`` that records the install command and advances that
  state file the way a real reinstall would;
- a local bare git repository as the tracked VCS ref, so the real
  ``git ls-remote`` decides whether the install is behind.

Usage::

    python -m pytest tests/e2e/test_upgrade_host_service_e2e.py -v
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="per-user host services exist only on macOS and Linux",
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

_KINDS: tuple[str, ...] = ("systemd_user", "launchd")
_SYSTEMD_UNIT = "omnigent-host.service"
_LAUNCHD_LABEL = "ai.omnigent.host"
_OLD_VERSION = "0.1.0"
_NEW_VERSION = "0.2.0"
# A restart issued asynchronously by a service manager still lands well
# within this window; nothing after it is attributed to the upgrade.
_POST_UPGRADE_SETTLE_S = 3.0

# Scripted service manager. The same script serves as ``systemctl`` and
# ``launchctl`` (dispatching on argv[0]), appends every invocation to
# calls.log, and models the service process with a detached ``sleep`` whose
# PID and start identity (ExecStart + PATH of the definition it was started
# from) are recorded so a test can tell a restarted service from the original.
_SERVICE_MANAGER_STUB = """\
#!{python}
import json
import os
import plistlib
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

state = Path(os.environ["SERVICE_STUB_STATE"])
state.mkdir(parents=True, exist_ok=True)
definition = Path(os.environ["SERVICE_STUB_DEFINITION"])
tool = Path(sys.argv[0]).name
args = sys.argv[1:]
with (state / "calls.log").open("a") as log:
    log.write(json.dumps({{"t": time.time(), "argv": [tool, *args]}}) + "\\n")
pid_file = state / "service.pid"
started_file = state / "started_from.json"


def alive():
    try:
        pid = int(pid_file.read_text())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return None
    return pid


def identity():
    raw = definition.read_bytes()
    if tool == "launchctl":
        payload = plistlib.loads(raw)
        return {{
            "exec": payload["ProgramArguments"],
            "path": payload.get("EnvironmentVariables", {{}}).get("PATH"),
        }}
    text = raw.decode()
    exec_line = next((l for l in text.splitlines() if l.startswith("ExecStart=")), "")
    match = re.search(r'^Environment="PATH=(.*)"$', text, re.MULTILINE)
    return {{"exec": exec_line, "path": match.group(1) if match else None}}


def start():
    if alive():
        return 0
    if not definition.exists():
        sys.stderr.write(f"{{tool}}: {{definition}} does not exist\\n")
        return 1
    proc = subprocess.Popen(
        ["sleep", "1000000"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    pid_file.write_text(str(proc.pid))
    started_file.write_text(
        json.dumps({{"pid": proc.pid, "started_at": time.time(), **identity()}})
    )
    return 0


def stop():
    pid = alive()
    if pid:
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while alive() and time.monotonic() < deadline:
            time.sleep(0.05)
    pid_file.unlink(missing_ok=True)
    return 0


def status():
    pid = alive()
    label = os.environ["SERVICE_STUB_LABEL"]
    if pid is None:
        print(f"o {{label}} - Omnigent host\\n     Active: inactive (dead)")
        return 3
    started = json.loads(started_file.read_text())
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started["started_at"]))
    path = identity()["path"] or ""
    head, sep, _rest = path.partition(":")
    print(f"* {{label}} - Omnigent host")
    print(f"     Active: active (running) since {{since}}")
    print(f"   Main PID: {{pid}}")
    print(f"  Definition PATH: {{head}}{{sep and ':...'}}")
    return 0


if tool == "systemctl":
    words = [a for a in args if not a.startswith("--")]
    cmd = words[0] if words else ""
    now = "--now" in args
    if cmd in ("daemon-reload", "is-enabled", "show", "reset-failed", "kill", ""):
        code = 0
    elif cmd == "enable":
        code = start() if now else 0
    elif cmd == "disable":
        code = stop() if now else 0
    elif cmd == "start":
        code = start()
    elif cmd == "stop":
        code = stop()
    elif cmd in ("restart", "try-restart", "reload-or-restart"):
        stop()
        code = start()
    elif cmd == "is-active":
        print("active" if alive() else "inactive")
        code = 0 if alive() else 3
    elif cmd == "status":
        code = status()
    else:
        sys.stderr.write(f"systemctl stub: unsupported command {{args!r}}\\n")
        code = 1
else:
    cmd = args[0] if args else ""
    if cmd == "bootstrap":
        code = start()
    elif cmd == "bootout":
        code = stop()
    elif cmd == "print":
        code = 0 if alive() else 113
    elif cmd == "kickstart":
        if "-k" in args:
            stop()
        code = start()
    elif cmd in ("enable", "disable", ""):
        code = 0
    elif cmd == "list":
        code = status()
    else:
        sys.stderr.write(f"launchctl stub: unsupported command {{args!r}}\\n")
        code = 1
raise SystemExit(code)
"""

# Fake installer: records every ``uv`` invocation and, for ``tool install`` /
# ``tool upgrade`` only, advances the install-state file the way a real
# reinstall moves the on-disk install (new commit for a VCS install, new
# version for a registry one). The CLI also queries ``uv --version`` and
# ``uv tool dir`` while recording the uninstall ledger; those are not installs.
_UV_STUB = """\
#!{python}
import json
import os
import sys
import time
from pathlib import Path

state_path = Path(os.environ["OMNIGENT_UPGRADE_STUB_STATE"])
log = Path(os.environ["SERVICE_STUB_STATE"]) / "uv-calls.log"
args = sys.argv[1:]
with log.open("a") as handle:
    handle.write(json.dumps({{"t": time.time(), "argv": args}}) + "\\n")
if args[:1] == ["--version"]:
    print("uv 0.12.23 (stub)")
elif args[:2] == ["tool", "dir"]:
    tool_dir = Path(os.environ["UV_STUB_TOOL_DIR"])
    print(tool_dir / "bin" if "--bin" in args else tool_dir)
elif args[:2] in (["tool", "install"], ["tool", "upgrade"]):
    state = json.loads(state_path.read_text())
    if os.environ.get("UV_STUB_NEW_COMMIT"):
        state["commit_sha"] = os.environ["UV_STUB_NEW_COMMIT"]
    if os.environ.get("UV_STUB_NEW_VERSION"):
        state["version"] = os.environ["UV_STUB_NEW_VERSION"]
    state["installed_at"] = time.time()
    state_path.write_text(json.dumps(state))
    print("Installed 1 executable: omnigent")
"""

# Imported at interpreter startup (site picks it up from PYTHONPATH) by the
# CLI subprocess and by the fresh interpreter ``_probe_installed_distribution``
# spawns, so both see the same uv-tool install described by the state file.
_SITECUSTOMIZE = """\
import importlib.metadata as _metadata
import json as _json
import os as _os
from pathlib import Path as _Path

_state_path = _Path(_os.environ["OMNIGENT_UPGRADE_STUB_STATE"])


def _state():
    return _json.loads(_state_path.read_text())


_platform = _os.environ.get("OMNIGENT_UPGRADE_STUB_PLATFORM")
if _platform:
    import platform as _platform_mod

    _platform_mod.system = lambda: _platform

_real_version = _metadata.version


def _version(name):
    if name == "omnigent" and _state().get("version"):
        return _state()["version"]
    return _real_version(name)


_metadata.version = _version

try:
    import omnigent.update_check as _update_check
except Exception:
    _update_check = None

if _update_check is not None:

    def _installed_info():
        state = _state()
        return _update_check._InstalledWheelInfo(
            install_time_epoch=0.0,
            installer="uv",
            vcs_url=state.get("vcs_url"),
            commit_sha=state.get("commit_sha"),
            is_editable=False,
            package_version=state.get("version") or "0.1.0",
            detected_installer="uv",
        )

    _update_check._find_repo_root = lambda: None
    _update_check._read_installed_wheel_info = _installed_info
    _update_check._uv_tool_receipt_path = lambda: _state_path
"""


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=e2e@example.com", "-c", "user.name=e2e", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _make_tracked_repo(root: Path) -> tuple[Path, str, str]:
    """Create a bare repo whose HEAD is one commit ahead of the installed one."""
    bare = root / "upstream.git"
    bare.mkdir()
    _git("init", "-q", "--bare", "--initial-branch=main", str(bare), cwd=root)
    work = root / "work"
    _git("clone", "-q", str(bare), str(work), cwd=root)
    _git("commit", "-q", "--allow-empty", "-m", "installed", cwd=work)
    old = _git("rev-parse", "HEAD", cwd=work)
    _git("commit", "-q", "--allow-empty", "-m", "newer", cwd=work)
    new = _git("rev-parse", "HEAD", cwd=work)
    _git("push", "-q", "origin", "HEAD:main", cwd=work)
    return bare, old, new


def _argv(call: dict[str, object]) -> list[str]:
    argv = call["argv"]
    assert isinstance(argv, list)
    return [str(part) for part in argv]


def _at(call: dict[str, object]) -> float:
    at = call["t"]
    assert isinstance(at, (int, float))
    return float(at)


def _read_log(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


@dataclass(frozen=True)
class _ServiceSnapshot:
    pid: int | None
    started_from: dict[str, object] | None
    definition: bytes | None


@dataclass(frozen=True)
class _UpgradeHarness:
    """Isolated home + stubs for driving ``omnigent`` CLI subprocesses."""

    kind: Literal["systemd_user", "launchd"]
    install: Literal["vcs", "registry"]
    root: Path
    env: dict[str, str]
    upgrade_env: dict[str, str]
    definition: Path
    service_state: Path
    install_state: Path
    old_commit: str
    new_commit: str

    @property
    def label(self) -> str:
        return _LAUNCHD_LABEL if self.kind == "launchd" else _SYSTEMD_UNIT

    def run_cli(
        self, *args: str, env: dict[str, str] | None = None, timeout: float = 180
    ) -> subprocess.CompletedProcess[str]:
        """Run ``omnigent <args>`` exactly as a user would."""
        return subprocess.run(
            [sys.executable, "-m", "omnigent.cli", *args],
            env=self.env if env is None else env,
            cwd=self.root / "cwd",
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def service_calls(self, since: float = 0.0) -> list[dict[str, object]]:
        calls = _read_log(self.service_state / "calls.log")
        return [c for c in calls if _at(c) >= since]

    def installer_runs(self) -> list[dict[str, object]]:
        """``uv`` invocations that (re)install omnigent, ignoring metadata queries."""
        calls = _read_log(self.service_state / "uv-calls.log")
        return [c for c in calls if _argv(c)[:2] in (["tool", "install"], ["tool", "upgrade"])]

    def service_pid(self) -> int | None:
        try:
            pid = int((self.service_state / "service.pid").read_text())
            os.kill(pid, 0)
        except (OSError, ValueError):
            return None
        return pid

    def snapshot(self) -> _ServiceSnapshot:
        started = self.service_state / "started_from.json"
        return _ServiceSnapshot(
            pid=self.service_pid(),
            started_from=json.loads(started.read_text()) if started.exists() else None,
            definition=self.definition.read_bytes() if self.definition.exists() else None,
        )

    def definition_path_env(self) -> str | None:
        """The PATH the installed service definition hands the host process."""
        if not self.definition.exists():
            return None
        raw = self.definition.read_bytes()
        if self.kind == "launchd":
            payload = plistlib.loads(raw)
            return payload.get("EnvironmentVariables", {}).get("PATH")
        match = re.search(r'^Environment="PATH=(.*)"$', raw.decode(), re.MULTILINE)
        return match.group(1) if match else None

    def install_state_data(self) -> dict[str, object]:
        return json.loads(self.install_state.read_text())

    def cleanup(self) -> None:
        pid = self.service_pid()
        if pid:
            os.kill(pid, signal.SIGKILL)
            deadline = time.monotonic() + 5
            while self.service_pid() == pid and time.monotonic() < deadline:
                time.sleep(0.05)


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def _build_harness(
    root: Path,
    kind: Literal["systemd_user", "launchd"],
    install: Literal["vcs", "registry"] = "vcs",
) -> _UpgradeHarness:
    """Lay out stubs, isolated state and env for one platform/install shape."""
    home = root / "home"
    home.mkdir(parents=True)
    xdg = root / "xdg"
    stub_bin = root / "bin"
    stub_bin.mkdir()
    new_install_bin = root / "new-install-bin"
    new_install_bin.mkdir()
    uv_tool_dir = root / "uv-tools"
    (uv_tool_dir / "bin").mkdir(parents=True)
    pysite = root / "pysite"
    pysite.mkdir()
    (root / "cwd").mkdir()
    service_state = root / "service-state"
    service_state.mkdir()

    if kind == "launchd":
        definition = home / "Library" / "LaunchAgents" / f"{_LAUNCHD_LABEL}.plist"
    else:
        definition = xdg / "systemd" / "user" / _SYSTEMD_UNIT

    stub = _SERVICE_MANAGER_STUB.format(python=sys.executable)
    _write_executable(stub_bin / "systemctl", stub)
    _write_executable(stub_bin / "launchctl", stub)
    _write_executable(stub_bin / "uv", _UV_STUB.format(python=sys.executable))
    (pysite / "sitecustomize.py").write_text(_SITECUSTOMIZE)

    bare, old_commit, new_commit = _make_tracked_repo(root)
    install_state = root / "install-state.json"
    if install == "vcs":
        state = {
            "vcs_url": f"git+file://{bare}",
            "commit_sha": old_commit,
            "version": _OLD_VERSION,
        }
    else:
        state = {"vcs_url": None, "commit_sha": None, "version": _OLD_VERSION}
    install_state.write_text(json.dumps(state))

    pythonpath = os.pathsep.join(
        entry
        for entry in (
            str(pysite),
            str(_REPO_ROOT),
            str(_REPO_ROOT / "sdks" / "python-client"),
            str(_REPO_ROOT / "sdks" / "ui"),
            os.environ.get("PYTHONPATH", ""),
        )
        if entry
    )
    path_enable = f"{stub_bin}{os.pathsep}{os.environ['PATH']}"
    env = {
        **os.environ,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(xdg),
        "OMNIGENT_DATA_DIR": str(root / "data"),
        "OMNIGENT_CONFIG_HOME": str(root / "config"),
        "OMNIGENT_SKIP_ONBOARD": "1",
        "OMNIGENT_NO_UPDATE_CHECK": "1",
        "PATH": path_enable,
        "PYTHONPATH": pythonpath,
        "SERVICE_STUB_STATE": str(service_state),
        "SERVICE_STUB_DEFINITION": str(definition),
        "SERVICE_STUB_LABEL": _LAUNCHD_LABEL if kind == "launchd" else _SYSTEMD_UNIT,
        "OMNIGENT_UPGRADE_STUB_STATE": str(install_state),
        "UV_STUB_TOOL_DIR": str(uv_tool_dir),
        "UV_STUB_NEW_COMMIT": new_commit if install == "vcs" else "",
        "UV_STUB_NEW_VERSION": _NEW_VERSION if install == "registry" else "",
    }
    if kind == "launchd":
        env["OMNIGENT_UPGRADE_STUB_PLATFORM"] = "Darwin"
    # The new install's bin dir leads PATH only at upgrade time, so a
    # re-installed definition is distinguishable from the original one.
    upgrade_env = {**env, "PATH": f"{new_install_bin}{os.pathsep}{path_enable}"}
    return _UpgradeHarness(
        kind=kind,
        install=install,
        root=root,
        env=env,
        upgrade_env=upgrade_env,
        definition=definition,
        service_state=service_state,
        install_state=install_state,
        old_commit=old_commit,
        new_commit=new_commit,
    )


@pytest.fixture(params=_KINDS)
def vcs_harness(request: pytest.FixtureRequest, tmp_path: Path) -> _UpgradeHarness:
    harness = _build_harness(tmp_path, request.param, "vcs")
    request.addfinalizer(harness.cleanup)
    return harness


@pytest.fixture(params=_KINDS)
def registry_harness(request: pytest.FixtureRequest, tmp_path: Path) -> _UpgradeHarness:
    harness = _build_harness(tmp_path, request.param, "registry")
    request.addfinalizer(harness.cleanup)
    return harness


def _enable_host_service(harness: _UpgradeHarness) -> _ServiceSnapshot:
    """``omnigent host enable`` and return the running service's identity."""
    enable = harness.run_cli("host", "enable")
    assert enable.returncode == 0, f"host enable failed: {enable.stdout}\n{enable.stderr}"
    assert harness.definition.exists(), "host enable did not install the service definition"
    before = harness.snapshot()
    assert before.pid is not None, "host enable did not start the service"
    assert harness.definition_path_env() == harness.env["PATH"]
    return before


def _stops_and_starts(
    harness: _UpgradeHarness, calls: list[dict[str, object]]
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Split service-manager calls into those stopping and those starting the service."""
    if harness.kind == "launchd":
        stops = [
            c for c in calls if _argv(c)[1] == "bootout" and harness.label in " ".join(_argv(c))
        ]
        starts = [c for c in calls if _argv(c)[1] in ("bootstrap", "kickstart")]
    else:
        stops = [
            c
            for c in calls
            if any(w in ("stop", "restart", "disable", "try-restart") for w in _argv(c)[1:])
            and harness.label in _argv(c)
        ]
        starts = [
            c
            for c in calls
            if any(w in ("start", "restart", "enable", "try-restart") for w in _argv(c)[1:])
            and harness.label in _argv(c)
        ]
    return stops, starts


def _assert_service_recycled_around_install(
    harness: _UpgradeHarness,
    before: _ServiceSnapshot,
    upgrade: subprocess.CompletedProcess[str],
    started_at: float,
) -> None:
    time.sleep(_POST_UPGRADE_SETTLE_S)
    calls = harness.service_calls(since=started_at)
    stops, starts = _stops_and_starts(harness, calls)
    installs = harness.installer_runs()
    assert installs, f"the installer never ran: {upgrade.stdout}\n{upgrade.stderr}"
    installed_at = _at(installs[-1])
    after = harness.snapshot()

    detail = (
        f"upgrade exit={upgrade.returncode}\nstdout={upgrade.stdout!r}\n"
        f"service-manager calls during upgrade={[_argv(c) for c in calls]!r}\n"
        f"service pid before={before.pid} after={after.pid}\n"
        f"definition PATH after={harness.definition_path_env()!r}"
    )
    assert stops, f"upgrade replaced the install without stopping {harness.label}\n{detail}"
    assert min(_at(c) for c in stops) < installed_at, (
        f"{harness.label} was still running while the installer replaced the code\n{detail}"
    )
    assert starts, f"upgrade left {harness.label} stopped after installing\n{detail}"
    assert after.pid is not None and after.pid != before.pid, (
        f"the service process started from the old install is still the one running\n{detail}"
    )
    assert harness.definition_path_env() == harness.upgrade_env["PATH"], (
        f"the service definition still carries the pre-upgrade PATH\n{detail}"
    )


def test_vcs_upgrade_restarts_enabled_host_service(vcs_harness: _UpgradeHarness) -> None:
    """A git install's re-pull must recycle the enabled host service.

    The service process keeps the pre-upgrade modules in memory while
    ``uv tool install --reinstall`` swaps the files underneath it, so a
    successful upgrade must stop it before the installer runs and bring it
    back on the new code (with a refreshed definition) afterwards.
    """
    before = _enable_host_service(vcs_harness)

    started_at = time.time()
    upgrade = vcs_harness.run_cli("upgrade", env=vcs_harness.upgrade_env)

    assert upgrade.returncode == 0, f"upgrade failed: {upgrade.stdout}\n{upgrade.stderr}"
    assert f"Updated to git {vcs_harness.new_commit[:9]}" in upgrade.stdout, upgrade.stdout
    _assert_service_recycled_around_install(vcs_harness, before, upgrade, started_at)


def test_registry_upgrade_restarts_enabled_host_service(
    registry_harness: _UpgradeHarness,
) -> None:
    """The package-index upgrade path must recycle the enabled host service too."""
    before = _enable_host_service(registry_harness)

    started_at = time.time()
    upgrade = registry_harness.run_cli(
        "upgrade", "--target-version", _NEW_VERSION, env=registry_harness.upgrade_env
    )

    assert upgrade.returncode == 0, f"upgrade failed: {upgrade.stdout}\n{upgrade.stderr}"
    assert f"Upgraded to v{_NEW_VERSION}" in upgrade.stdout, upgrade.stdout
    _assert_service_recycled_around_install(registry_harness, before, upgrade, started_at)


def test_upgrade_check_and_dry_run_leave_host_service_alone(
    registry_harness: _UpgradeHarness,
) -> None:
    """``--check`` and ``--dry-run`` must neither stop nor rewrite the service."""
    before = _enable_host_service(registry_harness)
    started_at = time.time()

    check = registry_harness.run_cli(
        "upgrade", "--check", "--target-version", _NEW_VERSION, env=registry_harness.upgrade_env
    )
    dry_run = registry_harness.run_cli(
        "upgrade", "--dry-run", "--target-version", _NEW_VERSION, env=registry_harness.upgrade_env
    )

    assert "Targeting v" in check.stdout, check.stdout
    assert dry_run.returncode == 0 and "Would run:" in dry_run.stdout, dry_run.stdout
    assert not registry_harness.installer_runs(), "--dry-run ran the installer"
    assert registry_harness.service_calls(since=started_at) == []
    after = registry_harness.snapshot()
    assert after.pid == before.pid
    assert after.definition == before.definition


def test_vcs_upgrade_dry_run_leaves_host_service_alone(vcs_harness: _UpgradeHarness) -> None:
    """``--dry-run`` on a git install must only print the re-pull command."""
    before = _enable_host_service(vcs_harness)
    started_at = time.time()

    dry_run = vcs_harness.run_cli("upgrade", "--dry-run", env=vcs_harness.upgrade_env)

    assert dry_run.returncode == 0 and "Would run:" in dry_run.stdout, dry_run.stdout
    assert not vcs_harness.installer_runs(), "--dry-run ran the installer"
    assert vcs_harness.service_calls(since=started_at) == []
    after = vcs_harness.snapshot()
    assert after.pid == before.pid
    assert after.definition == before.definition
