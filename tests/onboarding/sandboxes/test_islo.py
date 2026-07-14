"""Tests for :mod:`omnigent.onboarding.sandboxes.islo`."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import click
import pytest

import omnigent.onboarding.sandboxes.islo as islo_mod
from omnigent.onboarding.sandboxes.base import DEFAULT_HOST_IMAGE
from omnigent.onboarding.sandboxes.islo import (
    API_KEY_ENV_VAR,
    HOST_IMAGE_ENV_VAR,
    SANDBOX_ENV_PASSTHROUGH_ENV_VAR,
    IsloSandboxLauncher,
    _IsloClient,
    _parse_exec_sse,
)


@dataclass
class _ExecCall:
    """One fake Islo exec invocation."""

    sandbox_id: str
    command: list[str]


class _FakeResponse:
    """Minimal response stand-in for SDK raw HTTP paths."""

    def __init__(
        self,
        status_code: int,
        data: dict[str, Any] | None = None,
        text: str = "",
        lines: list[str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._data = data or {}
        self.text = text
        self._lines = lines or []

    def json(self) -> dict[str, Any]:
        """Return the canned JSON body."""
        return self._data

    def iter_lines(self) -> Iterator[str]:
        """Yield canned SSE lines."""
        yield from self._lines

    def read(self) -> bytes:
        """Return bytes for ``httpx.ResponseNotRead`` fallback paths."""
        return self.text.encode()


class _FakeStream:
    """Context manager returned by fake ``httpx.Client.stream``."""

    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    def __enter__(self) -> _FakeResponse:
        return self._response

    def __exit__(self, *exc_info: object) -> None:
        return None


class _FakeRawHTTPClient:
    """Recorder for streaming requests made with SDK auth headers."""

    def __init__(self, state: _SDKState) -> None:
        self._state = state

    def stream(self, method: str, url: str, **kwargs: Any) -> _FakeStream:
        """Record a raw streaming request."""
        self._state.stream_requests.append({"method": method, "url": url, **kwargs})
        response = _FakeResponse(
            self._state.stream_status,
            text=self._state.stream_text,
            lines=self._state.stream_lines,
        )
        return _FakeStream(response)

    def close(self) -> None:
        """Record close."""
        self._state.closed = True


class _FakeFernHTTPClient:
    """Recorder for SDK raw upload requests."""

    def __init__(self, state: _SDKState) -> None:
        self._state = state
        self.httpx_client = _FakeRawHTTPClient(state)

    def request(
        self,
        path: str,
        *,
        base_url: str,
        method: str,
        **kwargs: Any,
    ) -> _FakeResponse:
        """Record an upload request."""
        self._state.upload_requests.append(
            {"path": path, "base_url": base_url, "method": method, **kwargs}
        )
        return _FakeResponse(self._state.upload_status, text=self._state.upload_text)


@dataclass
class _FakeEnvironment:
    """SDK environment stand-in."""

    compute: str = "https://compute.islo.test"


class _FakeClientWrapper:
    """Subset of the SDK wrapper the Islo launcher uses."""

    def __init__(self, state: _SDKState) -> None:
        self._state = state
        self.httpx_client = _FakeFernHTTPClient(state)

    def get_environment(self) -> _FakeEnvironment:
        """Return the fake compute URL."""
        return _FakeEnvironment()

    def get_headers(self) -> dict[str, str]:
        """Return a fresh auth header so tests observe SDK token refresh use."""
        self._state.header_calls += 1
        return {"Authorization": f"Bearer session-{self._state.header_calls}"}


class _FakeAPIError(Exception):
    """SDK API error stand-in."""

    def __init__(self, status_code: int | None = None, body: object | None = None) -> None:
        super().__init__(f"status={status_code} body={body}")
        self.status_code = status_code
        self.body = body


class _FakeModel:
    """Pydantic-like SDK response object."""

    def __init__(self, **data: Any) -> None:
        self._data = data

    def model_dump(self) -> dict[str, Any]:
        """Return response data."""
        return dict(self._data)


@dataclass
class _SDKState:
    """Mutable state shared by fake SDK clients."""

    clients: list[Any] = field(default_factory=list)
    create_payloads: list[dict[str, Any]] = field(default_factory=list)
    get_calls: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    resumed: list[str] = field(default_factory=list)
    exec_calls: list[_ExecCall] = field(default_factory=list)
    upload_requests: list[dict[str, Any]] = field(default_factory=list)
    stream_requests: list[dict[str, Any]] = field(default_factory=list)
    header_calls: int = 0
    closed: bool = False
    statuses: dict[str, str] = field(default_factory=dict)
    create_error: Exception | None = None
    get_error: Exception | None = None
    delete_error: Exception | None = None
    resume_error: Exception | None = None
    exec_error: Exception | None = None
    exec_returncode: int = 0
    exec_stdout: str = "out\n"
    exec_stderr: str = "err\n"
    upload_status: int = 200
    upload_text: str = ""
    stream_status: int = 200
    stream_text: str = ""
    stream_lines: list[str] = field(
        default_factory=lambda: [
            "event: stdout",
            "data: out",
            "",
            "event: stderr",
            "data: err",
            "",
            "event: exit",
            "data: 0",
            "",
        ]
    )


def _install_fake_sdk(
    monkeypatch: pytest.MonkeyPatch, state: _SDKState | None = None
) -> _SDKState:
    """Install a fake Islo SDK loader and return its shared state."""
    state = state or _SDKState()

    class _FakeSandboxes:
        def create_sandbox(self, **payload: Any) -> _FakeModel:
            if state.create_error is not None:
                raise state.create_error
            state.create_payloads.append(dict(payload))
            return _FakeModel(name=payload["name"], status="running")

        def get_sandbox(self, name: str) -> _FakeModel:
            if state.get_error is not None:
                raise state.get_error
            state.get_calls.append(name)
            return _FakeModel(name=name, status=state.statuses.get(name, "running"))

        def delete_sandbox(self, name: str) -> None:
            if state.delete_error is not None:
                raise state.delete_error
            state.deleted.append(name)

        def resume_sandbox(self, name: str) -> _FakeModel:
            if state.resume_error is not None:
                raise state.resume_error
            state.resumed.append(name)
            return _FakeModel(name=name, status="running")

    class _FakeIslo:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.sandboxes = _FakeSandboxes()
            self._client_wrapper = _FakeClientWrapper(state)
            state.clients.append(self)

    @dataclass
    class _ExecResult:
        exit_code: int
        stdout: str
        stderr: str

    def _exec_and_wait_sync(
        _client: Any, sandbox_name: str, command: list[str], **kwargs: Any
    ) -> _ExecResult:
        del kwargs
        if state.exec_error is not None:
            raise state.exec_error
        state.exec_calls.append(_ExecCall(sandbox_id=sandbox_name, command=command))
        return _ExecResult(
            exit_code=state.exec_returncode,
            stdout=state.exec_stdout,
            stderr=state.exec_stderr,
        )

    monkeypatch.setattr(
        islo_mod,
        "_load_islo_sdk",
        lambda: islo_mod._IsloSDK(
            islo_cls=_FakeIslo,
            api_error_cls=_FakeAPIError,
            exec_and_wait_sync=_exec_and_wait_sync,
        ),
    )
    return state


@dataclass
class _FakeIsloAPI:
    """Recorder for the launcher-facing Islo API client."""

    create_payloads: list[dict[str, Any]] = field(default_factory=list)
    exec_calls: list[_ExecCall] = field(default_factory=list)
    uploads: list[tuple[str, Path, str]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    get_calls: list[str] = field(default_factory=list)
    resumed: list[str] = field(default_factory=list)
    statuses: dict[str, str] = field(default_factory=dict)
    get_error: islo_mod._IsloAPIError | None = None
    delete_error: islo_mod._IsloAPIError | None = None
    resume_error: islo_mod._IsloAPIError | None = None
    exec_error: islo_mod._IsloAPIError | None = None
    exec_returncode: int = 0

    def create_sandbox(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Record a create request and echo the sandbox name back."""
        self.create_payloads.append(dict(payload))
        return {"name": payload["name"], "status": "running"}

    def get_sandbox(self, name: str) -> dict[str, Any]:
        """Return a canned sandbox object."""
        if self.get_error is not None:
            raise self.get_error
        self.get_calls.append(name)
        return {"name": name, "status": self.statuses.get(name, "running")}

    def delete_sandbox(self, name: str) -> None:
        """Record deletion."""
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append(name)

    def resume_sandbox(self, name: str) -> dict[str, Any]:
        """Record resume."""
        if self.resume_error is not None:
            raise self.resume_error
        self.resumed.append(name)
        return {"name": name, "status": "running"}

    def upload_file(self, name: str, local_path: Path, remote_path: str) -> None:
        """Record file upload."""
        self.uploads.append((name, local_path, remote_path))

    def exec(
        self,
        name: str,
        command: list[str],
        *,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str, str]:
        """Record blocking command execution."""
        del workdir, env
        if self.exec_error is not None:
            raise self.exec_error
        self.exec_calls.append(_ExecCall(sandbox_id=name, command=command))
        stdout = "/root\n" if 'printf %s "$HOME"' in command[-1] else "out\n"
        return (self.exec_returncode, stdout, "err\n")

    def exec_stream(
        self,
        name: str,
        command: list[str],
        *,
        on_stdout: Any = None,
        on_stderr: Any = None,
    ) -> int:
        """Record command execution and emit one chunk per stream."""
        self.exec_calls.append(_ExecCall(sandbox_id=name, command=command))
        if on_stdout is not None:
            on_stdout("out\n")
        if on_stderr is not None:
            on_stderr("err\n")
        return 0

    def close(self) -> None:
        """No-op close for launcher tests."""
        return


