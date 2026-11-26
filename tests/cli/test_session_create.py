"""Tests for ``omnigent session create``: a session from a script, with no terminal."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx
from click.testing import CliRunner

from omnigent.cli import cli
from omnigent.db.utils import builtin_agent_id
from omnigent.native.native_coding_agents import CLAUDE_NATIVE_AGENT_NAME

_BASE = "http://localhost:6767"
_LOCAL_HOST = "host_local"
_SESSION = "conv_worker"


@dataclass
class _FakeServer:
    router: respx.MockRouter
    routed_host_ids: list[str | None] = field(default_factory=list)

    def create(self, status: int = 201, body: dict | None = None) -> respx.Route:
        return self.router.post(f"{_BASE}/v1/sessions").mock(
            return_value=httpx.Response(
                status, json=body if body is not None else {"id": _SESSION}
            )
        )

    def message(self, status: int = 202, body: dict | None = None) -> respx.Route:
        return self.router.post(f"{_BASE}/v1/sessions/{_SESSION}/events").mock(
            return_value=httpx.Response(
                status, json=body if body is not None else {"queued": True, "item_id": "item_1"}
            )
        )

    def host_status(self, statuses: list[str]) -> respx.Route:
        return self.router.get(f"{_BASE}/v1/hosts/{_LOCAL_HOST}").mock(
            side_effect=[
                httpx.Response(200, json={"host_id": _LOCAL_HOST, "status": status})
                for status in statuses
            ]
        )

    def project(self, config: dict) -> respx.Route:
        return self.router.get(f"{_BASE}/v1/projects/proj_1").mock(
            return_value=httpx.Response(200, json={"id": "proj_1", "config": config})
        )


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeServer]:
    """Run against a fake server and record the host each request is routed to."""
    host_ids: list[str | None] = []

    def _headers(server_url: str | None = None, *, host_id: str | None, **_: object):
        host_ids.append(host_id)
        return {"X-Routed-Host": host_id or ""}

    # No local-server pidfile, so the printed URL has no --base-path prefix even when
    # another test in the worker left one behind.
    monkeypatch.setattr("omnigent.host.local_server._read_local_server_pid_file", lambda: None)
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    monkeypatch.setattr("omnigent.cli._ensure_backend", lambda server: _BASE)
    monkeypatch.setattr("omnigent.cli._resolve_attach_server", lambda server, configured: _BASE)
    monkeypatch.setattr("omnigent.chat._remote_headers", _headers)
    monkeypatch.setattr(
        "omnigent.host.identity.load_or_create_host_identity",
        lambda: SimpleNamespace(host_id=_LOCAL_HOST),
    )
    with respx.mock(assert_all_called=False) as router:
        fake = _FakeServer(router, host_ids)
        fake.host_status(["online"])
        yield fake


def _invoke(args: list[str], **kwargs: object):
    return CliRunner().invoke(cli, ["session", "create", *args], **kwargs)


def test_creates_a_native_session_on_this_host_and_queues_the_task(
    server: _FakeServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The script gets only the session id on stdout, after the task is queued."""
    monkeypatch.chdir(tmp_path)
    create = server.create()
    message = server.message()

    result = _invoke(
        [
            "--agent",
            "claude",
            "--title",
            "Fix flaky test",
            "--model",
            "claude-sonnet-4-6",
            "--reasoning-effort",
            "high",
            "--permission-mode",
            "auto",
            "--message-file",
            "-",
        ],
        input="Fix tests/test_x.py\n",
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == f"{_SESSION}\n"
    assert json.loads(create.calls.last.request.content) == {
        "agent_id": builtin_agent_id(CLAUDE_NATIVE_AGENT_NAME),
        "host_type": "external",
        "host_id": _LOCAL_HOST,
        "workspace": str(tmp_path.resolve()),
        "title": "Fix flaky test",
        "model_override": "claude-sonnet-4-6",
        "reasoning_effort": "high",
        "permission_mode": "auto",
    }
    assert json.loads(message.calls.last.request.content) == {
        "type": "message",
        "data": {
            "role": "user",
            "content": [{"type": "input_text", "text": "Fix tests/test_x.py\n"}],
        },
    }
    # Every call reaches the replica holding this host's tunnel.
    assert set(server.routed_host_ids) == {_LOCAL_HOST}
    assert create.calls.last.request.headers["X-Routed-Host"] == _LOCAL_HOST
    assert message.calls.last.request.headers["X-Routed-Host"] == _LOCAL_HOST


def test_waits_for_this_host_to_connect_before_creating(server: _FakeServer) -> None:
    """A just-started host daemon registers asynchronously; creating sooner is refused."""
    host = server.host_status(["offline", "online"])
    create = server.create()

    result = _invoke(["--agent", "claude"])

    assert result.exit_code == 0, result.output
    assert host.call_count == 2
    assert create.called


def test_a_project_supplies_its_saved_agent_and_workspace(
    server: _FakeServer,
) -> None:
    project = server.project({"agent_id": "ag_saved", "workspace": "/repo"})
    create = server.create()

    result = _invoke(["--project-id", "proj_1", "--json"])

    assert result.exit_code == 0, result.output
    assert project.called
    # Absent fields let the server apply the project's saved defaults.
    assert json.loads(create.calls.last.request.content) == {
        "host_type": "external",
        "host_id": _LOCAL_HOST,
        "project_id": "proj_1",
    }
    assert json.loads(result.stdout) == {
        "session_id": _SESSION,
        "url": f"{_BASE}/c/{_SESSION}",
        "message_queued": False,
    }


def test_a_project_without_a_saved_workspace_works_in_the_current_directory(
    server: _FakeServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    server.project({})
    create = server.create()
    server.message()

    result = _invoke(["--agent", "codex", "--project-id", "proj_1", "-m", "Triage"])

    assert result.exit_code == 0, result.output
    body = json.loads(create.calls.last.request.content)
    assert body["workspace"] == str(tmp_path.resolve())
    assert body["project_id"] == "proj_1"


def test_another_host_takes_the_workspace_as_given(
    server: _FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No local host daemon starts for a session that runs elsewhere."""

    def _no_local_host(server_url: str | None) -> str:
        raise AssertionError("a session on another host must not start this machine's host")

    monkeypatch.setattr("omnigent.cli._ensure_backend", _no_local_host)
    create = server.create()
    message = server.message()

    result = _invoke(
        [
            *("--agent-id", "ag_custom", "--host-id", "host_remote"),
            *("--workspace", "/srv/repo", "-m", "Go"),
        ]
    )

    assert result.exit_code == 0, result.output
    body = json.loads(create.calls.last.request.content)
    assert (body["agent_id"], body["host_id"], body["workspace"]) == (
        "ag_custom",
        "host_remote",
        "/srv/repo",
    )
    assert server.routed_host_ids == ["host_remote"]
    assert message.calls.last.request.headers["X-Routed-Host"] == "host_remote"


def test_another_host_needs_a_server_to_reach_it(
    server: _FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("omnigent.cli._resolve_attach_server", lambda server, configured: None)

    result = _invoke(["--agent", "claude", "--host-id", "host_remote", "--workspace", "/srv"])

    assert result.exit_code == 1
    assert "No Omnigent server to create the session on" in result.stderr


def test_a_refused_create_reports_the_server_reason_and_sends_nothing(
    server: _FakeServer,
) -> None:
    server.create(400, {"error": {"code": "invalid_input", "message": "Host is offline"}})
    message = server.message()

    result = _invoke(["--agent", "claude", "-m", "Go"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "The server refused the session (400): Host is offline" in result.stderr
    assert not message.called


@pytest.mark.parametrize(
    ("ack_status", "ack", "reason"),
    [
        (202, {"queued": False, "denied": True}, "a policy denied its message"),
        (202, {"queued": False}, "did not queue its message"),
        (409, {"error": {"message": "Session is archived"}}, "Session is archived"),
    ],
)
def test_a_session_whose_task_was_not_queued_is_still_named(
    server: _FakeServer, ack_status: int, ack: dict, reason: str
) -> None:
    """A half-created worker is printed so the script can clean it up."""
    server.create()
    server.message(ack_status, ack)

    result = _invoke(["--agent", "claude", "-m", "Go"])

    assert result.exit_code == 1
    assert result.stdout == f"{_SESSION}\n"
    assert f"Session {_SESSION} was created" in result.stderr
    assert reason in result.stderr


@pytest.mark.parametrize(
    ("args", "error"),
    [
        (["-m", "Go"], "Pass --agent (e.g. claude) or --agent-id."),
        (["--agent", "claude", "--agent-id", "ag_1"], "Pass --agent or --agent-id, not both."),
        (["--agent", "not-an-agent"], "Unknown agent 'not-an-agent'"),
        (["--agent", "claude", "--host-id", "host_remote"], "--host-id needs --workspace"),
        (
            ["--agent", "claude", "--host-id", "host_remote", "--workspace", "repo"],
            "--workspace must be an absolute path",
        ),
        (["--agent", "claude", "-m", "  "], "The first message is empty."),
    ],
)
def test_bad_arguments_fail_before_contacting_the_server(
    server: _FakeServer, args: list[str], error: str
) -> None:
    create = server.create()

    result = _invoke(args)

    assert result.exit_code == 2
    assert error in result.stderr
    assert not create.called
    assert server.routed_host_ids == []
