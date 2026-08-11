"""Tests for :mod:`omnigent.onboarding.sandboxes.blaxel`."""

from __future__ import annotations

import sys
import threading
import types
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import click
import httpx
import pytest

from omnigent.onboarding.sandboxes.base import SandboxCapabilityError
from omnigent.onboarding.sandboxes.blaxel import (
    DEFAULT_BLAXEL_HOST_IMAGE,
    BlaxelSandboxLauncher,
    _BlaxelLogParser,
    _BlaxelLogStream,
    _bounded_utf8_size,
    _LineSink,
    _OutputLimitExceeded,
    managed_token_ttl_s,
    parse_ttl_seconds,
)
from tests.e2e.integrations.deploy.blaxel.blaxel_smoke_test import _production_workspace_name


class _SandboxAPIError(Exception):
    """Small stand-in for the SDK provider error."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class _RetryableReadinessError(Exception):
    """Stand-in for the SDK's structured workload-unavailable response."""

    def __init__(self) -> None:
        super().__init__("workload unavailable")
        self.data = {
            "error": {
                "code": "WORKLOAD_UNAVAILABLE",
                "retryable": True,
            }
        }
        self.response = types.SimpleNamespace(status_code=404)


@dataclass
class _ProcessResponse:
    """Process state returned by the fake SDK."""

    pid: str = "42"
    name: str = "command"
    status: str = "running"
    stdout: str = ""
    stderr: str = ""
    logs: str = ""
    exit_code: int | None = None


class _FakeLogStream:
    """Completed observable log stream returned by the fake opener."""

    def __init__(
        self,
        error: Exception | None = None,
        *,
        done: bool = True,
        wait_error: BaseException | None = None,
        error_cleanup_attempted: bool = False,
        error_cleanup_failure: str | None = None,
    ) -> None:
        self.error = error
        self.done = done
        self.wait_error = wait_error
        self.error_cleanup_attempted = error_cleanup_attempted
        self.error_cleanup_failure = error_cleanup_failure
        self.close_calls = 0

    def wait(self, timeout: float | None = None) -> bool:
        del timeout
        if self.wait_error is not None:
            raise self.wait_error
        return self.done

    def close(self) -> None:
        if self.close_calls:
            return
        self.close_calls += 1
        self.done = True


class _FakeProcess:
    """Record process API calls and return canned states."""

    def __init__(self) -> None:
        self.exec_calls: list[dict[str, object]] = []
        self.exec_response = _ProcessResponse()
        self.get_responses: list[_ProcessResponse] = [
            _ProcessResponse(status="completed", stdout="ok\n", exit_code=0)
        ]
        self.killed: list[str] = []
        self.kill_calls = 0
        self.exec_error: Exception | None = None
        self.kill_error: Exception | None = None
        self.get_error: Exception | None = None
        self.get_calls = 0
        self.stream_events: list[tuple[str, str]] | None = None
        self.stream_error: Exception | None = None
        self.stream_done = True
        self.stream_wait_error: BaseException | None = None
        self.stream_handle: _FakeLogStream | None = None

    def exec(self, request: dict[str, object]) -> _ProcessResponse:
        self.exec_calls.append(request)
        if self.exec_error is not None:
            raise self.exec_error
        return self.exec_response

    def get(self, identifier: str) -> _ProcessResponse:
        del identifier
        self.get_calls += 1
        if self.get_error is not None:
            raise self.get_error
        if self.get_responses:
            return self.get_responses.pop(0)
        return _ProcessResponse(status="completed", exit_code=0)

    def kill(self, identifier: str) -> None:
        self.kill_calls += 1
        if self.kill_error is not None:
            raise self.kill_error
        self.killed.append(identifier)


class _FakeFS:
    """Record filesystem calls."""

    def __init__(self) -> None:
        self.listed: list[str] = []
        self.writes: list[tuple[str, bytes]] = []
        self.ls_error: Exception | None = None
        self.ls_errors: list[Exception] = []

    def ls(self, path: str) -> list[object]:
        self.listed.append(path)
        if self.ls_errors:
            raise self.ls_errors.pop(0)
        if self.ls_error is not None:
            raise self.ls_error
        return []

    def write_binary(self, path: str, content: bytes) -> None:
        self.writes.append((path, content))


class _FakeSandbox:
    """Recording sandbox handle."""

    def __init__(self, name: str, *, labels: dict[str, str] | None = None) -> None:
        self.metadata = types.SimpleNamespace(name=name, labels=labels or {})
        self.status = "deployed"
        self.process = _FakeProcess()
        self.fs = _FakeFS()
        self.delete_calls = 0
        self.delete_error: Exception | None = None

    def delete(self) -> None:
        if self.delete_error is not None:
            raise self.delete_error
        self.delete_calls += 1


@dataclass
class _State:
    """Shared fake SDK state."""

    workspace: str | None = "test-workspace"
    api_key: str | None = "fake-key"
    created: list[dict[str, object]] = field(default_factory=list)
    sandboxes: dict[str, _FakeSandbox] = field(default_factory=dict)
    deleted: list[str] = field(default_factory=list)
    create_error: Exception | None = None
    create_committed_error: Exception | None = None
    get_error: Exception | None = None
    delete_error: Exception | None = None
    terminate_before_delete: bool = False


