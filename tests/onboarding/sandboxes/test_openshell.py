"""Tests for :mod:`omnigent.onboarding.sandboxes.openshell`."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import click
import pytest

from omnigent.onboarding.sandboxes.base import DEFAULT_HOST_IMAGE
from omnigent.onboarding.sandboxes.openshell import (
    BASE_URL_ENV_VAR,
    HOST_IMAGE_ENV_VAR,
    SANDBOX_ENV_PASSTHROUGH_ENV_VAR,
    OpenShellSandboxLauncher,
    _OpenShellClient,
)


@dataclass
class _HttpRequest:
    """One recorded fake HTTP request."""

    method: str
    url: str
    kwargs: dict[str, Any]


class _FakeResponse:
    """Minimal ``httpx.Response`` stand-in for client tests."""

    def __init__(self, status_code: int, data: dict[str, Any], text: str = "") -> None:
        self.status_code = status_code
        self._data = data
        self.text = text

    def json(self) -> dict[str, Any]:
        return self._data


class _FakeHTTPClient:
    """Recorder for the subset of ``httpx.Client`` used by ``_OpenShellClient``."""

    def __init__(self) -> None:
        self.requests: list[_HttpRequest] = []
        self.closed = False
        self.next_response: _FakeResponse | None = None

    def request(
        self,
        method: str,
        url: str,
        *,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> _FakeResponse:
        self.requests.append(_HttpRequest(method=method, url=url, kwargs=kwargs))
        if self.next_response is not None:
            resp = self.next_response
            self.next_response = None
            return resp
        return _FakeResponse(200, {"id": "sb-1", "ok": True})

    def close(self) -> None:
        self.closed = True


@dataclass
class _FakeOpenShellAPI:
    """Recorder for the launcher-facing OpenShell API client."""

    create_payloads: list[dict[str, Any]] = field(default_factory=list)
    exec_calls: list[tuple[str, str]] = field(default_factory=list)
    uploads: list[tuple[str, Path, str]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    closed: bool = False

    def create_sandbox(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.create_payloads.append(dict(payload))
        return {"id": "sb-test-123"}

    def execute(self, sandbox_id: str, command: str, timeout: int = 300) -> dict[str, Any]:
        self.exec_calls.append((sandbox_id, command))
        return {"stdout": "out\n", "stderr": "err\n", "exit_code": 0}

    def upload_file(self, sandbox_id: str, local_path: Path, remote_path: str) -> None:
        self.uploads.append((sandbox_id, local_path, remote_path))

    def get_status(self, sandbox_id: str) -> dict[str, Any]:
        self.statuses.append(sandbox_id)
        return {"status": "running"}

    def delete_sandbox(self, sandbox_id: str) -> None:
        self.deleted.append(sandbox_id)

    def close(self) -> None:
        self.closed = True


def test_prepare_requires_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Preflight fails when ``OPENSHELL_BASE_URL`` is absent."""
    monkeypatch.delenv(BASE_URL_ENV_VAR, raising=False)
    with pytest.raises(click.ClickException, match="OPENSHELL_BASE_URL"):
        OpenShellSandboxLauncher().prepare()

    monkeypatch.setenv(BASE_URL_ENV_VAR, "http://localhost:8000")
    OpenShellSandboxLauncher().prepare()


def test_prepare_accepts_constructor_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Constructor ``base_url`` satisfies the preflight check."""
    monkeypatch.delenv(BASE_URL_ENV_VAR, raising=False)
    OpenShellSandboxLauncher(base_url="http://localhost:8000").prepare()


def test_provision_creates_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provisioning sends image, cpu, and memory in the create payload."""
    fake = _FakeOpenShellAPI()
    launcher = OpenShellSandboxLauncher(
        base_url="http://localhost:8000",
        image="custom-image:latest",
        cpu=4,
        memory_mb=8192,
    )
    monkeypatch.setattr(launcher, "_openshell", lambda: fake)

    sandbox_id = launcher.provision("test-host")

    assert sandbox_id == "sb-test-123"
    assert fake.create_payloads == [
        {
            "image": "custom-image:latest",
            "name": "test-host",
            "cpu": 4,
            "memory_mb": 8192,
        }
    ]


