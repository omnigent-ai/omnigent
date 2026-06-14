"""Tests for :mod:`omnigent.onboarding.sandboxes.islo`."""

from __future__ import annotations

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
class _HttpRequest:
    """One recorded fake HTTP request."""

    method: str
    url: str
    headers: dict[str, str]
    kwargs: dict[str, Any]


class _FakeResponse:
    """Minimal ``httpx.Response`` stand-in for the Islo client tests."""

    def __init__(self, status_code: int, data: dict[str, Any], text: str = "") -> None:
        self.status_code = status_code
        self._data = data
        self.text = text

    def json(self) -> dict[str, Any]:
        """Return the canned JSON body."""
        return self._data


class _FakeHTTPClient:
    """Recorder for the subset of ``httpx.Client`` used by ``_IsloClient``."""

    def __init__(self) -> None:
        self.token_posts: list[dict[str, Any]] = []
        self.requests: list[_HttpRequest] = []
        self.closed = False

    def post(self, url: str, *, json: dict[str, Any], timeout: float) -> _FakeResponse:
        """Record the token exchange and return a cacheable session token."""
        self.token_posts.append({"url": url, "json": json, "timeout": timeout})
        return _FakeResponse(200, {"session_token": "session-123", "cookie_max_age": 120})

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        **kwargs: Any,
    ) -> _FakeResponse:
        """Record an authenticated API request and return an object body."""
        self.requests.append(_HttpRequest(method=method, url=url, headers=headers, kwargs=kwargs))
        return _FakeResponse(200, {"ok": True})

    def close(self) -> None:
        """Record close."""
        self.closed = True


@dataclass
class _ExecCall:
    """One fake Islo exec-stream invocation."""

    sandbox_id: str
    command: list[str]


@dataclass
class _FakeIsloAPI:
    """Recorder for the launcher-facing Islo API client."""

    create_payloads: list[dict[str, Any]] = field(default_factory=list)
    exec_calls: list[_ExecCall] = field(default_factory=list)
    uploads: list[tuple[str, Path, str]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    def create_sandbox(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Record a create request and echo the sandbox name back."""
        self.create_payloads.append(dict(payload))
        return {"name": payload["name"]}

    def get_sandbox(self, name: str) -> dict[str, Any]:
        """Return a canned sandbox object."""
        return {"name": name}

    def delete_sandbox(self, name: str) -> None:
        """Record deletion."""
        self.deleted.append(name)

    def upload_file(self, name: str, local_path: Path, remote_path: str) -> None:
        """Record file upload."""
        self.uploads.append((name, local_path, remote_path))

    def exec_stream(
        self,
        name: str,
        command: list[str],
        *,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
        on_stdout: Any = None,
        on_stderr: Any = None,
    ) -> int:
        """Record command execution and emit one chunk per stream."""
        del workdir, env
        self.exec_calls.append(_ExecCall(sandbox_id=name, command=command))
        if on_stdout is not None:
            on_stdout("out\n")
        if on_stderr is not None:
            on_stderr("err\n")
        return 0


def test_client_exchanges_access_key_for_cached_bearer_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``ISLO_API_KEY`` is not sent to sandbox endpoints directly; it is
    exchanged for a session token that authenticates API calls.
    """
    fake = _FakeHTTPClient()
    monkeypatch.setattr(islo_mod.httpx, "Client", lambda **kwargs: fake)

    client = _IsloClient(base_url="https://api.islo.dev/", api_key="ak-test")

    assert client.create_sandbox({"name": "sb-1"}) == {"ok": True}
    assert client.get_sandbox("sb/1") == {"ok": True}

    assert fake.token_posts == [
        {
            "url": "https://api.islo.dev/auth/token",
            "json": {"access_key": "ak-test"},
            "timeout": 30.0,
        }
    ]
    assert [req.headers["Authorization"] for req in fake.requests] == [
        "Bearer session-123",
        "Bearer session-123",
    ]
    assert fake.requests[1].url == "https://api.islo.dev/sandboxes/sb%2F1"


def test_prepare_requires_islo_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Preflight fails before provisioning when ``ISLO_API_KEY`` is absent."""
    monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
    with pytest.raises(click.ClickException, match="ISLO_API_KEY"):
        IsloSandboxLauncher().prepare()

    monkeypatch.setenv(API_KEY_ENV_VAR, "ak-test")
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
    assert "env" not in payload2


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


def test_run_streams_stdout_and_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run`` calls Islo exec streaming through ``bash -lc`` and captures both streams."""
    fake = _FakeIsloAPI()
    launcher = IsloSandboxLauncher()
    monkeypatch.setattr(launcher, "_islo", lambda: fake)

    result = launcher.run("sb-1", "printf hi")

    assert fake.exec_calls == [_ExecCall(sandbox_id="sb-1", command=["bash", "-lc", "printf hi"])]
    assert result.returncode == 0
    assert result.stdout == "out\n"
    assert result.stderr == "err\n"


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