def _install_fake_blaxel(monkeypatch: pytest.MonkeyPatch) -> _State:
    """Install a strict fake SDK through its public module paths."""
    state = _State()

    class _SyncSandboxInstance:
        @classmethod
        def create(cls, payload: dict[str, object]) -> _FakeSandbox:
            if state.create_error is not None:
                raise state.create_error
            state.created.append(payload)
            name = str(payload["name"])
            raw_labels = payload.get("labels")
            assert isinstance(raw_labels, dict)
            labels = {str(key): str(value) for key, value in raw_labels.items()}
            handle = state.sandboxes.setdefault(name, _FakeSandbox(name, labels=labels))
            if state.create_committed_error is not None:
                raise state.create_committed_error
            return handle

        @classmethod
        def get(cls, name: str) -> _FakeSandbox:
            if state.get_error is not None:
                raise state.get_error
            if name not in state.sandboxes:
                raise _SandboxAPIError("missing", 404)
            return state.sandboxes[name]

        @classmethod
        def delete(cls, name: str) -> _FakeSandbox:
            if state.delete_error is not None:
                raise state.delete_error
            if name not in state.sandboxes:
                raise _SandboxAPIError("missing", 404)
            state.deleted.append(name)
            sandbox = state.sandboxes[name]
            if state.terminate_before_delete and state.deleted.count(name) == 1:
                sandbox.status = "terminated"
                return sandbox
            del state.sandboxes[name]
            return sandbox

    blaxel = types.ModuleType("blaxel")
    core = types.ModuleType("blaxel.core")
    authentication = types.ModuleType("blaxel.core.authentication")
    sandbox = types.ModuleType("blaxel.core.sandbox")
    core.SyncSandboxInstance = _SyncSandboxInstance  # type: ignore[attr-defined]
    authentication.get_credentials = lambda: types.SimpleNamespace(  # type: ignore[attr-defined]
        workspace=state.workspace,
        api_key=state.api_key,
        client_credentials=None,
        device_code=None,
    )
    sandbox.SandboxAPIError = _SandboxAPIError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "blaxel", blaxel)
    monkeypatch.setitem(sys.modules, "blaxel.core", core)
    monkeypatch.setitem(sys.modules, "blaxel.core.authentication", authentication)
    monkeypatch.setitem(sys.modules, "blaxel.core.sandbox", sandbox)

    def _open_fake_stream(
        process_api: _FakeProcess,
        identifier: str,
        on_stdout: object,
        on_stderr: object,
        *,
        max_output_bytes: int,
        on_error: object = None,
    ) -> _FakeLogStream:
        del identifier, max_output_bytes
        assert callable(on_stdout)
        assert callable(on_stderr)
        events = process_api.stream_events
        if events is None and process_api.get_responses:
            final = process_api.get_responses[0]
            events = []
            if final.stdout:
                events.append(("stdout", final.stdout))
            if final.stderr:
                events.append(("stderr", final.stderr))
            if not final.stdout and not final.stderr and final.logs:
                events.append(("stdout", final.logs))
        for kind, content in events or []:
            callback = on_stderr if kind == "stderr" else on_stdout
            callback(content)
        cleanup_attempted = False
        cleanup_result: object = None
        if process_api.stream_error is not None and callable(on_error):
            cleanup_attempted = True
            cleanup_result = on_error(process_api.stream_error)
        handle = _FakeLogStream(
            process_api.stream_error,
            done=process_api.stream_done,
            wait_error=process_api.stream_wait_error,
            error_cleanup_attempted=cleanup_attempted,
            error_cleanup_failure=str(cleanup_result) if cleanup_result else None,
        )
        process_api.stream_handle = handle
        return handle

    monkeypatch.setattr(
        "omnigent.onboarding.sandboxes.blaxel._open_process_log_stream",
        _open_fake_stream,
    )
    monkeypatch.setattr("omnigent.onboarding.sandboxes.blaxel._READINESS_INITIAL_BACKOFF_S", 0)
    monkeypatch.setattr("omnigent.onboarding.sandboxes.blaxel._PROCESS_POLL_INTERVAL_S", 0)
    return state


def test_capabilities_match_exec_provider_contract() -> None:
    launcher = BlaxelSandboxLauncher(image="image")

    assert launcher.capabilities.cli_bootstrap is True
    assert launcher.capabilities.managed_launch is True
    assert launcher.capabilities.local_port_forward is False
    assert launcher.capabilities.programmatic_terminate is True
    assert launcher.capabilities.file_copy is True
    assert launcher.capabilities.streaming_exec is True
    assert launcher.capabilities.foreground_exec is True

    with pytest.raises(SandboxCapabilityError, match="cannot forward a local port"):
        launcher.forward_local_port("sb", 8022)
    assert "--force-reinstall" in launcher.wheel_install_command("/tmp/wheels.tgz")