def test_provision_uses_default_image(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without an explicit image, the official host image is used."""
    monkeypatch.delenv(HOST_IMAGE_ENV_VAR, raising=False)
    fake = _FakeOpenShellAPI()
    launcher = OpenShellSandboxLauncher(base_url="http://localhost:8000")
    monkeypatch.setattr(launcher, "_openshell", lambda: fake)

    launcher.provision("host")

    [payload] = fake.create_payloads
    assert payload["image"] == DEFAULT_HOST_IMAGE


def test_provision_uses_image_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Host image can be overridden via environment variable."""
    monkeypatch.setenv(HOST_IMAGE_ENV_VAR, "docker.io/custom/host:1")
    fake = _FakeOpenShellAPI()
    launcher = OpenShellSandboxLauncher(base_url="http://localhost:8000")
    monkeypatch.setattr(launcher, "_openshell", lambda: fake)

    launcher.provision("host")

    [payload] = fake.create_payloads
    assert payload["image"] == "docker.io/custom/host:1"


def test_provision_with_env_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configured env vars are injected into the sandbox create payload."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("GIT_TOKEN", "ghp-test")
    fake = _FakeOpenShellAPI()
    launcher = OpenShellSandboxLauncher(
        base_url="http://localhost:8000",
        env=["OPENAI_API_KEY", "GIT_TOKEN"],
    )
    monkeypatch.setattr(launcher, "_openshell", lambda: fake)

    launcher.provision("host")

    [payload] = fake.create_payloads
    assert payload["env"] == {"OPENAI_API_KEY": "sk-test", "GIT_TOKEN": "ghp-test"}


def test_provision_env_passthrough_via_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env passthrough names can come from the process environment."""
    monkeypatch.setenv(SANDBOX_ENV_PASSTHROUGH_ENV_VAR, "OPENAI_API_KEY")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    fake = _FakeOpenShellAPI()
    launcher = OpenShellSandboxLauncher(base_url="http://localhost:8000")
    monkeypatch.setattr(launcher, "_openshell", lambda: fake)

    launcher.provision("host")

    [payload] = fake.create_payloads
    assert payload["env"] == {"OPENAI_API_KEY": "sk-test"}


def test_provision_env_passthrough_missing_var_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured but unset env name aborts before creating a sandbox."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    fake = _FakeOpenShellAPI()
    launcher = OpenShellSandboxLauncher(
        base_url="http://localhost:8000", env=["OPENAI_API_KEY"]
    )
    monkeypatch.setattr(launcher, "_openshell", lambda: fake)

    with pytest.raises(click.ClickException, match="OPENAI_API_KEY"):
        launcher.provision("host")
    assert fake.create_payloads == []


def test_run_captures_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run`` captures stdout, stderr, and exit code from the API response."""
    fake = _FakeOpenShellAPI()
    launcher = OpenShellSandboxLauncher(base_url="http://localhost:8000")
    monkeypatch.setattr(launcher, "_openshell", lambda: fake)

    result = launcher.run("sb-1", "echo hello")

    assert fake.exec_calls == [("sb-1", "echo hello")]
    assert result.returncode == 0
    assert result.stdout == "out\n"
    assert result.stderr == "err\n"


def test_run_check_raises_on_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run`` with ``check=True`` raises on non-zero exit."""
    fake = _FakeOpenShellAPI()
    fake_execute_orig = fake.execute

    def _failing_execute(sandbox_id: str, command: str, timeout: int = 300) -> dict:
        fake.exec_calls.append((sandbox_id, command))
        return {"stdout": "", "stderr": "error\n", "exit_code": 1}

    fake.execute = _failing_execute  # type: ignore[assignment]
    launcher = OpenShellSandboxLauncher(base_url="http://localhost:8000")
    monkeypatch.setattr(launcher, "_openshell", lambda: fake)

    with pytest.raises(click.ClickException, match="exit 1"):
        launcher.run("sb-1", "false")


def test_run_no_check_allows_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run`` with ``check=False`` returns the result even on non-zero exit."""
    fake = _FakeOpenShellAPI()

    def _failing_execute(sandbox_id: str, command: str, timeout: int = 300) -> dict:
        fake.exec_calls.append((sandbox_id, command))
        return {"stdout": "", "stderr": "error\n", "exit_code": 1}

    fake.execute = _failing_execute  # type: ignore[assignment]
    launcher = OpenShellSandboxLauncher(base_url="http://localhost:8000")
    monkeypatch.setattr(launcher, "_openshell", lambda: fake)

    result = launcher.run("sb-1", "false", check=False)

    assert result.returncode == 1
    assert result.stderr == "error\n"


def test_put_uploads_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``put`` delegates to the client's upload_file method."""
    fake = _FakeOpenShellAPI()
    launcher = OpenShellSandboxLauncher(base_url="http://localhost:8000")
    monkeypatch.setattr(launcher, "_openshell", lambda: fake)

    local_file = tmp_path / "wheels.tgz"
    local_file.write_bytes(b"fake-tarball")

    launcher.put("sb-1", local_file, "/tmp/wheels.tgz")

    assert fake.uploads == [("sb-1", local_file, "/tmp/wheels.tgz")]


def test_attach_validates_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """``attach`` checks sandbox status via the API."""
    fake = _FakeOpenShellAPI()
    launcher = OpenShellSandboxLauncher(base_url="http://localhost:8000")
    monkeypatch.setattr(launcher, "_openshell", lambda: fake)

    launcher.attach("sb-1")

    assert fake.statuses == ["sb-1"]


def test_terminate_deletes_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """``terminate`` calls delete and cleans up the client."""
    fake = _FakeOpenShellAPI()
    launcher = OpenShellSandboxLauncher(base_url="http://localhost:8000")
    monkeypatch.setattr(launcher, "_openshell", lambda: fake)
    monkeypatch.setattr(launcher, "_client", fake)  # So terminate can close it

    launcher.terminate("sb-1")

    assert fake.deleted == ["sb-1"]


def test_wheel_install_command() -> None:
    """``wheel_install_command`` delegates to the shared helper."""
    launcher = OpenShellSandboxLauncher(base_url="http://localhost:8000")
    cmd = launcher.wheel_install_command("/tmp/oa-wheels.tgz")
    assert "pip install" in cmd
    assert "/tmp/oa-wheels.tgz" in cmd


def test_client_create_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """The HTTP client sends a POST to /sandboxes."""
    import omnigent.onboarding.sandboxes.openshell as openshell_mod

    fake = _FakeHTTPClient()
    monkeypatch.setattr(openshell_mod.httpx, "Client", lambda **kwargs: fake)

    client = _OpenShellClient(base_url="http://localhost:8000/")

    result = client.create_sandbox({"image": "python:3.11"})
    assert result == {"id": "sb-1", "ok": True}

    assert len(fake.requests) == 1
    assert fake.requests[0].method == "POST"
    assert fake.requests[0].url == "http://localhost:8000/sandboxes"


def test_client_delete_ignores_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deleting a missing sandbox is treated as success (idempotent)."""
    import omnigent.onboarding.sandboxes.openshell as openshell_mod

    fake = _FakeHTTPClient()
    fake.next_response = _FakeResponse(
        404, {}, text="not found"
    )
    monkeypatch.setattr(openshell_mod.httpx, "Client", lambda **kwargs: fake)

    client = _OpenShellClient(base_url="http://localhost:8000")

    # The 404 triggers _response_error which raises _OpenShellAPIError
    # containing "HTTP 404", which delete_sandbox catches.
    client.delete_sandbox("gone-sandbox")

    assert len(fake.requests) == 1
    assert fake.requests[0].method == "DELETE"
