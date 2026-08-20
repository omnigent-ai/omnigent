"""Tests for :mod:`omnigent.onboarding.sandboxes.cwsandbox`."""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

import click
import pytest

from omnigent.onboarding.sandboxes.base import DEFAULT_HOST_IMAGE
from omnigent.onboarding.sandboxes.cwsandbox import (
    HOST_IMAGE_ENV_VAR,
    SANDBOX_ENV_PASSTHROUGH_ENV_VAR,
    CWSandboxLauncher,
)

# ── Fake cwsandbox SDK ──────────────────────────────────────
#
# The SDK is an optional dependency the test env may not install, and
# real Sandbox objects only exist server-side — so these are hand-rolled
# stubs injected via sys.modules, resolving the launcher's function-local
# `import cwsandbox` / `from cwsandbox.exceptions import ...`.


class _CWSandboxError(Exception):
    pass


class _SandboxNotFoundError(_CWSandboxError):
    pass


@dataclass
class _FakeNetworkOptions:
    egress_mode: str | None = None
    ingress_mode: str | None = None
    exposed_ports: tuple[int, ...] | None = None


@dataclass
class _FakeResult:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


class _FakeOp:
    """Stands in for an OperationRef: `.result()` returns the value."""

    def __init__(self, value: object = None) -> None:
        self._value = value

    def result(self, timeout: float | None = None) -> object:
        return self._value


class _FakeProcess:
    def __init__(self, result: _FakeResult, *, wait_raises: BaseException | None = None) -> None:
        self._result = result
        self._wait_raises = wait_raises
        self.cancelled = False

    @property
    def stdout(self):
        return iter(self._result.stdout.splitlines(keepends=True))

    def result(self, timeout: float | None = None) -> _FakeResult:
        return self._result

    def wait(self, timeout: float | None = None) -> int:
        if self._wait_raises is not None:
            raise self._wait_raises
        return self._result.returncode

    def cancel(self) -> bool:
        self.cancelled = True
        return True


@dataclass
class _State:
    """Shared recorder for assertions."""

    run_kwargs: dict = field(default_factory=dict)
    run_command: tuple = ()
    written: list[tuple[str, bytes]] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    exec_result: _FakeResult = field(default_factory=_FakeResult)
    from_id_missing: bool = False
    # Each exec() call's command (list argv), in order.
    exec_commands: list[list] = field(default_factory=list)
    # Processes handed back by successive exec() calls, in order.
    exec_processes: list[_FakeProcess] = field(default_factory=list)


class _FakeSandbox:
    _state: _State

    def __init__(self, sandbox_id: str = "sb-1") -> None:
        self._sandbox_id = sandbox_id

    @property
    def sandbox_id(self) -> str:
        return self._sandbox_id

    @classmethod
    def run(cls, *command, **kwargs) -> _FakeSandbox:
        cls._state.run_command = command
        cls._state.run_kwargs = kwargs
        return cls()

    @classmethod
    def from_id(cls, sandbox_id: str) -> _FakeOp:
        if cls._state.from_id_missing:
            raise _SandboxNotFoundError(sandbox_id)
        return _FakeOp(cls(sandbox_id))

    def wait(self, timeout: float | None = None) -> _FakeSandbox:
        return self

    def exec(self, command, **kwargs) -> _FakeProcess:
        self._state.exec_commands.append(list(command))
        if self._state.exec_processes:
            return self._state.exec_processes.pop(0)
        return _FakeProcess(self._state.exec_result)

    def write_file(self, path: str, data: bytes) -> _FakeOp:
        self._state.written.append((path, data))
        return _FakeOp(None)

    def stop(self) -> _FakeOp:
        self._state.stopped.append(self._sandbox_id)
        return _FakeOp(None)


@pytest.fixture()
def sdk(monkeypatch: pytest.MonkeyPatch) -> _State:
    state = _State()
    _FakeSandbox._state = state

    mod = types.ModuleType("cwsandbox")
    mod.Sandbox = _FakeSandbox  # type: ignore[attr-defined]
    mod.NetworkOptions = _FakeNetworkOptions  # type: ignore[attr-defined]
    exc = types.ModuleType("cwsandbox.exceptions")
    exc.CWSandboxError = _CWSandboxError  # type: ignore[attr-defined]
    exc.SandboxNotFoundError = _SandboxNotFoundError  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "cwsandbox", mod)
    monkeypatch.setitem(sys.modules, "cwsandbox.exceptions", exc)
    monkeypatch.setenv("CWSANDBOX_API_KEY", "cw-test-key")
    monkeypatch.delenv(HOST_IMAGE_ENV_VAR, raising=False)
    monkeypatch.delenv(SANDBOX_ENV_PASSTHROUGH_ENV_VAR, raising=False)
    return state


