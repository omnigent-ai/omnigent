"""Real Secret Service lookup and Linux sandbox isolation with disposable credentials."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.os_env import create_os_environment
from omnigent.inner.sandbox import create_exec_launcher, resolve_sandbox
from tests.inner.sandbox.conftest import run_async

pytestmark = pytest.mark.skipif(
    sys.platform != "linux"
    or any(
        shutil.which(name) is None for name in ("dbus-daemon", "gnome-keyring-daemon", "bwrap")
    ),
    reason="requires Linux, dbus-daemon, gnome-keyring-daemon, and bwrap",
)


@pytest.fixture
def desktop_keyring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    runtime = tmp_path / "desktop"
    runtime.mkdir(mode=0o700)
    bus_path = runtime / "bus"
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", f"unix:path={bus_path}")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("OMNIGENT_DISABLE_KEYRING", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    processes: list[subprocess.Popen[bytes]] = []
    try:
        bus = subprocess.Popen(
            ["dbus-daemon", "--session", "--nofork", f"--address=unix:path={bus_path}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        processes.append(bus)
        deadline = time.monotonic() + 10
        while not bus_path.exists():
            assert bus.poll() is None and time.monotonic() < deadline, "test bus did not start"
            time.sleep(0.05)
        daemon = subprocess.Popen(
            [
                "gnome-keyring-daemon",
                "--foreground",
                "--unlock",
                "--components=secrets",
                f"--control-directory={runtime / 'keyring'}",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        processes.append(daemon)
        assert daemon.stdin is not None
        daemon.stdin.write(b"disposable-test-password")
        daemon.stdin.close()
        probe = (
            "from keyring.backends.SecretService import Keyring; "
            "Keyring().set_password('omnigent', 'desktop-test', 'test-only-credential')"
        )
        while True:
            ready = subprocess.run(
                [sys.executable, "-c", probe], capture_output=True, text=True, timeout=10
            )
            if ready.returncode == 0:
                break
            assert time.monotonic() < deadline and daemon.poll() is None, ready.stderr
            time.sleep(0.05)
        yield bus_path
    finally:
        for process in reversed(processes):
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


@pytest.mark.parametrize("recipient", ["runner", "goose"])
def test_runner_and_granted_goose_read_real_desktop_keyring(
    desktop_keyring: Path, tmp_path: Path, recipient: str
) -> None:
    from omnigent.cli import _build_host_daemon_env
    from omnigent.host.connect import _build_runner_env

    env = _build_runner_env(
        _build_host_daemon_env(server_url=None),
        server_url="http://localhost:6767",
        runner_id="test-keyring",
        binding_token="test-binding-token",
        workspace=str(tmp_path),
        parent_pid=os.getpid(),
    )
    if recipient == "goose":
        from omnigent.inner.goose_executor import GooseExecutor
        from omnigent.runner.app import _build_spawn_env_from_spec
        from omnigent.runtime.harnesses.process_manager import _build_harness_spawn_env
        from omnigent.spec.types import AgentSpec, ExecutorSpec

        os_env = OSEnvSpec(
            type="caller_process",
            sandbox=OSEnvSandboxSpec(
                type="none", env_passthrough=["DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"]
            ),
        )
        spec = AgentSpec(
            spec_version=1,
            name="desktop-test",
            executor=ExecutorSpec(type="omnigent", config={"harness": "goose"}),
            os_env=os_env,
        )
        with patch.dict(os.environ, env, clear=True):
            harness_env = _build_harness_spawn_env(_build_spawn_env_from_spec(spec, "goose"))
        with patch.dict(os.environ, harness_env, clear=True):
            executor = GooseExecutor.__new__(GooseExecutor)
            executor._os_env = os_env
            executor._provider = None
            executor._model = None
            env = executor._build_spawn_env()
    lookup = (
        "resolve_secret('keychain:desktop-test')"
        if recipient == "runner"
        else "keyring.get_password('omnigent', 'desktop-test')"
    )
    probe = f"""
import keyring
from keyring.backends.SecretService import Keyring
from omnigent.onboarding.provider_config import resolve_secret
keyring.set_keyring(Keyring())
assert {lookup} == 'test-only-credential'
print('credential resolved')
"""
    result = subprocess.run(
        [sys.executable, "-c", probe], env=env, capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "credential resolved"
    env.pop("DBUS_SESSION_BUS_ADDRESS")
    env.pop("XDG_RUNTIME_DIR")
    missing_session = subprocess.run(
        [sys.executable, "-c", probe], env=env, capture_output=True, text=True, timeout=15
    )
    assert missing_session.returncode != 0


@pytest.mark.parametrize("launch_path", ["helper", "launcher"])
def test_sandbox_cannot_reach_known_desktop_bus_or_parent_env(
    desktop_keyring: Path, tmp_path: Path, launch_path: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec = OSEnvSpec(
        type="caller_process",
        cwd=str(workspace),
        sandbox=OSEnvSandboxSpec(
            type="linux_bwrap",
            read_paths=[str(Path(__file__).resolve().parents[3])],
            env_passthrough=["DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"],
        ),
    )
    probe = f"""
import os, socket
from pathlib import Path
assert 'DBUS_SESSION_BUS_ADDRESS' not in os.environ
assert os.environ['XDG_RUNTIME_DIR'] != {str(desktop_keyring.parent)!r}
try:
    with socket.socket(socket.AF_UNIX) as sock:
        sock.connect({str(desktop_keyring)!r})
except OSError:
    pass
else:
    raise AssertionError('host desktop bus reachable')
try:
    Path('/proc/{os.getpid()}/environ').read_bytes()
except OSError:
    pass
else:
    raise AssertionError('parent environment readable')
print('desktop isolated')
"""
    if launch_path == "helper":
        helper = create_os_environment(spec)
        try:
            result = run_async(helper.shell(shlex.join([sys.executable, "-c", probe])))
        finally:
            helper.close()
        assert result["exit_code"] == 0, result
        assert str(result["stdout"]).strip() == "desktop isolated"
    else:
        launcher = create_exec_launcher(sys.executable, resolve_sandbox(spec, workspace))
        try:
            completed = subprocess.run(
                [launcher, "-c", probe], cwd=workspace, capture_output=True, text=True, timeout=30
            )
        finally:
            Path(launcher).unlink(missing_ok=True)
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == "desktop isolated"