def test_client_constructs_sdk_with_api_key_and_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """The adapter delegates auth/token refresh setup to the Islo SDK."""
    state = _install_fake_sdk(monkeypatch)

    client = _IsloClient(base_url="https://api.islo.dev/", api_key="ak-test")
    created = client.create_sandbox({"name": "sb-1", "image": "img"})
    fetched = client.get_sandbox("sb/1")
    returncode, stdout, stderr = client.exec("sb/1", ["bash", "-lc", "printf hi"])

    assert state.clients[0].kwargs == {
        "api_key": "ak-test",
        "base_url": "https://api.islo.dev",
        "timeout": 30.0,
    }
    assert state.create_payloads == [{"name": "sb-1", "image": "img"}]
    assert state.get_calls == ["sb/1"]
    assert created["name"] == "sb-1"
    assert fetched["status"] == "running"
    assert (returncode, stdout, stderr) == (0, "out\n", "err\n")
    assert state.exec_calls == [_ExecCall(sandbox_id="sb/1", command=["bash", "-lc", "printf hi"])]


def test_client_stream_exec_uses_sdk_auth_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Raw SSE requests source their auth headers from SDK internals."""
    state = _install_fake_sdk(monkeypatch)
    client = _IsloClient(base_url="https://api.islo.dev", api_key="ak-test")

    assert client.exec_stream("sb/1", ["bash", "-lc", "echo hi"]) == 0
    assert client.exec_stream("sb/1", ["bash", "-lc", "echo bye"]) == 0

    assert [req["headers"]["Authorization"] for req in state.stream_requests] == [
        "Bearer session-1",
        "Bearer session-2",
    ]
    assert (
        state.stream_requests[0]["url"] == "https://compute.islo.test/sandboxes/sb%2F1/exec/stream"
    )


def test_prepare_requires_islo_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Preflight fails before provisioning when ``ISLO_API_KEY`` is absent."""
    _install_fake_sdk(monkeypatch)
    monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
    with pytest.raises(click.ClickException, match="ISLO_API_KEY"):
        IsloSandboxLauncher().prepare()

    monkeypatch.setenv(API_KEY_ENV_VAR, "ak-test")
    IsloSandboxLauncher().prepare()


