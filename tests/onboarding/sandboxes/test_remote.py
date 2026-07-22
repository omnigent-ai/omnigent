"""Tests for the external sandbox runtime controller adapter."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import URLError
from urllib.request import Request

import click
import pytest

from omnigent.onboarding.sandboxes.remote import RemoteSandboxLauncher


class _Response:
    def __init__(self, body: dict[str, object] | None = None) -> None:
        self._body = json.dumps(body).encode() if body is not None else b""

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, amount: int = -1) -> bytes:
        return self._body if amount < 0 else self._body[:amount]


def test_provision_sends_launch_context_and_returns_stable_runtime_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[Request] = []

    def _urlopen(request: Request, *, timeout: int) -> _Response:
        assert timeout == 15 * 60
        requests.append(request)
        return _Response({"runtime": {"id": "runtime_abc", "state": "running"}})

    monkeypatch.setenv("OMNIGENT_REMOTE_SANDBOX_TOKEN", "runtime-secret")
    monkeypatch.setenv("PLATFORM_GIT_BROKER_URL", "https://platform.example.com/git")
    monkeypatch.setattr("omnigent.onboarding.sandboxes.remote.urlopen", _urlopen)
    launcher = RemoteSandboxLauncher(
        url="https://platform.example.com",
        env=["PLATFORM_GIT_BROKER_URL"],
    )
    launcher.set_launch_context(owner="alice@example.com", session_id="conv_alice")

    assert launcher.provision("managed-abcd1234") == "runtime_abc"
    payload = json.loads(requests[0].data or b"{}")
    assert payload == {
        "name": "managed-abcd1234",
        "owner": "alice@example.com",
        "sessionId": "conv_alice",
        "env": {"PLATFORM_GIT_BROKER_URL": "https://platform.example.com/git"},
    }
    assert requests[0].headers["Authorization"] == "Bearer runtime-secret"
    assert requests[0].headers["X-sandbox-runtime-api-version"] == "1"


def test_run_uses_controller_command_endpoint_and_preserves_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def _urlopen(request: Request, *, timeout: int) -> _Response:
        del timeout
        seen["url"] = request.full_url
        seen["payload"] = json.loads(request.data or b"{}")
        if request.method == "GET":
            return _Response({"runtime": {"id": "runtime_abc", "state": "running"}})
        return _Response({"result": {"exitCode": 0, "stdout": "hello\n", "stderr": ""}})

    monkeypatch.setenv("OMNIGENT_REMOTE_SANDBOX_TOKEN", "runtime-secret")
    monkeypatch.setattr("omnigent.onboarding.sandboxes.remote.urlopen", _urlopen)
    launcher = RemoteSandboxLauncher(url="https://platform.example.com")

    result = launcher.run("runtime_abc", "printf hello")

    assert seen["url"].endswith("/api/v1/sandbox-runtimes/runtime_abc/commands")
    assert seen["payload"] == {"command": "printf hello", "timeoutSeconds": 15 * 60}
    assert result.returncode == 0
    assert result.stdout == "hello\n"


def test_background_run_uses_the_shared_durable_shell_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads: list[dict[str, object]] = []

    def _urlopen(request: Request, *, timeout: int) -> _Response:
        del timeout
        if request.method == "GET":
            return _Response({"runtime": {"id": "runtime_abc", "state": "running"}})
        payloads.append(json.loads(request.data or b"{}"))
        return _Response({"result": {"exitCode": 0, "stdout": "launched\n", "stderr": ""}})

    monkeypatch.setenv("OMNIGENT_REMOTE_SANDBOX_TOKEN", "runtime-secret")
    monkeypatch.setattr("omnigent.onboarding.sandboxes.remote.urlopen", _urlopen)
    launcher = RemoteSandboxLauncher(url="https://platform.example.com")

    result = launcher.run_background(
        "runtime_abc",
        "FOO=bar omnigent host --server https://omnigent.example.com",
    )

    assert result.stdout == "launched\n"
    assert payloads == [
        {
            "command": "setsid nohup sh -c "
            "'FOO=bar omnigent host --server https://omnigent.example.com' "
            "> /tmp/omnigent-host.log 2>&1 < /dev/null & echo launched",
            "timeoutSeconds": 15 * 60,
        }
    ]


def test_stopped_runtime_is_resumed_through_the_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, str]] = []
    get_states = iter(["stopped", "stopped", "running"])

    def _urlopen(request: Request, *, timeout: int) -> _Response:
        del timeout
        requests.append((request.method, request.full_url))
        if request.method == "GET":
            return _Response({"runtime": {"id": "runtime_abc", "state": next(get_states)}})
        return _Response({"runtime": {"id": "runtime_abc", "state": "provisioning"}})

    monkeypatch.setenv("OMNIGENT_REMOTE_SANDBOX_TOKEN", "runtime-secret")
    monkeypatch.setattr("omnigent.onboarding.sandboxes.remote.urlopen", _urlopen)
    monkeypatch.setattr("omnigent.onboarding.sandboxes.remote.time.sleep", lambda _seconds: None)
    launcher = RemoteSandboxLauncher(url="https://platform.example.com")

    assert launcher.is_running("runtime_abc") is False
    assert launcher.exists("runtime_abc") is True
    launcher.resume("runtime_abc")

    assert (
        "POST",
        "https://platform.example.com/api/v1/sandbox-runtimes/runtime_abc/resume",
    ) in requests
    assert requests[-1] == (
        "GET",
        "https://platform.example.com/api/v1/sandbox-runtimes/runtime_abc",
    )


def test_first_command_polls_a_stopped_runtime_before_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, str]] = []
    states = iter(["stopped", "provisioning", "running"])

    def _urlopen(request: Request, *, timeout: int) -> _Response:
        del timeout
        requests.append((request.method, request.full_url))
        if request.method == "GET":
            return _Response({"runtime": {"id": "runtime_abc", "state": next(states)}})
        if request.full_url.endswith("/resume"):
            return _Response({"runtime": {"id": "runtime_abc", "state": "provisioning"}})
        return _Response({"result": {"exitCode": 0, "stdout": "awake\n", "stderr": ""}})

    monkeypatch.setenv("OMNIGENT_REMOTE_SANDBOX_TOKEN", "runtime-secret")
    monkeypatch.setattr("omnigent.onboarding.sandboxes.remote.urlopen", _urlopen)
    monkeypatch.setattr("omnigent.onboarding.sandboxes.remote.time.sleep", lambda _seconds: None)
    launcher = RemoteSandboxLauncher(url="https://platform.example.com")

    result = launcher.run("runtime_abc", "printf awake")

    assert result.stdout == "awake\n"
    assert requests == [
        ("GET", "https://platform.example.com/api/v1/sandbox-runtimes/runtime_abc"),
        ("POST", "https://platform.example.com/api/v1/sandbox-runtimes/runtime_abc/resume"),
        ("GET", "https://platform.example.com/api/v1/sandbox-runtimes/runtime_abc"),
        ("GET", "https://platform.example.com/api/v1/sandbox-runtimes/runtime_abc"),
        ("POST", "https://platform.example.com/api/v1/sandbox-runtimes/runtime_abc/commands"),
    ]


def test_activity_signal_uses_versioned_controller_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, str, dict[str, object]]] = []

    def _urlopen(request: Request, *, timeout: int) -> _Response:
        assert timeout == 30
        requests.append((request.method, request.full_url, json.loads(request.data or b"{}")))
        return _Response({"runtime": {"id": "runtime_abc", "active": True}})

    monkeypatch.setenv("OMNIGENT_REMOTE_SANDBOX_TOKEN", "runtime-secret")
    monkeypatch.setattr("omnigent.onboarding.sandboxes.remote.urlopen", _urlopen)
    launcher = RemoteSandboxLauncher(url="https://platform.example.com")

    launcher.set_activity("runtime_abc", active=True)

    assert requests == [
        (
            "POST",
            "https://platform.example.com/api/v1/sandbox-runtimes/runtime_abc/activity",
            {"active": True},
        )
    ]


def test_retryable_status_lookup_recovers_from_transient_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def _urlopen(request: Request, *, timeout: int) -> _Response:
        del request, timeout
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise URLError("temporary outage")
        return _Response({"runtime": {"id": "runtime_abc", "state": "running"}})

    monkeypatch.setenv("OMNIGENT_REMOTE_SANDBOX_TOKEN", "runtime-secret")
    monkeypatch.setattr("omnigent.onboarding.sandboxes.remote.urlopen", _urlopen)
    monkeypatch.setattr("omnigent.onboarding.sandboxes.remote.time.sleep", lambda _seconds: None)
    launcher = RemoteSandboxLauncher(url="https://platform.example.com")

    assert launcher.is_running("runtime_abc") is True
    assert attempts == 2


def test_response_body_is_bounded_before_json_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _OversizedResponse(_Response):
        def __init__(self) -> None:
            self._body = b"x" * (1024 * 1024 + 1)

    monkeypatch.setenv("OMNIGENT_REMOTE_SANDBOX_TOKEN", "runtime-secret")
    monkeypatch.setattr(
        "omnigent.onboarding.sandboxes.remote.urlopen",
        lambda _request, *, timeout: _OversizedResponse(),
    )
    launcher = RemoteSandboxLauncher(url="https://platform.example.com")

    with pytest.raises(click.ClickException, match="response exceeded 1048576 bytes"):
        launcher.is_running("runtime_abc")
