"""Tests for :mod:`omnigent.onboarding.sandboxes.cwsandbox`."""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import click
import pytest

from omnigent.onboarding.sandboxes.base import DEFAULT_HOST_IMAGE
from omnigent.onboarding.sandboxes.cwsandbox import (
    AUTH_STRATEGY_ENV_VAR,
    EGRESS_HOSTS_ENV_VAR,
    HOST_IMAGE_ENV_VAR,
    PLACEMENT_MODE_ENV_VAR,
    RUNNER_IDS_ENV_VAR,
    SANDBOX_ENV_PASSTHROUGH_ENV_VAR,
    CWSandboxLauncher,
)

try:
    import cwsandbox as real_sdk
except ImportError:
    real_sdk = None

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


class _FakeAuthStrategy(StrEnum):
    WANDB = "wandb"
    COREWEAVE_API_KEY = "coreweave_api_key"


class _FakePlacementMode(StrEnum):
    UNSPECIFIED = "unspecified"
    SERVERLESS = "serverless"
    CKS = "cks"


@dataclass
class _FakeEgressRule:
    dns_name: str


@dataclass
class _FakeNetworkOptions:
    egress: list[_FakeEgressRule] = field(default_factory=list)


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
    from_id_auth: object = None
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
    def from_id(cls, sandbox_id: str, *, auth=None) -> _FakeOp:
        cls._state.from_id_auth = auth
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
    mod.EgressRule = _FakeEgressRule
    mod.PlacementMode = _FakePlacementMode
    mod.AuthStrategy = _FakeAuthStrategy
    mod.NetworkOptions = _FakeNetworkOptions  # type: ignore[attr-defined]
    exc = types.ModuleType("cwsandbox.exceptions")
    exc.CWSandboxError = _CWSandboxError  # type: ignore[attr-defined]
    exc.SandboxNotFoundError = _SandboxNotFoundError  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "cwsandbox", mod)
    monkeypatch.setitem(sys.modules, "cwsandbox.exceptions", exc)
    monkeypatch.setenv("WANDB_API_KEY", "wandb-test-key")
    monkeypatch.setenv("CWSANDBOX_API_KEY", "cw-test-key")
    monkeypatch.delenv(AUTH_STRATEGY_ENV_VAR, raising=False)
    monkeypatch.delenv(EGRESS_HOSTS_ENV_VAR, raising=False)
    monkeypatch.delenv(PLACEMENT_MODE_ENV_VAR, raising=False)
    monkeypatch.delenv(RUNNER_IDS_ENV_VAR, raising=False)
    monkeypatch.delenv(HOST_IMAGE_ENV_VAR, raising=False)
    monkeypatch.delenv(SANDBOX_ENV_PASSTHROUGH_ENV_VAR, raising=False)
    return state