def test_prepare_reports_missing_optional_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Selecting Islo without ``omnigent[islo]`` gives a clear install hint."""
    monkeypatch.setenv(API_KEY_ENV_VAR, "ak-test")
    monkeypatch.setattr(
        islo_mod,
        "_load_islo_sdk",
        lambda: (_ for _ in ()).throw(click.ClickException("install omnigent[islo]")),
    )

    with pytest.raises(click.ClickException, match=r"omnigent\[islo\]"):
        IsloSandboxLauncher().prepare()


def test_provision_builds_islo_create_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Provisioning sends the official host image defaults plus configured
    env passthrough and Islo-specific resource/profile fields.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("GIT_TOKEN", "ghp-test")
    monkeypatch.setattr(islo_mod, "_new_sandbox_name", lambda label: "omnigent-fixed")
    fake = _FakeIsloAPI()
    launcher = IsloSandboxLauncher(
        image="docker.io/me/omnigent-host:latest",
        env=["OPENAI_API_KEY", "GIT_TOKEN"],
        gateway_profile="default",
        snapshot_name="warm-host",
        workdir="/root/workspace",
        vcpus=4,
        memory_mb=8192,
        disk_gb=40,
        idle_pause_after_s=1200,
    )
    monkeypatch.setattr(launcher, "_islo", lambda: fake)

    sandbox_id = launcher.provision("Managed Host")

    assert sandbox_id == "omnigent-fixed"
    assert fake.create_payloads == [
        {
            "name": "omnigent-fixed",
            "image": "docker.io/me/omnigent-host:latest",
            "vcpus": 4,
            "memory_mb": 8192,
            "init": {"type": "minimal"},
            "env": {"OPENAI_API_KEY": "sk-test", "GIT_TOKEN": "ghp-test"},
            "workdir": "/root/workspace",
            "gateway_profile": "default",
            "snapshot_name": "warm-host",
            "disk_gb": 40,
            "lifecycle": {"pause_after_idle": 1200, "auto_resume": "never"},
        }
    ]


def test_provision_uses_image_and_env_var_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Without constructor fields, host image and sandbox env names resolve
    from process env vars; otherwise the official image and empty env apply.
    """
    monkeypatch.setenv(HOST_IMAGE_ENV_VAR, "docker.io/env/host:1")
    monkeypatch.setenv(SANDBOX_ENV_PASSTHROUGH_ENV_VAR, "OPENAI_API_KEY")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(islo_mod, "_new_sandbox_name", lambda label: "omnigent-env")
    fake = _FakeIsloAPI()
    launcher = IsloSandboxLauncher()
    monkeypatch.setattr(launcher, "_islo", lambda: fake)

    launcher.provision("a")

    [payload] = fake.create_payloads
    assert payload["image"] == "docker.io/env/host:1"
    assert payload["env"] == {"OPENAI_API_KEY": "sk-test"}

    monkeypatch.delenv(HOST_IMAGE_ENV_VAR)
    monkeypatch.delenv(SANDBOX_ENV_PASSTHROUGH_ENV_VAR)
    fake2 = _FakeIsloAPI()
    launcher2 = IsloSandboxLauncher()
    monkeypatch.setattr(launcher2, "_islo", lambda: fake2)

    launcher2.provision("b")

    [payload2] = fake2.create_payloads
    assert payload2["image"] == DEFAULT_HOST_IMAGE
    assert payload2["lifecycle"] == {"pause_after_idle": 900, "auto_resume": "never"}
    assert "env" not in payload2


