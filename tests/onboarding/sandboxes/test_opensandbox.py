"""Tests for :mod:`omnigent.onboarding.sandboxes.opensandbox`."""

from __future__ import annotations

import logging
import shlex
import sys
import types
from dataclasses import dataclass, field
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import click
import pytest

from omnigent.onboarding.sandboxes.base import DEFAULT_HOST_IMAGE
from omnigent.onboarding.sandboxes.opensandbox import (
    API_KEY_ENV_VAR,
    DOMAIN_ENV_VAR,
    HOST_IMAGE_ENV_VAR,
    MAX_LIFETIME_ENV_VAR,
    PROTOCOL_ENV_VAR,
    READY_TIMEOUT_ENV_VAR,
    REQUEST_TIMEOUT_ENV_VAR,
    SANDBOX_ENV_PASSTHROUGH_ENV_VAR,
    SERVER_PROXY_ENV_VAR,
    SNAPSHOT_ID_ENV_VAR,
    OpenSandboxLauncher,
    managed_token_ttl_s,
)


class _NotFound(Exception):
    status_code = 404


@dataclass
class _State:
    create_kwargs: dict[str, object] = field(default_factory=dict)
    connect_kwargs: dict[str, object] = field(default_factory=dict)
    connect_error: Exception | None = None
    command: str | None = None
    run_opts: object | None = None
    exit_code: int | None = 0
    command_status_exit_code: int | None = 0
    execution_error: object | None = None
    status_queries: list[str] = field(default_factory=list)
    stdout: list[str] = field(default_factory=list)
    stderr: list[str] = field(default_factory=list)
    killed: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    status: str = "running"
    missing: bool = False
    ready_error: Exception | None = None
    preflight_error: Exception | None = None
    kill_error: Exception | None = None
    manager_kill_error: Exception | None = None
    info_error: Exception | None = None
    close_error: Exception | None = None


class _ConnectionConfig:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        for name, value in kwargs.items():
            setattr(self, name, value)


class _RunCommandOpts:
    timeout = None


class _SandboxFilter:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs


class _Commands:
    def __init__(self, state: _State) -> None:
        self._state = state

    def run(self, command: str, *, opts: object) -> object:
        self._state.run_opts = opts
        self._state.command = command
        return SimpleNamespace(
            id="cmd-1",
            exit_code=self._state.exit_code,
            error=self._state.execution_error,
            logs=SimpleNamespace(
                stdout=[SimpleNamespace(text=line) for line in self._state.stdout],
                stderr=[SimpleNamespace(text=line) for line in self._state.stderr],
            ),
        )

    def get_command_status(self, command_id: str) -> object:
        self._state.status_queries.append(command_id)
        return SimpleNamespace(exit_code=self._state.command_status_exit_code)


class _Sandbox:
    state: _State

    def __init__(self, sandbox_id: str = "sb-1") -> None:
        self.id = sandbox_id
        self.commands = _Commands(self.state)

    @classmethod
    def create(cls, image: str | None, **kwargs: object) -> _Sandbox:
        cls.state.create_kwargs = {"image": image, **kwargs}
        return cls()

    @classmethod
    def connect(cls, sandbox_id: str, **kwargs: object) -> _Sandbox:
        cls.state.connect_kwargs = kwargs
        if cls.state.connect_error is not None:
            raise cls.state.connect_error
        if cls.state.missing:
            raise _NotFound(sandbox_id)
        return cls(sandbox_id)

    def check_ready(self, *, timeout: timedelta, polling_interval: timedelta) -> None:
        del timeout, polling_interval
        if self.state.ready_error is not None:
            raise self.state.ready_error

    def kill(self) -> None:
        if self.state.kill_error is not None:
            raise self.state.kill_error
        self.state.killed.append(str(self.id))
        self.state.status = "terminated"

    def close(self) -> None:
        if self.state.close_error is not None:
            raise self.state.close_error
        self.state.closed.append(str(self.id))


