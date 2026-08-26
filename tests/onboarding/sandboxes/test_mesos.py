"""Tests for the mesos-compose managed sandbox launcher."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import click
import httpx
import pytest
import yaml

from omnigent.host.identity import HOST_ID_ENV_VAR, HOST_NAME_ENV_VAR, HOST_TOKEN_ENV_VAR
from omnigent.onboarding.sandboxes.base import DEFAULT_HOST_IMAGE
from omnigent.onboarding.sandboxes.mesos import MesosSandboxLauncher, build_compose_manifest


class _FakeClient:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.closed = False

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.requests.append((method, url, kwargs))
        response = self.responses.pop(0)
        response.request = httpx.Request(method, url)
        return response

    def close(self) -> None:
        self.closed = True


def test_build_compose_manifest_runs_host_on_requested_agent() -> None:
    manifest = build_compose_manifest(
        image="registry.example/omnigent-host:test",
        sandbox_id="omnigent-managed-a1b2c3",
        host_id="host-1",
        host_name="managed-a1b2c3",
        token="launch-token",
        server_url="http://omnigent.weave.local:6767",
        env_literals={"OPENAI_BASE_URL": "http://ollama:11434/v1"},
        target_hostname="andreas-ki.lab.internal",
        repo_url="https://github.com/example/repo.git",
        repo_branch="main",
        repo_name="repo",
        host_config={"executor": {"model": "qwen3.6:latest"}},
    )

    service = manifest["services"]["host"]
    assert service["image"] == "registry.example/omnigent-host:test"
    assert service["shell"] is True
    assert service["restart"] == "no"
    assert service["deploy"]["resources"]["limits"] == {
        "cpus": 1.0,
        "memory": 1024,
    }
    assert service["deploy"]["placement"]["constraints"] == [
        "node.hostname==andreas-ki.lab.internal"
    ]
    assert service["environment"] == {
        "HOME": "/mnt/mesos/sandbox",
        "IS_SANDBOX": "1",
        HOST_ID_ENV_VAR: "host-1",
        HOST_NAME_ENV_VAR: "managed-a1b2c3",
        HOST_TOKEN_ENV_VAR: "launch-token",
        "OPENAI_BASE_URL": "http://ollama:11434/v1",
    }
    command = service["command"]
    assert "git clone --branch main --single-branch --" in command
    assert "https://github.com/example/repo.git" in command
    assert "omnigent host --server http://omnigent.weave.local:6767" in command
    assert "qwen3.6:latest" not in command
    assert "__PAYLOAD__" not in command


def test_launcher_uses_mesos_compose_api_for_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_MESOS_COMPOSE_PASSWORD", "password")
    monkeypatch.setenv("OMNIGENT_MESOS_ALLOW_INSECURE_TLS", "1")
    monkeypatch.setattr(
        "omnigent.onboarding.sandboxes.mesos.uuid.uuid4",
        lambda: SimpleNamespace(hex="123456abcdef"),
    )
    fake = _FakeClient(
        [
            httpx.Response(200, text="/api/compose/v0"),
            httpx.Response(200, json={"services": {"host": {}}}),
            httpx.Response(
                200,
                json=[
                    {
                        "TaskID": "omnigent-managed-a1b2-123456_host.task.0",
                        "State": "TASK_RUNNING",
                    }
                ],
            ),
            httpx.Response(200, text=""),
        ]
    )
    monkeypatch.setattr(httpx, "Client", lambda **_kwargs: fake)
    launcher = MesosSandboxLauncher(
        image="registry.example/omnigent-host:test",
        env=(),
        compose_url="https://compose.example:10002/",
        username="user",
        verify_ssl=False,
        target_hostname="agent.example",
    )

    launcher.prepare()
    sandbox_id = launcher.provision("Managed A1B2")
    workspace = launcher.start_host(
        sandbox_id,
        token="token",
        host_id="host-id",
        host_name="managed-a1b2",
        server_url="https://omnigent.example",
    )
    launcher.terminate(sandbox_id)

    assert sandbox_id.startswith("omnigent-managed-a1b2-")
    assert workspace == "/mnt/mesos/sandbox/workspace"
    assert [(method, url) for method, url, _ in fake.requests] == [
        ("GET", "/api/compose/versions"),
        ("PUT", f"/api/compose/v0/{sandbox_id}"),
        ("GET", "/api/compose/v0/tasks"),
        ("DELETE", f"/api/compose/v0/{sandbox_id}/host"),
    ]
    pushed = yaml.safe_load(fake.requests[1][2]["content"])
    assert pushed["services"]["host"]["deploy"]["placement"]["constraints"] == [
        "node.hostname==agent.example"
    ]


def test_prepare_surfaces_mesos_compose_auth_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient([httpx.Response(401, text="unauthorized")])
    monkeypatch.setattr(httpx, "Client", lambda **_kwargs: fake)
    launcher = MesosSandboxLauncher(compose_url="https://compose.example", env=())

    with pytest.raises(click.ClickException, match=r"authenticate|401"):
        launcher.prepare()


def test_env_passthrough_requires_server_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISSING_MESOS_VALUE", raising=False)
    launcher = MesosSandboxLauncher(
        compose_url="https://compose.example", env=["MISSING_MESOS_VALUE"]
    )

    with pytest.raises(click.ClickException, match="not set"):
        launcher._resolve_sandbox_env()


def test_default_image_is_used_when_none_is_configured() -> None:
    launcher = MesosSandboxLauncher(compose_url="https://compose.example", env=())

    assert launcher._resolve_image() == DEFAULT_HOST_IMAGE


def test_basic_auth_password_comes_from_environment_and_requires_https(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_MESOS_COMPOSE_PASSWORD", "secret")
    launcher = MesosSandboxLauncher(
        compose_url="http://compose.example",
        username="user",
        env=(),
    )

    with pytest.raises(click.ClickException, match="HTTPS"):
        launcher.prepare()


def test_authenticated_client_uses_environment_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_MESOS_COMPOSE_PASSWORD", "secret")
    captured: dict[str, Any] = {}
    fake = _FakeClient([httpx.Response(200, text="/api/compose/v0")])

    def _client(**kwargs: Any) -> _FakeClient:
        captured.update(kwargs)
        return fake

    monkeypatch.setattr(httpx, "Client", _client)
    launcher = MesosSandboxLauncher(
        compose_url="https://compose.example",
        username="user",
        env=(),
    )

    launcher.prepare()

    assert captured["auth"] == ("user", "secret")


def test_insecure_tls_with_auth_requires_explicit_environment_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_MESOS_COMPOSE_PASSWORD", "secret")
    monkeypatch.delenv("OMNIGENT_MESOS_ALLOW_INSECURE_TLS", raising=False)
    launcher = MesosSandboxLauncher(
        compose_url="https://compose.example",
        username="user",
        verify_ssl=False,
        env=(),
    )

    with pytest.raises(click.ClickException, match="ALLOW_INSECURE_TLS"):
        launcher.prepare()


def test_terminate_retries_transient_compose_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(
        [
            httpx.Response(503, text="unavailable"),
            httpx.Response(502, text="bad gateway"),
            httpx.Response(200, text=""),
        ]
    )
    monkeypatch.setattr(httpx, "Client", lambda **_kwargs: fake)
    monkeypatch.setattr("omnigent.onboarding.sandboxes.mesos.time.sleep", lambda _delay: None)
    launcher = MesosSandboxLauncher(compose_url="https://compose.example", env=())

    launcher.terminate("omnigent-managed-a1b2")

    assert [(method, path) for method, path, _ in fake.requests] == [
        ("DELETE", "/api/compose/v0/omnigent-managed-a1b2/host"),
        ("DELETE", "/api/compose/v0/omnigent-managed-a1b2/host"),
        ("DELETE", "/api/compose/v0/omnigent-managed-a1b2/host"),
    ]


@pytest.mark.parametrize("state", ["TASK_FINISHED", "TASK_UNREACHABLE", "TASK_UNKNOWN"])
def test_start_host_fails_immediately_for_additional_terminal_states(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    fake = _FakeClient(
        [
            httpx.Response(200, json={"services": {"host": {}}}),
            httpx.Response(
                200,
                json=[
                    {
                        "TaskID": "sandbox_host.task.0",
                        "State": state,
                    }
                ],
            ),
        ]
    )
    monkeypatch.setattr(httpx, "Client", lambda **_kwargs: fake)
    launcher = MesosSandboxLauncher(compose_url="https://compose.example", env=())

    with pytest.raises(click.ClickException, match=state):
        launcher.start_host(
            "sandbox",
            token="token",
            host_id="host-id",
            host_name="host-name",
            server_url="https://omnigent.example",
        )