def test_provision_can_disable_idle_pause_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """``None`` leaves lifecycle policy to the operator."""
    monkeypatch.setattr(islo_mod, "_new_sandbox_name", lambda label: "omnigent-manual")
    fake = _FakeIsloAPI()
    launcher = IsloSandboxLauncher(idle_pause_after_s=None)
    monkeypatch.setattr(launcher, "_islo", lambda: fake)

    launcher.provision("manual")

    [payload] = fake.create_payloads
    assert "lifecycle" not in payload


def test_provision_env_passthrough_missing_var_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured but unset env name aborts before creating a sandbox."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    fake = _FakeIsloAPI()
    launcher = IsloSandboxLauncher(env=["OPENAI_API_KEY"])
    monkeypatch.setattr(launcher, "_islo", lambda: fake)

    with pytest.raises(click.ClickException, match="OPENAI_API_KEY"):
        launcher.provision("a")
    assert fake.create_payloads == []


@pytest.mark.parametrize("cred_var", ["CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"])
def test_provision_clears_seeded_helper_when_user_injects_claude_cred(
    monkeypatch: pytest.MonkeyPatch, cred_var: str
) -> None:
    """
    A user-injected Claude credential strips Islo's gateway ``apiKeyHelper``
    so the injected credential wins (covers both CLI and managed launches,
    which share ``provision``).
    """
    monkeypatch.setenv(cred_var, "secret-value")
    monkeypatch.setattr(islo_mod, "_new_sandbox_name", lambda label: "omnigent-byo")
    fake = _FakeIsloAPI()
    launcher = IsloSandboxLauncher(env=[cred_var])
    monkeypatch.setattr(launcher, "_islo", lambda: fake)

    launcher.provision("host")

    strip_calls = [call for call in fake.exec_calls if "apiKeyHelper" in call.command[-1]]
    assert len(strip_calls) == 1
    assert strip_calls[0].sandbox_id == "omnigent-byo"
    assert strip_calls[0].command[:2] == ["bash", "-lc"]