class _Manager:
    state: _State

    @classmethod
    def create(cls, *, connection_config: object) -> _Manager:
        del connection_config
        return cls()

    def list_sandbox_infos(self, sandbox_filter: object) -> object:
        del sandbox_filter
        if self.state.preflight_error is not None:
            raise self.state.preflight_error
        return SimpleNamespace(items=[])

    def get_sandbox_info(self, sandbox_id: str) -> object:
        if self.state.info_error is not None:
            raise self.state.info_error
        if self.state.missing:
            raise _NotFound(sandbox_id)
        return SimpleNamespace(status=SimpleNamespace(state=self.state.status))

    def kill_sandbox(self, sandbox_id: str) -> None:
        if self.state.manager_kill_error is not None:
            raise self.state.manager_kill_error
        if self.state.missing:
            raise _NotFound(sandbox_id)
        self.state.killed.append(sandbox_id)
        self.state.status = "terminated"

    def close(self) -> None:
        if self.state.close_error is not None:
            raise self.state.close_error
        self.state.closed.append("manager")


@pytest.fixture()
def sdk(monkeypatch: pytest.MonkeyPatch) -> _State:
    state = _State()
    monkeypatch.setattr(_Sandbox, "state", state, raising=False)
    monkeypatch.setattr(_Manager, "state", state, raising=False)

    root = types.ModuleType("opensandbox")
    root.SandboxSync = _Sandbox  # type: ignore[attr-defined]
    root.SandboxManagerSync = _Manager  # type: ignore[attr-defined]
    config = types.ModuleType("opensandbox.config")
    config.ConnectionConfigSync = _ConnectionConfig  # type: ignore[attr-defined]
    models = types.ModuleType("opensandbox.models")
    execd = types.ModuleType("opensandbox.models.execd")
    execd.RunCommandOpts = _RunCommandOpts  # type: ignore[attr-defined]
    sandboxes = types.ModuleType("opensandbox.models.sandboxes")
    sandboxes.SandboxFilter = _SandboxFilter  # type: ignore[attr-defined]
    for name, module in (
        ("opensandbox", root),
        ("opensandbox.config", config),
        ("opensandbox.models", models),
        ("opensandbox.models.execd", execd),
        ("opensandbox.models.sandboxes", sandboxes),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    monkeypatch.setenv(API_KEY_ENV_VAR, "secret-key")
    monkeypatch.setenv(DOMAIN_ENV_VAR, "sandbox.example.com")
    monkeypatch.setenv(SERVER_PROXY_ENV_VAR, "true")
    for name in (
        HOST_IMAGE_ENV_VAR,
        MAX_LIFETIME_ENV_VAR,
        PROTOCOL_ENV_VAR,
        READY_TIMEOUT_ENV_VAR,
        REQUEST_TIMEOUT_ENV_VAR,
        SANDBOX_ENV_PASSTHROUGH_ENV_VAR,
        SNAPSHOT_ID_ENV_VAR,
    ):
        monkeypatch.delenv(name, raising=False)
    return state


def test_constructor_builds_connection_and_validates_image_snapshot(sdk: _State) -> None:
    launcher = OpenSandboxLauncher()
    assert launcher._image == DEFAULT_HOST_IMAGE
    kwargs = cast(Any, launcher._connection_config).kwargs
    assert kwargs["domain"] == "sandbox.example.com"
    assert kwargs["protocol"] == "http"
    assert kwargs["request_timeout"] == timedelta(seconds=30)
    assert kwargs["use_server_proxy"] is True
    with pytest.raises(click.ClickException, match="only one"):
        OpenSandboxLauncher(image="img", snapshot_id="snap")


def test_provision_from_snapshot_omits_image(sdk: _State) -> None:
    assert OpenSandboxLauncher(snapshot_id="snap-1").provision("snapshot-test") == "sb-1"
    assert sdk.create_kwargs["image"] is None
    assert sdk.create_kwargs["snapshot_id"] == "snap-1"


def test_capabilities_are_managed_only(sdk: _State) -> None:
    capabilities = OpenSandboxLauncher().capabilities
    assert capabilities.managed_launch is True
    assert capabilities.cli_bootstrap is False
    assert capabilities.programmatic_terminate is True


def test_provision_uses_image_lifetime_metadata_and_env(
    sdk: _State, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "llm-secret")
    launcher = OpenSandboxLauncher(
        image="registry.example/host:v1",
        env=["OPENAI_API_KEY"],
        max_lifetime_s=7200,
        ready_timeout_s=240,
    )
    assert launcher.provision("managed-test") == "sb-1"
    assert sdk.create_kwargs["image"] == "registry.example/host:v1"
    assert sdk.create_kwargs["timeout"] == timedelta(seconds=7200)
    assert sdk.create_kwargs["ready_timeout"] == timedelta(seconds=240)
    assert sdk.create_kwargs["env"] == {"OPENAI_API_KEY": "llm-secret"}
    assert sdk.create_kwargs["metadata"] == {
        "omnigent-name": "managed-test",
        "omnigent-provider": "opensandbox",
    }


def test_env_passthrough_rejects_missing_and_control_credentials(
    sdk: _State, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NOT_SET", raising=False)
    with pytest.raises(click.ClickException, match="NOT_SET"):
        OpenSandboxLauncher(env=["NOT_SET"]).provision("x")
    with pytest.raises(click.ClickException, match="control credential"):
        OpenSandboxLauncher(env=[API_KEY_ENV_VAR]).provision("x")


def test_readiness_failure_cleans_up_and_redacts_api_key(sdk: _State) -> None:
    sdk.ready_error = RuntimeError("backend rejected secret-key")
    with pytest.raises(click.ClickException, match=r"\[redacted\]") as exc:
        OpenSandboxLauncher().provision("x")
    assert "secret-key" not in str(exc.value)
    assert sdk.killed == ["sb-1"]
    assert "sb-1" in sdk.closed


def test_run_returns_output_and_checks_exit_status(sdk: _State) -> None:
    sdk.stdout = ["hello\n"]
    sdk.stderr = ["warning\n"]
    result = OpenSandboxLauncher().run("sb-1", "echo hello", check=False)
    assert result.returncode == 0
    assert result.stdout == "hello"
    assert result.stderr == "warning"
    assert sdk.command == "bash -lc 'echo hello'"
    assert isinstance(sdk.run_opts, _RunCommandOpts)
    assert cast(Any, sdk.run_opts).timeout is None

    quoted_command = 'printf "%s" "it\'s"'
    OpenSandboxLauncher().run("sb-1", quoted_command, check=False)
    assert sdk.command == f"bash -lc {shlex.quote(quoted_command)}"

    sdk.exit_code = 3
    with pytest.raises(click.ClickException, match="exit 3"):
        OpenSandboxLauncher().run("sb-1", "false")


def test_run_resolves_deferred_status_and_surfaces_execution_error(sdk: _State) -> None:
    sdk.exit_code = None
    result = OpenSandboxLauncher().run("sb-1", "true")
    assert result.returncode == 0
    assert sdk.status_queries == ["cmd-1"]

    sdk.command_status_exit_code = None
    sdk.execution_error = SimpleNamespace(name="RemoteError", value="command failed")
    result = OpenSandboxLauncher().run("sb-1", "false", check=False)
    assert result.returncode == 1
    assert result.stderr == "RemoteError: command failed"

    sdk.exit_code = 5
    result = OpenSandboxLauncher().run("sb-1", "false", check=False)
    assert result.returncode == 5
    assert result.stderr == "RemoteError: command failed"

    sdk.exit_code = None
    sdk.execution_error = None
    with pytest.raises(click.ClickException, match="did not report a command exit code"):
        OpenSandboxLauncher().run("sb-1", "unknown")


def test_connect_forwards_timeouts_and_redacts_injected_credentials(sdk: _State) -> None:
    config = _ConnectionConfig(api_key="injected-secret")
    launcher = OpenSandboxLauncher(connection_config=cast(Any, config), ready_timeout_s=42)
    launcher.run("external", "true")
    assert sdk.connect_kwargs["connection_config"] is config
    assert sdk.connect_kwargs["connect_timeout"] == timedelta(seconds=42)
    assert sdk.connect_kwargs["health_check_polling_interval"] == timedelta(seconds=1)

    sdk.connect_error = RuntimeError("backend rejected injected-secret")
    with pytest.raises(click.ClickException, match=r"\[redacted\]") as exc:
        launcher.run("missing", "true")
    assert "injected-secret" not in str(exc.value)


def test_env_passthrough_list_is_resolved_from_environment(
    sdk: _State, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SANDBOX_ENV_PASSTHROUGH_ENV_VAR, " OPENAI_API_KEY, GIT_TOKEN ")
    monkeypatch.setenv("OPENAI_API_KEY", "llm-secret")
    monkeypatch.setenv("GIT_TOKEN", "git-secret")
    OpenSandboxLauncher().provision("x")
    assert sdk.create_kwargs["env"] == {
        "OPENAI_API_KEY": "llm-secret",
        "GIT_TOKEN": "git-secret",
    }


@pytest.mark.parametrize(
    ("name", "value"),
    [
        (SERVER_PROXY_ENV_VAR, "maybe"),
        (READY_TIMEOUT_ENV_VAR, "not-a-number"),
    ],
)
def test_invalid_environment_configuration_fails_loudly(
    sdk: _State,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(click.ClickException, match=name):
        OpenSandboxLauncher()


def test_failed_create_cleanup_redacts_passthrough_secrets(
    sdk: _State,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sandbox-secret")
    sdk.ready_error = RuntimeError("readiness failed")
    sdk.kill_error = RuntimeError("kill echoed sandbox-secret")
    sdk.close_error = RuntimeError("close echoed sandbox-secret")

    with caplog.at_level(logging.DEBUG, logger="omnigent.onboarding.sandboxes.opensandbox"):
        with pytest.raises(click.ClickException, match="readiness failed"):
            OpenSandboxLauncher(env=["OPENAI_API_KEY"]).provision("x")

    assert caplog.records
    assert "sandbox-secret" not in caplog.text
    assert "[redacted]" in caplog.text


def test_terminate_handles_cached_uncached_and_missing(sdk: _State) -> None:
    launcher = OpenSandboxLauncher()
    launcher.provision("x")
    launcher.terminate("sb-1")
    assert sdk.killed == ["sb-1"]

    sdk.status = "running"
    launcher.terminate("sb-2")
    assert sdk.killed[-1] == "sb-2"

    sdk.missing = True
    launcher.terminate("already-gone")
    assert sdk.killed == ["sb-1", "sb-2"]


def test_terminate_and_status_redact_provider_errors(
    sdk: _State, caplog: pytest.LogCaptureFixture
) -> None:
    launcher = OpenSandboxLauncher()
    sdk.manager_kill_error = RuntimeError("backend rejected secret-key")
    sdk.info_error = RuntimeError("poll rejected secret-key")
    with pytest.raises(click.ClickException, match=r"\[redacted\]") as exc:
        launcher.terminate("sb-1")
    assert "secret-key" not in str(exc.value)

    sdk.manager_kill_error = None
    sdk.info_error = RuntimeError("status rejected secret-key")
    with caplog.at_level(logging.WARNING, logger="omnigent.onboarding.sandboxes.opensandbox"):
        assert launcher.is_running("sb-1") is None
    assert "secret-key" not in caplog.text
    assert "[redacted]" in caplog.text


def test_terminate_accepts_lost_response_when_sandbox_reached_terminal_state(
    sdk: _State,
) -> None:
    launcher = OpenSandboxLauncher()
    launcher.provision("x")
    sdk.closed.clear()
    sdk.status = "terminated"
    sdk.kill_error = RuntimeError("kill response was lost")
    launcher.terminate("sb-1")
    assert sdk.closed[-1] == "sb-1"


def test_close_drains_cached_clients(sdk: _State) -> None:
    launcher = OpenSandboxLauncher()
    launcher.provision("x")
    launcher.close()
    assert sdk.closed == ["sb-1"]
    assert launcher._clients == {}


def test_prepare_and_is_running(sdk: _State) -> None:
    launcher = OpenSandboxLauncher()
    launcher.prepare()
    assert "manager" in sdk.closed
    assert launcher.is_running("sb-1") is True
    sdk.status = "terminated"
    assert launcher.is_running("sb-1") is False
    sdk.status = "paused"
    assert launcher.is_running("sb-1") is False
    sdk.status = "pending"
    assert launcher.is_running("sb-1") is None
    sdk.status = "unknown"
    assert launcher.is_running("sb-1") is None
    sdk.missing = True
    assert launcher.is_running("sb-1") is False


def test_prepare_redacts_provider_errors(sdk: _State) -> None:
    sdk.preflight_error = RuntimeError("backend rejected secret-key")
    with pytest.raises(click.ClickException, match=r"\[redacted\]") as exc:
        OpenSandboxLauncher().prepare()
    assert "secret-key" not in str(exc.value)


def test_managed_token_ttl_tracks_lifetime(sdk: _State) -> None:
    assert managed_token_ttl_s() == 25 * 3600
    assert managed_token_ttl_s(7200) == 10_800
    assert managed_token_ttl_s(60) == 3660
    with pytest.raises(click.ClickException, match="at least 60"):
        managed_token_ttl_s(59)