def test_prepare_requires_api_key(sdk: _State, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WANDB_API_KEY")
    with pytest.raises(click.ClickException, match="WANDB_API_KEY"):
        CWSandboxLauncher().prepare()


def test_provision_uses_serverless_and_network_defaults(sdk: _State) -> None:
    assert CWSandboxLauncher().provision("managed-x") == "sb-1"
    assert sdk.run_kwargs["auth"] == _FakeAuthStrategy.WANDB
    assert sdk.run_command == ("sleep", "infinity")
    assert sdk.run_kwargs["container_image"] == DEFAULT_HOST_IMAGE
    assert sdk.run_kwargs["network"] is None
    assert sdk.run_kwargs["placement_mode"] == "serverless"
    assert sdk.run_kwargs["runner_ids"] is None
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


def _use_real_network_types(monkeypatch):
    if real_sdk is None:
        pytest.skip("Install the cwsandbox extra to validate SDK network types")
    monkeypatch.setattr(sys.modules["cwsandbox"], "NetworkOptions", real_sdk.NetworkOptions)
    monkeypatch.setattr(sys.modules["cwsandbox"], "EgressRule", real_sdk.EgressRule)


def test_provision_grants_https_hosts_with_real_sdk(sdk: _State, monkeypatch: pytest.MonkeyPatch):
    _use_real_network_types(monkeypatch)
    monkeypatch.setenv(EGRESS_HOSTS_ENV_VAR, " server.example.com,api.openai.com,, ")
    CWSandboxLauncher().provision("x")
    network = sdk.run_kwargs["network"]
    assert isinstance(network, real_sdk.NetworkOptions)
    assert [rule.dns_name for rule in network.egress] == ["server.example.com", "api.openai.com"]


def test_invalid_egress_host_fails_before_provision(sdk: _State, monkeypatch: pytest.MonkeyPatch):
    _use_real_network_types(monkeypatch)
    monkeypatch.setenv(EGRESS_HOSTS_ENV_VAR, "https://server.example.com/path")
    with pytest.raises(click.ClickException, match=EGRESS_HOSTS_ENV_VAR):
        CWSandboxLauncher().provision("x")
    assert not sdk.run_kwargs


@pytest.mark.parametrize("mode", ["cks", " CKS "])
def test_provision_selects_cks_runner(sdk: _State, monkeypatch: pytest.MonkeyPatch, mode):
    monkeypatch.setenv(PLACEMENT_MODE_ENV_VAR, mode)
    monkeypatch.setenv(RUNNER_IDS_ENV_VAR, " runner-a,runner-b, ")
    CWSandboxLauncher().provision("x")
    assert sdk.run_kwargs["placement_mode"] == "cks"
    assert sdk.run_kwargs["runner_ids"] == ["runner-a", "runner-b"]


@pytest.mark.parametrize("mode", ["invalid", "unspecified"])
def test_invalid_placement_fails_before_provision(sdk: _State, monkeypatch, mode):
    monkeypatch.setenv(PLACEMENT_MODE_ENV_VAR, mode)
    with pytest.raises(click.ClickException, match=PLACEMENT_MODE_ENV_VAR):
        CWSandboxLauncher().provision("x")
    assert not sdk.run_kwargs


def test_serverless_rejects_runner_pin(sdk: _State, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(RUNNER_IDS_ENV_VAR, "runner-a")
    with pytest.raises(click.ClickException, match=r"requires.*=cks"):
        CWSandboxLauncher().provision("x")
    assert not sdk.run_kwargs


def test_cks_creation_failure_suggests_checking_runner_ids(
    sdk: _State, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PLACEMENT_MODE_ENV_VAR, "CKS")
    monkeypatch.setenv(RUNNER_IDS_ENV_VAR, "missing-runner")

    def fail(*args, **kwargs):
        raise _CWSandboxError("no eligible runner; retry shortly")

    monkeypatch.setattr(_FakeSandbox, "run", fail)
    with pytest.raises(click.ClickException, match="CKS runner IDs exist and are ready"):
        CWSandboxLauncher().provision("test")


@pytest.mark.parametrize("strategy", ["wandb", "coreweave_api_key"])
def test_auth_strategy_used_for_create_and_attach(sdk, monkeypatch, strategy):
    monkeypatch.setenv(AUTH_STRATEGY_ENV_VAR, strategy)
    launcher = CWSandboxLauncher()
    launcher.prepare()
    launcher.provision("auth-test")
    assert sdk.run_kwargs["auth"] == strategy
    CWSandboxLauncher().attach("sb-existing")
    assert sdk.from_id_auth == strategy


def test_coreweave_auth_requires_its_own_key(sdk, monkeypatch):
    monkeypatch.setenv(AUTH_STRATEGY_ENV_VAR, "coreweave_api_key")
    monkeypatch.delenv("CWSANDBOX_API_KEY")
    with pytest.raises(click.ClickException, match="CWSANDBOX_API_KEY"):
        CWSandboxLauncher().prepare()


def test_invalid_auth_fails_before_provision(sdk, monkeypatch):
    monkeypatch.setenv(AUTH_STRATEGY_ENV_VAR, "invalid")
    with pytest.raises(click.ClickException, match=AUTH_STRATEGY_ENV_VAR):
        CWSandboxLauncher().provision("bad-auth")
    assert not sdk.run_kwargs