def test_provision_keeps_seeded_helper_without_user_claude_cred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gateway users (Option A) inject no Claude credential, so the seeded helper stays."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(islo_mod, "_new_sandbox_name", lambda label: "omnigent-gw")
    fake = _FakeIsloAPI()
    launcher = IsloSandboxLauncher(env=["OPENAI_API_KEY"])
    monkeypatch.setattr(launcher, "_islo", lambda: fake)

    launcher.provision("host")

    assert all("apiKeyHelper" not in call.command[-1] for call in fake.exec_calls)


def test_attach_validates_existing_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """``attach`` resolves the sandbox through the provider client."""
    fake = _FakeIsloAPI()
    launcher = IsloSandboxLauncher()
    monkeypatch.setattr(launcher, "_islo", lambda: fake)

    launcher.attach("sb-1")

    assert fake.get_calls == ["sb-1"]


def test_attach_failure_maps_to_click_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """Attach failures are reported at the CLI/provider boundary."""
    fake = _FakeIsloAPI(get_error=islo_mod._IsloAPIError("not found"))
    launcher = IsloSandboxLauncher()
    monkeypatch.setattr(launcher, "_islo", lambda: fake)

    with pytest.raises(click.ClickException, match="Could not attach"):
        launcher.attach("missing")