def test_prepare_requires_api_key(sdk: _State, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CWSANDBOX_API_KEY")
    with pytest.raises(click.ClickException, match="CWSANDBOX_API_KEY"):
        CWSandboxLauncher().prepare()


def test_provision_requests_host_image_and_egress(sdk: _State) -> None:
    assert CWSandboxLauncher().provision("managed-x") == "sb-1"
    assert sdk.run_command == ("sleep", "infinity")
    assert sdk.run_kwargs["container_image"] == DEFAULT_HOST_IMAGE
    assert sdk.run_kwargs["network"].egress_mode == "internet"
    assert sdk.run_kwargs["tags"] == ["omnigent", "managed-x"]


def test_provision_image_resolution_order(sdk: _State, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(HOST_IMAGE_ENV_VAR, "ghcr.io/env/override:1")
    CWSandboxLauncher(image="ghcr.io/explicit/img:2").provision("x")
    assert sdk.run_kwargs["container_image"] == "ghcr.io/explicit/img:2"


def test_provision_env_passthrough_from_server_env(
    sdk: _State, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-123")
    CWSandboxLauncher(env=["ANTHROPIC_API_KEY"]).provision("x")
    assert sdk.run_kwargs["environment_variables"] == {"ANTHROPIC_API_KEY": "sk-ant-123"}


def test_provision_env_passthrough_missing_var_fails_loud(sdk: _State) -> None:
    with pytest.raises(click.ClickException, match="NOT_SET_ANYWHERE"):
        CWSandboxLauncher(env=["NOT_SET_ANYWHERE"]).provision("x")


def test_run_returns_output_and_exit_code(sdk: _State) -> None:
    sdk.exec_result = _FakeResult(stdout="hi\n", returncode=0)
    result = CWSandboxLauncher().run("sb-1", "echo hi")
    assert result.returncode == 0 and result.stdout == "hi\n"


def test_run_raises_on_nonzero_when_checked(sdk: _State) -> None:
    sdk.exec_result = _FakeResult(returncode=3)
    launcher = CWSandboxLauncher()
    with pytest.raises(click.ClickException, match="exit 3"):
        launcher.run("sb-1", "false")
    assert launcher.run("sb-1", "false", check=False).returncode == 3


def test_put_writes_bytes(sdk: _State, tmp_path: Path) -> None:
    local = tmp_path / "wheels.tgz"
    local.write_bytes(b"binary\x00data")
    CWSandboxLauncher().put("sb-1", local, "/tmp/wheels.tgz")
    assert sdk.written == [("/tmp/wheels.tgz", b"binary\x00data")]


def test_terminate_swallows_not_found(sdk: _State) -> None:
    sdk.from_id_missing = True
    CWSandboxLauncher().terminate("already-gone")  # must not raise
    assert sdk.stopped == []


def test_terminate_stops_existing(sdk: _State) -> None:
    CWSandboxLauncher().terminate("sb-1")
    assert sdk.stopped == ["sb-1"]


# ── exec_foreground ─────────────────────────────────────────


def test_exec_foreground_records_pid_and_streams_output(sdk: _State) -> None:
    """The foreground command records its pid in a private mode-700 dir."""
    sdk.exec_processes = [
        _FakeProcess(_FakeResult(stdout="host-output\n", returncode=0)),
        _FakeProcess(_FakeResult()),  # cleanup exec on normal exit
    ]

    returncode = CWSandboxLauncher().exec_foreground("sb-1", "omnigent host --server u")

    assert returncode == 0
    remote = sdk.exec_commands[0][-1]
    # The pidfile lives in a private, unpredictably-named dir created mode 700
    # (fails closed if it already exists) so /tmp can't be pre-seeded.
    assert "mkdir -m 700 /tmp/oa-foreground-" in remote
    assert "echo $$ > /tmp/oa-foreground-" in remote and "/pid" in remote
    # `exec` keeps the recorded pid across the swap to the real command.
    assert "exec omnigent host --server u" in remote
    # A normal exit cleans up the run dir so it isn't orphaned in /tmp.
    assert len(sdk.exec_commands) == 2
    cleanup = sdk.exec_commands[1][-1]
    assert cleanup.startswith("rm -rf /tmp/oa-foreground-")


def test_exec_foreground_kills_remote_on_interrupt(sdk: _State) -> None:
    """Ctrl-C kills the remote process (via the pidfile) and re-raises."""
    sdk.exec_processes = [_FakeProcess(_FakeResult(), wait_raises=KeyboardInterrupt())]

    with pytest.raises(KeyboardInterrupt):
        CWSandboxLauncher().exec_foreground("sb-1", "omnigent host --server u")

    # Second exec is the kill, addressed via the recorded pidfile. The pid is
    # validated as numeric before being signalled, and the dir is cleaned up.
    assert len(sdk.exec_commands) == 2
    kill = sdk.exec_commands[1][-1]
    assert 'case "$pid" in' in kill and 'kill "$pid"' in kill
    assert "rm -rf /tmp/oa-foreground-" in kill
