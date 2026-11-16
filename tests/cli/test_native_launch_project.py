"""Tests for ``omnigent claude|codex --project``: filing a new session in a named project."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import click
import httpx
import pytest
import respx
from click.testing import CliRunner

from omnigent.cli import _require_project_aware_session_create, cli

_BASE = "http://localhost:6767"
_HOST_ID = "host_abc123"
_PROJECT_ID = "proj_factory"
_LAUNCHERS = [
    ("claude", "omnigent.harnesses.claude_native.main.run_claude_native"),
    ("codex", "omnigent.harnesses.codex_native.main.run_codex_native"),
]
# Native flags that still start a new Omnigent session, so they pass through unchanged.
_PASS_THROUGH_ARGS = {
    "claude": ["--continue", "--session-id", "123e4567-e89b-12d3-a456-426614174000"],
    "codex": ["--search"],
}


@pytest.fixture()
def launch_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Point both launchers at a fake backend and capture what each would launch."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    monkeypatch.setattr("omnigent.cli._ensure_backend", lambda _server: _BASE)
    monkeypatch.setattr("omnigent.cli._ensure_host_daemon", lambda _server: False)
    monkeypatch.setattr(
        "omnigent.host.identity.load_or_create_host_identity",
        lambda: type("_Id", (), {"host_id": _HOST_ID})(),
    )
    monkeypatch.setattr("omnigent.smart_routing_cli.local_gateway_inference", dict)
    monkeypatch.setenv("OMNIGENT_REMOTE_AUTH_TOKEN", "test-token")
    for _command, launcher in _LAUNCHERS:
        monkeypatch.setattr(launcher, lambda **kwargs: captured.update(kwargs))
    return captured


def _mock_server_version(version: object = "0.22.0") -> respx.Route:
    return respx.get(f"{_BASE}/api/version").mock(
        return_value=httpx.Response(200, json={"version": version})
    )


def _mock_smart_routing(*, enabled: bool) -> None:
    respx.get(f"{_BASE}/v1/info").mock(
        return_value=httpx.Response(
            200,
            json={
                "smart_routing_enabled": enabled,
                "smart_routing_sources": {"external": enabled, "oss": False},
            },
        )
    )
    respx.get(f"{_BASE}/v1/hosts").mock(
        return_value=httpx.Response(
            200, json={"hosts": [{"host_id": _HOST_ID, "status": "online"}]}
        )
    )


def _mock_projects(*projects: dict[str, str]) -> respx.Route:
    _mock_server_version()
    return respx.get(f"{_BASE}/v1/projects").mock(
        return_value=httpx.Response(200, json={"object": "list", "data": list(projects)})
    )


@respx.mock
@pytest.mark.parametrize(("command", "launcher"), _LAUNCHERS)
def test_an_existing_project_is_passed_to_the_new_session(
    command: str, launcher: str, launch_env: dict[str, Any]
) -> None:
    listed = _mock_projects(
        {"id": "proj_other", "name": "factory"},
        {"id": _PROJECT_ID, "name": "factory workers"},
    )
    created = respx.post(f"{_BASE}/v1/projects")
    native_args = _PASS_THROUGH_ARGS[command]

    result = CliRunner().invoke(cli, [command, "--project", "  factory workers ", *native_args])

    assert result.exit_code == 0, result.output
    assert launch_env["project_id"] == _PROJECT_ID
    assert launch_env["session_id"] is None
    assert list(launch_env["extra_args"]) == native_args
    assert listed.calls.last.request.headers["authorization"] == "Bearer test-token"
    assert not created.called
    assert "created project" not in result.output


@respx.mock
@pytest.mark.parametrize(("command", "launcher"), _LAUNCHERS)
def test_a_missing_project_is_created_and_announced(
    command: str, launcher: str, launch_env: dict[str, Any]
) -> None:
    _mock_projects({"id": "proj_other", "name": "Factory Workers"})
    created = respx.post(f"{_BASE}/v1/projects").mock(
        return_value=httpx.Response(200, json={"id": _PROJECT_ID, "name": "factory workers"})
    )

    result = CliRunner().invoke(cli, [command, "--project", "factory workers"])

    assert result.exit_code == 0, result.output
    assert json.loads(created.calls.last.request.content) == {"name": "factory workers"}
    assert launch_env["project_id"] == _PROJECT_ID
    assert "omnigent: created project 'factory workers'." in result.stderr


@respx.mock
@pytest.mark.parametrize(("command", "launcher"), _LAUNCHERS)
@pytest.mark.parametrize(
    ("listing", "detail"),
    [
        ({"return_value": httpx.Response(503, text="unavailable")}, "could not list projects"),
        ({"side_effect": httpx.ConnectError("connection refused")}, "ConnectError"),
    ],
)
def test_a_project_that_cannot_be_resolved_stops_the_launch(
    command: str,
    launcher: str,
    listing: dict[str, Any],
    detail: str,
    launch_env: dict[str, Any],
) -> None:
    _mock_server_version()
    respx.get(f"{_BASE}/v1/projects").mock(**listing)

    result = CliRunner().invoke(cli, [command, "--project", "factory workers"])

    assert result.exit_code == 1, result.output
    assert "Could not use project 'factory workers'" in result.output
    assert detail in result.output
    assert launch_env == {}


@pytest.mark.parametrize(("command", "launcher"), _LAUNCHERS)
def test_without_project_nothing_is_resolved(
    command: str, launcher: str, launch_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "omnigent.cli._resolve_launch_project_id",
        lambda **_: pytest.fail("a launch without --project must not resolve a project"),
    )

    result = CliRunner().invoke(cli, [command])

    assert result.exit_code == 0, result.output
    assert launch_env["project_id"] is None


@pytest.mark.parametrize("command", ["claude", "codex"])
@pytest.mark.parametrize("routing_args", [[], ["--smart-routing"]])
@pytest.mark.parametrize(
    "resume_args",
    [["--resume", "conv_old"], ["--resume"], ["--session", "conv_old"], ["-r", "conv_old"]],
)
def test_project_with_a_resume_is_rejected_before_the_backend(
    command: str,
    routing_args: list[str],
    resume_args: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.cli._ensure_backend",
        lambda *_: pytest.fail("invalid args must not start the backend"),
    )

    result = CliRunner().invoke(
        cli, [command, *routing_args, "--project", "factory workers", *resume_args]
    )

    assert result.exit_code == 2, result.output
    assert "--project files a new session, so it cannot be combined with --resume" in result.output


@pytest.mark.parametrize("command", ["claude", "codex"])
@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("   ", "the project name must not be empty"),
        ("x" * 101, "the project name must be at most 100 characters"),
    ],
)
def test_an_invalid_project_name_is_rejected_before_the_backend(
    command: str, name: str, message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "omnigent.cli._ensure_backend",
        lambda *_: pytest.fail("invalid args must not start the backend"),
    )

    result = CliRunner().invoke(cli, [command, "--project", name])

    assert result.exit_code == 2, result.output
    assert message in result.output


@respx.mock
@pytest.mark.parametrize(("command", "launcher"), _LAUNCHERS)
@pytest.mark.parametrize(
    ("arm_response", "launched_session"),
    [
        (httpx.Response(201, json={"id": "conv_routed"}), "conv_routed"),
        # A rejected arm falls back to a fresh session, which must still be filed.
        (httpx.Response(500, json={"detail": "routing unavailable"}), None),
    ],
)
def test_smart_routing_files_the_armed_session_in_the_project(
    command: str,
    launcher: str,
    arm_response: httpx.Response,
    launched_session: str | None,
    launch_env: dict[str, Any],
) -> None:
    _mock_smart_routing(enabled=True)
    _mock_projects({"id": _PROJECT_ID, "name": "factory workers"})
    armed = respx.post(f"{_BASE}/v1/sessions").mock(return_value=arm_response)

    result = CliRunner().invoke(cli, [command, "--smart-routing", "--project", "factory workers"])

    assert result.exit_code == 0, result.output
    payload = json.loads(armed.calls.last.request.content)
    assert payload["project_id"] == _PROJECT_ID
    # The launch cwd stays the workspace, so the project's folder defaults don't apply.
    assert payload["workspace"] == str(Path.cwd().resolve())
    assert payload["git"] is None
    assert launch_env["session_id"] == launched_session
    assert launch_env["project_id"] == _PROJECT_ID


@respx.mock
@pytest.mark.parametrize(("command", "launcher"), _LAUNCHERS)
@pytest.mark.parametrize("routing_args", [[], ["--smart-routing"]])
@pytest.mark.parametrize(
    ("version_response", "message"),
    [
        (
            httpx.Response(200, json={"version": "0.21.0"}),
            "This Omnigent server (0.21.0) can't file new sessions in a project",
        ),
        (httpx.Response(500, text="unavailable"), "Could not use project 'factory workers'"),
    ],
)
def test_an_older_or_unreachable_server_stops_the_launch_before_any_write(
    command: str,
    launcher: str,
    routing_args: list[str],
    version_response: httpx.Response,
    message: str,
    launch_env: dict[str, Any],
) -> None:
    _mock_smart_routing(enabled=True)
    respx.get(f"{_BASE}/api/version").mock(return_value=version_response)
    projects = respx.route(url__startswith=f"{_BASE}/v1/projects")
    sessions = respx.route(url__startswith=f"{_BASE}/v1/sessions")

    result = CliRunner().invoke(cli, [command, *routing_args, "--project", "factory workers"])

    assert result.exit_code == 1, result.output
    assert message in result.output
    assert not projects.called
    assert not sessions.called
    assert launch_env == {}


@respx.mock
@pytest.mark.parametrize(("command", "launcher"), _LAUNCHERS)
def test_unavailable_smart_routing_stops_the_launch_before_the_project_is_created(
    command: str, launcher: str, launch_env: dict[str, Any]
) -> None:
    _mock_smart_routing(enabled=False)
    versions = _mock_server_version()
    projects = respx.route(url__startswith=f"{_BASE}/v1/projects")

    result = CliRunner().invoke(cli, [command, "--smart-routing", "--project", "factory workers"])

    assert result.exit_code == 1, result.output
    assert "Smart Routing is not enabled" in result.output
    assert not versions.called
    assert not projects.called
    assert launch_env == {}


@respx.mock
@pytest.mark.parametrize("reported", ["0.22.0", "0.22.1", "0.23.0.dev0"])
async def test_a_project_aware_server_version_is_accepted(reported: str) -> None:
    _mock_server_version(reported)

    async with httpx.AsyncClient(base_url=_BASE) as client:
        await _require_project_aware_session_create(client)


@respx.mock
@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"version": "0.21.0"}, "This Omnigent server (0.21.0) can't file new sessions"),
        ({"version": "0.22.0.dev20261110"}, "needs server 0.22.0 or later"),
        ({}, "this server reported None"),
        ({"version": "unknown"}, "this server reported 'unknown'"),
        (["0.22.0"], "this server reported None"),
    ],
)
async def test_an_older_or_unversioned_server_is_refused(payload: object, message: str) -> None:
    respx.get(f"{_BASE}/api/version").mock(return_value=httpx.Response(200, json=payload))

    async with httpx.AsyncClient(base_url=_BASE) as client:
        with pytest.raises(click.ClickException, match=re.escape(message)):
            await _require_project_aware_session_create(client)