@pytest.mark.parametrize(
    "workspace",
    ["main", "main-dev", "prod", "prod-us", "customer-prod", "production-test"],
)
def test_live_smoke_rejects_production_workspace_names(workspace: str) -> None:
    assert _production_workspace_name(workspace) is True


def test_live_smoke_accepts_clear_test_workspace_name() -> None:
    assert _production_workspace_name("calibrator") is False


def test_prepare_reports_missing_optional_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "blaxel", None)

    with pytest.raises(click.ClickException, match=r"omnigent\[blaxel\]"):
        BlaxelSandboxLauncher(image="image").prepare()


def test_prepare_rejects_missing_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_fake_blaxel(monkeypatch)
    state.workspace = None

    with pytest.raises(click.ClickException, match="complete Blaxel credentials"):
        BlaxelSandboxLauncher(image="image").prepare()


def test_prepare_rejects_workspace_without_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_fake_blaxel(monkeypatch)
    state.api_key = None

    with pytest.raises(click.ClickException, match="complete Blaxel credentials"):
        BlaxelSandboxLauncher(image="image").prepare()


def test_prepare_uses_public_compatible_image(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_blaxel(monkeypatch)
    monkeypatch.delenv("OMNIGENT_BLAXEL_HOST_IMAGE", raising=False)
    launcher = BlaxelSandboxLauncher()

    launcher.prepare()

    assert launcher._resolve_image() == DEFAULT_BLAXEL_HOST_IMAGE


def test_image_precedence_reaches_create_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_fake_blaxel(monkeypatch)
    monkeypatch.setenv("OMNIGENT_BLAXEL_HOST_IMAGE", "sandbox/environment-host:v1")

    BlaxelSandboxLauncher(image="sandbox/explicit-host:v2").provision("explicit")
    BlaxelSandboxLauncher().provision("environment")

    assert state.created[0]["image"] == "sandbox/explicit-host:v2"
    assert state.created[1]["image"] == "sandbox/environment-host:v1"


def test_provision_forwards_config_and_checks_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_fake_blaxel(monkeypatch)
    monkeypatch.setenv("MODEL_KEY", "secret-value")
    launcher = BlaxelSandboxLauncher(
        image="registry/image",
        env=["MODEL_KEY"],
        region="us-test-1",
        memory_mb=8192,
        ttl="2h",
    )

    assert launcher.provision("omni-test") == "omni-test"
    assert len(state.created) == 1
    created = state.created[0]
    assert created | {"labels": {"managed-by": "omnigent"}} == {
        "name": "omni-test",
        "image": "registry/image",
        "memory": 8192,
        "labels": {"managed-by": "omnigent"},
        "envs": [{"name": "MODEL_KEY", "value": "secret-value"}],
        "region": "us-test-1",
        "ttl": "2h",
    }
    labels = created["labels"]
    assert isinstance(labels, dict)
    assert labels["managed-by"] == "omnigent"
    assert len(str(labels["omnigent-create-attempt"])) == 32
    assert state.sandboxes["omni-test"].fs.listed == ["/"]


def test_provision_retries_provider_readiness_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_fake_blaxel(monkeypatch)
    sandbox = _FakeSandbox("sb")
    sandbox.fs.ls_errors = [_RetryableReadinessError(), _RetryableReadinessError()]
    state.sandboxes["sb"] = sandbox

    assert BlaxelSandboxLauncher(image="image").provision("sb") == "sb"

    assert sandbox.fs.listed == ["/", "/", "/"]
    assert sandbox.delete_calls == 0


def test_provision_applies_public_image_and_bounded_default_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_fake_blaxel(monkeypatch)
    monkeypatch.delenv("OMNIGENT_BLAXEL_HOST_IMAGE", raising=False)

    BlaxelSandboxLauncher().provision("sb")

    assert state.created[0]["image"] == DEFAULT_BLAXEL_HOST_IMAGE
    assert state.created[0]["ttl"] == "24h"
    assert parse_ttl_seconds(state.created[0]["ttl"]) == 24 * 3600


@pytest.mark.parametrize(
    ("ttl", "expected"),
    [
        (None, 24 * 3600),
        ("30m", 1800),
        ("24h", 24 * 3600),
        ("7d", 7 * 24 * 3600),
        ("2w", 14 * 24 * 3600),
        ("1h30m", 5400),
        (" 12H ", 12 * 3600),
        ("1.5h", 5400),
        ("90s", 90),
    ],
)
def test_parse_ttl_seconds_reads_blaxel_durations(ttl: str | None, expected: int) -> None:
    """Every unit the Blaxel runtime documents for ``ttl`` (w, d, h, m, s)."""
    assert parse_ttl_seconds(ttl) == expected


@pytest.mark.parametrize("ttl", ["", "forever", "24", "h", "24hh", "1y", "-1h", "0h"])
def test_parse_ttl_seconds_rejects_unusable_durations(ttl: str) -> None:
    with pytest.raises(ValueError):
        parse_ttl_seconds(ttl)


def test_managed_token_ttl_outlives_the_sandbox_it_authenticates() -> None:
    """
    Blaxel reaps a sandbox at its max age, so the launch token is minted for
    that age plus a reconnect margin, never the reverse, which would drop a
    live managed session's host mid-session.
    """
    for ttl in (None, "30m", "24h", "7d"):
        assert managed_token_ttl_s(ttl) == parse_ttl_seconds(ttl) + 3600
    assert managed_token_ttl_s("7d") > managed_token_ttl_s()


def test_provision_cleans_name_after_ambiguous_create_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_fake_blaxel(monkeypatch)
    state.terminate_before_delete = True
    state.create_committed_error = TimeoutError("response lost after create")

    with pytest.raises(click.ClickException, match="response lost after create"):
        BlaxelSandboxLauncher(image="image").provision("sb")

    assert state.deleted == ["sb", "sb"]
    assert "sb" not in state.sandboxes


def test_provision_rejects_unset_environment_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_blaxel(monkeypatch)
    monkeypatch.delenv("MISSING_KEY", raising=False)

    with pytest.raises(click.ClickException, match="MISSING_KEY"):
        BlaxelSandboxLauncher(image="image", env=["MISSING_KEY"]).provision("sb")


@pytest.mark.parametrize("name", ["BL_API_KEY", "BL_CLIENT_CREDENTIALS"])
def test_provision_rejects_control_credential_passthrough(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    _install_fake_blaxel(monkeypatch)
    monkeypatch.setenv(name, "control-secret")

    with pytest.raises(click.ClickException, match="control credential"):
        BlaxelSandboxLauncher(image="image", env=[name]).provision("sb")


def test_provision_redacts_workload_secret_from_sdk_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_fake_blaxel(monkeypatch)
    state.create_error = RuntimeError("provider rejected workload-secret")
    monkeypatch.setenv("MODEL_KEY", "workload-secret")

    with pytest.raises(click.ClickException) as exc_info:
        BlaxelSandboxLauncher(image="image", env=["MODEL_KEY"]).provision("sb")
    assert "workload-secret" not in str(exc_info.value)
    assert "[redacted]" in str(exc_info.value)


def test_ambiguous_create_does_not_delete_preexisting_fixed_cli_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_fake_blaxel(monkeypatch)
    name = "omnigent-host"
    state.sandboxes[name] = _FakeSandbox(
        name,
        labels={"managed-by": "omnigent", "omnigent-create-attempt": "older-attempt"},
    )
    state.create_error = TimeoutError("request outcome unknown")

    with pytest.raises(click.ClickException, match="outcome unknown"):
        BlaxelSandboxLauncher(image="image").provision(name)

    assert state.deleted == []
    assert state.sandboxes[name].metadata.labels["omnigent-create-attempt"] == "older-attempt"


def test_provision_does_not_delete_namesake_after_explicit_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_fake_blaxel(monkeypatch)
    state.sandboxes["sb"] = _FakeSandbox("sb")
    state.create_error = _SandboxAPIError("already exists", 409)

    with pytest.raises(click.ClickException, match="already exists"):
        BlaxelSandboxLauncher(image="image").provision("sb")

    assert state.deleted == []
    assert "sb" in state.sandboxes


def test_provision_deletes_handle_after_readiness_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_fake_blaxel(monkeypatch)
    state.terminate_before_delete = True
    sandbox = _FakeSandbox("sb")
    sandbox.fs.ls_error = RuntimeError("sandbox API unavailable")
    state.sandboxes["sb"] = sandbox

    with pytest.raises(click.ClickException, match="creation failed"):
        BlaxelSandboxLauncher(image="image").provision("sb")
    assert state.deleted == ["sb", "sb"]
    assert "sb" not in state.sandboxes


def test_provision_reports_readiness_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_fake_blaxel(monkeypatch)
    sandbox = _FakeSandbox("sb")
    sandbox.fs.ls_error = RuntimeError("sandbox API unavailable")
    state.delete_error = RuntimeError("delete unavailable")
    state.sandboxes["sb"] = sandbox

    with pytest.raises(click.ClickException, match="Cleanup also failed for sandbox 'sb'"):
        BlaxelSandboxLauncher(image="image").provision("sb")


def test_run_returns_separate_output_and_checks_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_fake_blaxel(monkeypatch)
    sandbox = _FakeSandbox("sb")
    sandbox.process.get_responses = [
        _ProcessResponse(
            status="failed",
            stdout="out first\nout second\n",
            stderr="err first\nerr second\n",
            exit_code=7,
        )
    ]
    state.sandboxes["sb"] = sandbox
    launcher = BlaxelSandboxLauncher(image="image")

    result = launcher.run("sb", "false", check=False)
    assert (result.returncode, result.stdout, result.stderr) == (
        7,
        "out first\nout second\n",
        "err first\nerr second\n",
    )
    assert sandbox.process.exec_calls[0]["timeout"] == 15 * 60
    sandbox.process.get_responses = [_ProcessResponse(status="failed", stderr="err", exit_code=7)]
    with pytest.raises(click.ClickException, match="exit 7"):
        launcher.run("sb", "false")


def test_bounded_utf8_counter_stops_at_limit_plus_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("omnigent.onboarding.sandboxes.blaxel._UTF8_COUNT_CHARS", 4)

    assert _bounded_utf8_size(("ab€", "cd"), 8) == 7
    assert _bounded_utf8_size(("€" * 100_000,), 10) == 11


def test_run_applies_output_cap_to_authoritative_final_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_fake_blaxel(monkeypatch)
    sandbox = _FakeSandbox("sb")
    sandbox.process.get_responses = [
        _ProcessResponse(status="completed", stdout="123456", stderr="78901", exit_code=0)
    ]
    state.sandboxes["sb"] = sandbox
    monkeypatch.setattr("omnigent.onboarding.sandboxes.blaxel._MAX_PROCESS_OUTPUT_BYTES", 10)

    with pytest.raises(click.ClickException, match="output exceeded 10 bytes"):
        BlaxelSandboxLauncher(image="image").run("sb", "echo too-much")


def test_run_redacts_control_secret_from_sdk_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_fake_blaxel(monkeypatch)
    sandbox = _FakeSandbox("sb")
    sandbox.process.exec_error = RuntimeError("bad top-secret")
    state.sandboxes["sb"] = sandbox
    monkeypatch.setenv("BL_API_KEY", "top-secret")

    with pytest.raises(click.ClickException) as exc_info:
        BlaxelSandboxLauncher(image="image").run("sb", "echo safe")
    assert "top-secret" not in str(exc_info.value)
    assert "[redacted]" in str(exc_info.value)


def test_run_timeout_kills_remote_process(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_fake_blaxel(monkeypatch)
    sandbox = _FakeSandbox("sb")
    sandbox.process.get_responses = [_ProcessResponse(status="running")]
    sandbox.process.stream_done = False
    state.sandboxes["sb"] = sandbox
    ticks = iter([0.0, 2.0])
    monkeypatch.setattr("omnigent.onboarding.sandboxes.blaxel.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("omnigent.onboarding.sandboxes.blaxel._COMMAND_TIMEOUT_S", 1)

    with pytest.raises(click.ClickException, match="timed out"):
        BlaxelSandboxLauncher(image="image").run("sb", "sleep 20")
    assert sandbox.process.killed == ["42"]


def test_run_timeout_reports_remote_kill_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_fake_blaxel(monkeypatch)
    sandbox = _FakeSandbox("sb")
    sandbox.process.get_responses = [_ProcessResponse(status="running")]
    sandbox.process.stream_done = False
    sandbox.process.kill_error = RuntimeError("kill unavailable")
    state.sandboxes["sb"] = sandbox
    ticks = iter([0.0, 2.0])
    monkeypatch.setattr("omnigent.onboarding.sandboxes.blaxel.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("omnigent.onboarding.sandboxes.blaxel._COMMAND_TIMEOUT_S", 1)

    with pytest.raises(click.ClickException, match="Remote kill also failed: kill unavailable"):
        BlaxelSandboxLauncher(image="image").run("sb", "sleep 20")


def test_process_state_failure_kills_remote_process(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_fake_blaxel(monkeypatch)
    sandbox = _FakeSandbox("sb")
    sandbox.process.get_error = RuntimeError("state unavailable")
    state.sandboxes["sb"] = sandbox

    with pytest.raises(click.ClickException, match="remote process was killed"):
        BlaxelSandboxLauncher(image="image").run("sb", "sleep 20")
    assert sandbox.process.killed == ["42"]


def test_background_launch_keeps_token_out_of_command_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_fake_blaxel(monkeypatch)
    sandbox = _FakeSandbox("sb")
    state.sandboxes["sb"] = sandbox

    BlaxelSandboxLauncher(image="image").run_background(
        "sb",
        "OMNIGENT_HOST_TOKEN=token-value OMNIGENT_HOST_ID=host-1 omnigent host",
    )

    request = sandbox.process.exec_calls[0]
    assert request["env"] == {
        "OMNIGENT_HOST_TOKEN": "token-value",
        "OMNIGENT_HOST_ID": "host-1",
    }
    assert "token-value" not in str(request["command"])
    assert "omnigent host" in str(request["command"])
    assert "while :; do" in str(request["command"]), "host crashes must be supervised"
    assert request["keep_alive"] is True
    assert request["timeout"] == 0


def test_background_launch_preserves_shell_operators(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_fake_blaxel(monkeypatch)
    sandbox = _FakeSandbox("sb")
    state.sandboxes["sb"] = sandbox

    BlaxelSandboxLauncher(image="image").run_background(
        "sb",
        "OMNIGENT_HOST_TOKEN='token with space' first && second > output",
    )

    request = sandbox.process.exec_calls[0]
    assert request["env"] == {"OMNIGENT_HOST_TOKEN": "token with space"}
    assert "first && second > output" in str(request["command"])


def test_background_launch_rejects_immediate_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_fake_blaxel(monkeypatch)
    sandbox = _FakeSandbox("sb")
    sandbox.process.exec_response = _ProcessResponse(status="completed", exit_code=0)
    state.sandboxes["sb"] = sandbox

    with pytest.raises(click.ClickException, match="failed to start"):
        BlaxelSandboxLauncher(image="image").run_background("sb", "omnigent host")


def test_file_copy_and_idempotent_termination(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = _install_fake_blaxel(monkeypatch)
    sandbox = _FakeSandbox("sb")
    state.sandboxes["sb"] = sandbox
    source = tmp_path / "payload.bin"
    source.write_bytes(b"\x00payload")
    launcher = BlaxelSandboxLauncher(image="image")

    launcher.put("sb", source, "/tmp/payload.bin")
    launcher.terminate("sb")
    launcher.terminate("sb")

    assert sandbox.fs.writes == [("/tmp/payload.bin", b"\x00payload")]
    assert state.deleted == ["sb"]


def test_termination_purges_provider_tombstone(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_fake_blaxel(monkeypatch)
    state.terminate_before_delete = True
    state.sandboxes["sb"] = _FakeSandbox("sb")

    BlaxelSandboxLauncher(image="image").terminate("sb")

    assert state.deleted == ["sb", "sb"]
    assert "sb" not in state.sandboxes


def test_foreground_cancellation_kills_process(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_fake_blaxel(monkeypatch)
    sandbox = _FakeSandbox("sb")
    state.sandboxes["sb"] = sandbox
    launcher = BlaxelSandboxLauncher(image="image")
    sandbox.process.stream_done = False
    sandbox.process.stream_wait_error = KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        launcher.exec_foreground("sb", "sleep 20")
    assert sandbox.process.killed == ["42"]
    assert sandbox.process.stream_handle is not None
    assert sandbox.process.stream_handle.close_calls == 1


def test_foreground_uses_incremental_stream_and_closes_it(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = _install_fake_blaxel(monkeypatch)
    sandbox = _FakeSandbox("sb")
    state.sandboxes["sb"] = sandbox
    sandbox.process.stream_events = [("stdout", "streamed output\n")]

    assert BlaxelSandboxLauncher(image="image").exec_foreground("sb", "printf live") == 0
    assert "streamed output" in capsys.readouterr().out
    assert sandbox.process.stream_handle is not None
    assert sandbox.process.stream_handle.close_calls == 1


def test_attach_and_running_state_use_remote_record(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_fake_blaxel(monkeypatch)
    sandbox = _FakeSandbox("sb")
    state.sandboxes["sb"] = sandbox
    launcher = BlaxelSandboxLauncher(image="image")

    launcher.attach("sb")
    assert launcher.is_running("sb") is True
    sandbox.status = "deactivated"
    assert launcher.is_running("sb") is False
    sandbox.status = "deploying"
    assert launcher.is_running("sb") is None
    sandbox.status = "unknown"
    assert launcher.is_running("sb") is None
    assert launcher.is_running("missing") is False


def test_running_state_does_not_hide_provider_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_fake_blaxel(monkeypatch)
    state.get_error = _SandboxAPIError("service unavailable", 503)

    with pytest.raises(click.ClickException, match="service unavailable"):
        BlaxelSandboxLauncher(image="image").is_running("sb")


def _streaming_launcher(monkeypatch: pytest.MonkeyPatch) -> tuple[BlaxelSandboxLauncher, _State]:
    """Provision a sandbox whose process API streams output through callbacks."""
    state = _install_fake_blaxel(monkeypatch)
    monkeypatch.setattr("omnigent.onboarding.sandboxes.blaxel._STREAM_POLL_INTERVAL_S", 0)
    launcher = BlaxelSandboxLauncher(image="image")
    launcher.provision("sb")
    state.sandboxes["sb"].process.stream_events = [("stdout", "streamed output\n")]
    return launcher, state


def test_stream_exec_yields_lines_and_keeps_one_iterator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher, state = _streaming_launcher(monkeypatch)

    process = launcher.stream_exec("sb", "bl auth login")

    assert process.lines is process.lines, "repeated access must resume the same stream"
    assert list(process.lines) == ["streamed output\n"]

    request = state.sandboxes["sb"].process.exec_calls[-1]
    assert request["keep_alive"] is True, "an OAuth login outlives the default exec cap"
    assert str(request["name"]).startswith("omni-stream-")
    assert "bl auth login" in str(request["command"])
    assert "on_stdout" not in request
    assert "on_stderr" not in request
    assert state.sandboxes["sb"].process.get_calls == 1


def test_stream_exec_terminates_each_callback_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher, state = _streaming_launcher(monkeypatch)
    process_api = state.sandboxes["sb"].process

    process_api.stream_events = [
        ("stdout", "first line\n"),
        ("stdout", "second line\n"),
    ]

    process = launcher.stream_exec("sb", "echo hi")

    assert list(process.lines) == ["first line\n", "second line\n"]


def test_stream_exec_wait_returns_exit_code_and_close_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher, state = _streaming_launcher(monkeypatch)
    process_api = state.sandboxes["sb"].process
    process_api.get_responses = [_ProcessResponse(status="completed", exit_code=3)]
    process_api.stream_events = [
        ("stdout", "first line\n"),
        ("stderr", "second line\n"),
    ]

    process = launcher.stream_exec("sb", "exit 3")

    assert process.wait() == 3
    assert process.wait() == 3, "the exit code is reported without re-polling"
    assert list(process.lines) == ["first line\n", "second line\n"]

    process.close()
    process.close()
    assert process_api.stream_handle is not None
    assert process_api.stream_handle.close_calls == 1
    assert process_api.get_calls == 1
    # A process that already reached a terminal status must not be killed.
    assert process_api.killed == []


def test_stream_exec_close_kills_a_running_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher, state = _streaming_launcher(monkeypatch)
    process_api = state.sandboxes["sb"].process
    process_api.get_responses = [_ProcessResponse(status="running")]

    process = launcher.stream_exec("sb", "sleep 600")
    process.close()

    assert process_api.killed, "an unfinished stream must be torn down"


def test_stream_state_failure_surfaces_and_close_still_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher, state = _streaming_launcher(monkeypatch)
    process_api = state.sandboxes["sb"].process
    process_api.get_error = RuntimeError("state unavailable")

    process = launcher.stream_exec("sb", "sleep 600")
    with pytest.raises(click.ClickException, match="Could not read Blaxel process state"):
        list(process.lines)

    process.close()
    assert process_api.killed == ["42"]
    assert process_api.stream_handle is not None
    assert process_api.stream_handle.close_calls >= 1


def test_stream_reader_failure_is_observable_and_kills_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher, state = _streaming_launcher(monkeypatch)
    process_api = state.sandboxes["sb"].process
    process_api.stream_error = RuntimeError("log socket failed")

    process = launcher.stream_exec("sb", "sleep 600")

    with pytest.raises(click.ClickException, match="log socket failed"):
        list(process.lines)
    assert process_api.killed == ["42"]
    assert process_api.kill_calls == 1

    process.close()
    assert process_api.kill_calls == 1, "close must not repeat a successful reader kill"


def test_close_retries_reader_kill_only_after_failed_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher, state = _streaming_launcher(monkeypatch)
    process_api = state.sandboxes["sb"].process
    process_api.stream_error = RuntimeError("log socket failed")
    process_api.kill_error = RuntimeError("kill unavailable")

    process = launcher.stream_exec("sb", "sleep 600")
    with pytest.raises(click.ClickException, match="Remote kill also failed"):
        list(process.lines)
    assert process_api.kill_calls == 1

    process_api.kill_error = None
    process.close()
    assert process_api.kill_calls == 2
    assert process_api.killed == ["42"]


def test_log_parser_handles_newline_dense_chunk_incrementally() -> None:
    stdout: list[str] = []
    parser = _BlaxelLogParser(stdout.append, stdout.append)

    parser.feed(b"stdout:first\n" + (b"x\n" * 10_000))
    parser.finish()

    assert len(stdout) == 10_001
    assert stdout[0] == "first\n"
    assert stdout[-1] == "x\n"


def test_log_parser_preserves_multiline_and_interleaved_stream_kinds() -> None:
    stdout: list[str] = []
    stderr: list[str] = []
    parser = _BlaxelLogParser(stdout.append, stderr.append)

    parser.feed(b"stderr:first error\ncontinued")
    parser.feed(b" error\nstdout:first out\ncontinued out\nstderr:last")
    parser.finish()

    assert "".join(stdout) == "first out\ncontinued out\n"
    assert "".join(stderr) == "first error\ncontinued error\nlast"


def test_log_parser_chunks_long_unterminated_output() -> None:
    chunks: list[str] = []
    parser = _BlaxelLogParser(
        chunks.append,
        chunks.append,
        max_line_bytes=16,
        max_total_bytes=1024,
    )

    parser.feed(b"stdout:" + b"x" * 100)
    parser.finish()

    assert "".join(chunks) == "x" * 100
    assert max(len(chunk.encode()) for chunk in chunks) <= 16


def test_log_parser_preserves_utf8_across_forced_chunks() -> None:
    chunks: list[str] = []
    parser = _BlaxelLogParser(
        chunks.append,
        chunks.append,
        max_line_bytes=8,
        max_total_bytes=128,
    )

    parser.feed(b"stdout:" + "€".encode() + b"\n")
    parser.finish()

    assert "".join(chunks) == "€\n"


def test_log_parser_enforces_total_output_limit() -> None:
    parser = _BlaxelLogParser(
        lambda _text: None,
        lambda _text: None,
        max_line_bytes=8,
        max_total_bytes=10,
    )

    with pytest.raises(_OutputLimitExceeded, match="exceeded 10 bytes"):
        parser.feed(b"stdout:" + b"x" * 32)


def test_log_stream_exposes_parser_failure_from_reader_thread() -> None:
    class _NoisyResponse:
        def __enter__(self) -> _NoisyResponse:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def iter_bytes(self, *, chunk_size: int) -> Iterator[bytes]:
            del chunk_size
            yield b"stdout:" + b"x" * 64

        def close(self) -> None:
            pass

    class _NoisyClient:
        def __init__(self) -> None:
            self.timeout = httpx.Timeout(5)

        def __enter__(self) -> _NoisyClient:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def stream(self, method: str, path: str) -> _NoisyResponse:
            del method, path
            return _NoisyResponse()

        def close(self) -> None:
            pass

    class _ProcessAPI:
        def get_client(self) -> _NoisyClient:
            return _NoisyClient()

        def handle_response_error(self, response: object) -> None:
            del response

    stream = _BlaxelLogStream(
        _ProcessAPI(),
        "42",
        lambda _text: None,
        lambda _text: None,
        max_output_bytes=10,
    )

    assert stream.wait(timeout=1)
    assert isinstance(stream.error, _OutputLimitExceeded)
    stream.close()


def test_log_stream_close_before_client_activation_opens_no_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_started = threading.Event()
    release_get = threading.Event()

    class _Client:
        def __init__(self) -> None:
            self.timeout = httpx.Timeout(5)
            self.closed = False
            self.stream_calls = 0

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def stream(self, method: str, path: str) -> object:
            del method, path
            self.stream_calls += 1
            raise AssertionError("a preactivation close must not open a request")

        def close(self) -> None:
            self.closed = True

    class _ProcessAPI:
        def __init__(self) -> None:
            self.client = _Client()

        def get_client(self) -> _Client:
            get_started.set()
            assert release_get.wait(timeout=5)
            return self.client

    process_api = _ProcessAPI()
    monkeypatch.setattr("omnigent.onboarding.sandboxes.blaxel._STREAM_CLOSE_TIMEOUT_S", 0.01)
    stream = _BlaxelLogStream(process_api, "42", lambda _text: None, lambda _text: None)
    assert get_started.wait(timeout=1)

    stream.close()
    release_get.set()

    assert stream.wait(timeout=1)
    assert process_api.client.closed is True
    assert process_api.client.stream_calls == 0
    assert not stream._thread.is_alive()


def test_stalled_tiny_line_consumer_fails_and_kills_from_reader_thread() -> None:
    sink = _LineSink(max_bytes=16)

    class _Response:
        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def iter_bytes(self, *, chunk_size: int) -> Iterator[bytes]:
            del chunk_size
            yield b"stdout:x\n" * 100

        def close(self) -> None:
            pass

    class _Client:
        def __init__(self) -> None:
            self.timeout = httpx.Timeout(5)

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def stream(self, method: str, path: str) -> _Response:
            del method, path
            return _Response()

        def close(self) -> None:
            pass

    class _ProcessAPI:
        def __init__(self) -> None:
            self.killed: list[str] = []

        def get_client(self) -> _Client:
            return _Client()

        def handle_response_error(self, response: object) -> None:
            del response

        def kill(self, identifier: str) -> None:
            self.killed.append(identifier)

    process_api = _ProcessAPI()
    stream = _BlaxelLogStream(
        process_api,
        "42",
        sink.feed_chunk,
        sink.feed_chunk,
        max_output_bytes=1024,
        on_error=lambda _exc: process_api.kill("42"),
    )

    assert stream.wait(timeout=1)
    assert isinstance(stream.error, _OutputLimitExceeded)
    assert process_api.killed == ["42"]
    stream.close()


def test_log_stream_disables_read_timeout() -> None:
    received: list[str] = []

    class _SlowResponse:
        def __enter__(self) -> _SlowResponse:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def iter_bytes(self, *, chunk_size: int) -> Iterator[bytes]:
            assert chunk_size == 8 * 1024
            yield b"stdout:after-idle\n"

        def close(self) -> None:
            pass

    class _SlowClient:
        def __init__(self) -> None:
            self.timeout = httpx.Timeout(5)
            self.response = _SlowResponse()

        def __enter__(self) -> _SlowClient:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def stream(self, method: str, path: str) -> _SlowResponse:
            assert method == "GET"
            assert path.endswith("/logs/stream")
            return self.response

        def close(self) -> None:
            pass

    class _ProcessAPI:
        def __init__(self) -> None:
            self.client = _SlowClient()

        def get_client(self) -> _SlowClient:
            return self.client

        def handle_response_error(self, response: object) -> None:
            del response

    process_api = _ProcessAPI()
    stream = _BlaxelLogStream(process_api, "42", received.append, received.append)

    assert stream.wait(timeout=1)
    assert stream.error is None
    assert received == ["after-idle\n"]
    assert process_api.client.timeout.read is None
    stream.close()


def test_line_sink_enforces_byte_capacity_without_blocking_reader() -> None:
    sink = _LineSink(max_bytes=8)
    sink.feed("first")

    with pytest.raises(_OutputLimitExceeded, match="exceeded 8 bytes"):
        sink.feed("xy")

    assert sink.drain() == ["first\n"]
    sink.feed("xy")
    assert sink.drain() == ["xy\n"]
    sink.close()