@pytest.mark.parametrize("status", ["running", "ready"])
def test_resume_running_sandbox_is_noop(monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    """Already-running Islo sandboxes do not call resume."""
    fake = _FakeIsloAPI(statuses={"sb-1": status})
    launcher = IsloSandboxLauncher()
    monkeypatch.setattr(launcher, "_islo", lambda: fake)

    launcher.resume("sb-1")

    assert fake.get_calls == ["sb-1"]
    assert fake.resumed == []


@pytest.mark.parametrize("status", ["paused", "stopped"])
def test_resume_paused_or_stopped_sandbox(monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    """Paused/stopped Islo sandboxes resume in place."""
    fake = _FakeIsloAPI(statuses={"sb-1": status})
    launcher = IsloSandboxLauncher()
    monkeypatch.setattr(launcher, "_islo", lambda: fake)

    launcher.resume("sb-1")

    assert fake.resumed == ["sb-1"]


@pytest.mark.parametrize("status", ["deleted", "failed", "mystery"])
def test_resume_rejects_non_resumable_states(monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    """Deleted, failed, and unknown states are not papered over."""
    fake = _FakeIsloAPI(statuses={"sb-1": status})
    launcher = IsloSandboxLauncher()
    monkeypatch.setattr(launcher, "_islo", lambda: fake)

    with pytest.raises(click.ClickException, match="cannot"):
        launcher.resume("sb-1")
    assert fake.resumed == []


def test_resume_sdk_failure_maps_to_click_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resume API failures surface as click errors."""
    fake = _FakeIsloAPI(
        statuses={"sb-1": "paused"},
        resume_error=islo_mod._IsloAPIError("resume failed"),
    )
    launcher = IsloSandboxLauncher()
    monkeypatch.setattr(launcher, "_islo", lambda: fake)

    with pytest.raises(click.ClickException, match="Could not resume"):
        launcher.resume("sb-1")


def test_start_host_stops_preserved_daemon_before_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Islo resume restarts the host daemon with the newly armed token."""
    fake = _FakeIsloAPI()
    launcher = IsloSandboxLauncher()
    monkeypatch.setattr(launcher, "_islo", lambda: fake)

    workspace = launcher.start_host(
        "sb-1",
        token="tok-new",
        host_id="host_1",
        host_name="managed-1",
        server_url="https://omnigent.example.com",
    )

    assert workspace == "/root/workspace"
    commands = [call.command[-1] for call in fake.exec_calls]
    cleanup_index = next(i for i, cmd in enumerate(commands) if "preserved omnigent host" in cmd)
    launch_index = next(i for i, cmd in enumerate(commands) if "OMNIGENT_HOST_TOKEN" in cmd)
    assert cleanup_index < launch_index
    assert "tok-new" in commands[launch_index]


def test_terminate_deletes_sandbox_and_closes_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """``terminate`` deletes the sandbox and clears the cached client."""
    fake = _FakeIsloAPI()
    launcher = IsloSandboxLauncher()
    launcher._client = fake  # type: ignore[assignment]

    launcher.terminate("sb-1")

    assert fake.deleted == ["sb-1"]
    assert launcher._client is None


def test_client_delete_ignores_missing_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provider delete is idempotent when the SDK reports 404."""
    state = _install_fake_sdk(
        monkeypatch, _SDKState(delete_error=_FakeAPIError(404, {"error": "missing"}))
    )
    client = _IsloClient(base_url="https://api.islo.dev", api_key="ak-test")

    client.delete_sandbox("missing")

    assert state.deleted == []


def test_client_delete_maps_sdk_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-404 SDK failures become launcher-facing ``_IsloAPIError``."""
    _install_fake_sdk(monkeypatch, _SDKState(delete_error=_FakeAPIError(500, {"error": "boom"})))
    client = _IsloClient(base_url="https://api.islo.dev", api_key="ak-test")

    with pytest.raises(islo_mod._IsloAPIError, match="HTTP 500"):
        client.delete_sandbox("sb-1")


def test_put_uploads_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``put`` delegates local file upload to the Islo client."""
    local = tmp_path / "wheels.tgz"
    local.write_bytes(b"wheel-data")
    fake = _FakeIsloAPI()
    launcher = IsloSandboxLauncher()
    monkeypatch.setattr(launcher, "_islo", lambda: fake)

    launcher.put("sb-1", local, "/tmp/wheels.tgz")

    assert fake.uploads == [("sb-1", local, "/tmp/wheels.tgz")]


def test_client_upload_uses_compute_url_and_file_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The SDK-backed upload path still sends a multipart file to compute."""
    state = _install_fake_sdk(monkeypatch)
    local = tmp_path / "wheels.tgz"
    local.write_bytes(b"wheel-data")
    client = _IsloClient(base_url="https://api.islo.dev", api_key="ak-test")

    client.upload_file("sb/1", local, "/tmp/wheels.tgz")

    [request] = state.upload_requests
    assert request["path"] == "sandboxes/sb%2F1/files"
    assert request["base_url"] == "https://compute.islo.test"
    assert request["method"] == "POST"
    assert request["params"] == {"path": "/tmp/wheels.tgz"}
    assert "file" in request["files"]


def test_client_upload_failure_maps_to_provider_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """HTTP upload failures become ``_IsloAPIError``."""
    state = _SDKState(upload_status=500, upload_text="boom")
    _install_fake_sdk(monkeypatch, state)
    local = tmp_path / "wheels.tgz"
    local.write_bytes(b"wheel-data")
    client = _IsloClient(base_url="https://api.islo.dev", api_key="ak-test")

    with pytest.raises(islo_mod._IsloAPIError, match="HTTP 500"):
        client.upload_file("sb-1", local, "/tmp/wheels.tgz")


def test_run_captures_stdout_and_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run`` calls Islo SDK exec through ``bash -lc`` and captures both streams."""
    fake = _FakeIsloAPI()
    launcher = IsloSandboxLauncher()
    monkeypatch.setattr(launcher, "_islo", lambda: fake)

    result = launcher.run("sb-1", "printf hi")

    assert fake.exec_calls == [_ExecCall(sandbox_id="sb-1", command=["bash", "-lc", "printf hi"])]
    assert result.returncode == 0
    assert result.stdout == "out\n"
    assert result.stderr == "err\n"


def test_run_nonzero_raises_when_check_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-zero remote exits fail by default and can be inspected with ``check=False``."""
    fake = _FakeIsloAPI(exec_returncode=7)
    launcher = IsloSandboxLauncher()
    monkeypatch.setattr(launcher, "_islo", lambda: fake)

    with pytest.raises(click.ClickException, match="exit 7"):
        launcher.run("sb-1", "false")
    assert launcher.run("sb-1", "false", check=False).returncode == 7


def test_run_sdk_failure_maps_to_click_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """SDK exec errors are translated to ``ClickException``."""
    fake = _FakeIsloAPI(exec_error=islo_mod._IsloAPIError("sdk failed"))
    launcher = IsloSandboxLauncher()
    monkeypatch.setattr(launcher, "_islo", lambda: fake)

    with pytest.raises(click.ClickException, match="Remote command failed to execute"):
        launcher.run("sb-1", "printf hi")


def test_stream_exec_yields_combined_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """``stream_exec`` returns a process that yields stdout/stderr lines."""
    fake = _FakeIsloAPI()
    launcher = IsloSandboxLauncher()
    monkeypatch.setattr(launcher, "_islo", lambda: fake)

    process = launcher.stream_exec("sb-1", "printf hi", pty=True)

    assert list(process.lines) == ["out\n", "err\n"]
    assert process.wait() == 0
    assert fake.exec_calls == [_ExecCall(sandbox_id="sb-1", command=["bash", "-lc", "printf hi"])]


def test_exec_foreground_echoes_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Foreground exec streams combined output to the caller."""
    fake = _FakeIsloAPI()
    launcher = IsloSandboxLauncher()
    monkeypatch.setattr(launcher, "_islo", lambda: fake)

    assert launcher.exec_foreground("sb-1", "env") == 0

    assert capsys.readouterr().out.endswith("out\nerr\n")
    assert fake.exec_calls == [
        _ExecCall(sandbox_id="sb-1", command=["bash", "-lc", "TERM=xterm-256color exec env"])
    ]


def test_provision_sdk_failure_maps_to_click_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """SDK create failures surface as click errors at the launcher boundary."""
    fake = _FakeIsloAPI()

    def _fail_create(_payload: dict[str, Any]) -> dict[str, Any]:
        raise islo_mod._IsloAPIError("sdk create failed")

    fake.create_sandbox = _fail_create  # type: ignore[method-assign]
    launcher = IsloSandboxLauncher()
    monkeypatch.setattr(launcher, "_islo", lambda: fake)

    with pytest.raises(click.ClickException, match="Islo sandbox creation failed"):
        launcher.provision("host")


def test_parse_exec_sse_routes_events_and_requires_exit() -> None:
    """Islo exec SSE events are routed by event type and must include an exit code."""
    stdout: list[str] = []
    stderr: list[str] = []

    returncode = _parse_exec_sse(
        iter(
            [
                "event: stdout",
                "data: hello",
                "",
                "event: stderr",
                "data: warn",
                "",
                "event: exit",
                "data: 7",
                "",
            ]
        ),
        on_stdout=stdout.append,
        on_stderr=stderr.append,
    )

    assert returncode == 7
    assert stdout == ["hello"]
    assert stderr == ["warn"]

    with pytest.raises(RuntimeError, match="without exit event"):
        _parse_exec_sse(iter(["event: stdout", "data: hello", ""]), on_stdout=None, on_stderr=None)
