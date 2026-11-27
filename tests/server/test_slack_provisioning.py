"""The Slack watcher automation follows the project's link: create, update, pause, delete."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from types import SimpleNamespace

import httpx

from omnigent.server.slack_provisioning import (
    AutomationClient,
    SlackAutomationConfig,
    SlackProvisioner,
    relay_key_hash,
)
from omnigent.stores.project_slack_store import ProjectSlackStore

CONFIG = SlackAutomationConfig(
    api_url="https://automation.example.com/api/2.0/agent",
    relay_url="https://factory.example.com",
    org="example-org",
    repo="example-repo",
)


@dataclass
class _Connection:
    access_token: str = "owner-token"
    workspace_host: str = "https://automation.example.com"
    refresh_token: str | None = None
    token_expires_at: int | None = None


class _Connections:
    def __init__(self, connection: _Connection | None) -> None:
        self.connection = connection

    def get(self, user_id: str, *, with_tokens: bool = False) -> _Connection | None:
        return self.connection


class _FakeService:
    def __init__(self, status: int = 200) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.status = status

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, request.url.path, body))
        assert request.headers["Authorization"] == "Bearer owner-token"
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "nope"})
        if request.method == "POST" and request.url.path.endswith("/scheduled-tasks"):
            task = body["scheduled_task"]
            return httpx.Response(200, json={"schedule_id": "s-1", "username": task["username"]})
        return httpx.Response(200, json={})


def _setup(
    db_uri: str,
    *,
    status: int = 200,
    connection: _Connection | None = _Connection(),
    channel_id: str = "C0001",
):
    store = ProjectSlackStore(db_uri)
    service = _FakeService(status)
    provisioner = SlackProvisioner(
        CONFIG,
        store,
        connection_store=_Connections(connection),
        connection_client=SimpleNamespace(),
        client=AutomationClient(CONFIG.api_url, transport=httpx.MockTransport(service.handler)),
    )
    project = SimpleNamespace(
        id=uuid.uuid4().hex, name="zz-slack (throwaway)", user_id="ann.lee@example.com"
    )
    binding = store.claim(
        project.id,
        slack_team_id="T01",
        channel_id=channel_id,
        channel_name="eng",
        destination={},
        owner_user_id=project.user_id,
        actor_user_id=project.user_id,
    )
    return store, service, provisioner, project, binding


def test_provision_creates_once_then_updates(db_uri: str) -> None:
    store, service, provisioner, project, binding = _setup(db_uri)
    active = asyncio.run(provisioner.provision(project, binding, actor_user_id=project.user_id))
    assert active.state == "active"
    assert active.automation_id == "ann_lee/s-1"
    method, path, body = service.calls[0]
    assert (method, path) == ("POST", "/api/2.0/agent/scheduled-tasks")
    task = body["scheduled_task"]
    assert task["idempotency_key"] == binding.id
    assert task["spec"]["slack_channel_id"] == "C0001"
    # Only the hash is stored; the key is in the watcher's instructions.
    key = task["spec"]["prompt"].split("X-Omnigent-Relay-Key: ", 1)[1].split("`", 1)[0]
    assert len(key) >= 40 and active.has_relay_key
    assert store.relay_binding(binding.id, relay_key_hash(key)) is not None

    asyncio.run(provisioner.provision(project, active, actor_user_id=project.user_id))
    assert [call[:2] for call in service.calls[1:]] == [
        ("PATCH", "/api/2.0/agent/scheduled-tasks/ann_lee/s-1"),
        ("POST", "/api/2.0/agent/scheduled-tasks/ann_lee/s-1/state"),
    ]
    assert service.calls[2][2]["state"] == "SCHEDULED_TASK_STATE_ACTIVE"
    # Re-provisioning rotates the key, so the old one stops working.
    assert store.relay_binding(binding.id, relay_key_hash(key)) is None


def test_pause_and_remove(db_uri: str) -> None:
    _, service, provisioner, project, binding = _setup(db_uri)
    active = asyncio.run(provisioner.provision(project, binding, actor_user_id=None))
    paused = asyncio.run(provisioner.pause(project, active, actor_user_id=None))
    assert paused.state == "disabled"
    assert service.calls[-1][2]["state"] == "SCHEDULED_TASK_STATE_PAUSED"
    asyncio.run(provisioner.remove(project, paused, actor_user_id=None))
    assert service.calls[-1][:2] == ("DELETE", "/api/2.0/agent/scheduled-tasks/ann_lee/s-1")


def test_refused_or_missing_connection_needs_reconnect(db_uri: str) -> None:
    _, _, provisioner, project, binding = _setup(db_uri, status=401)
    assert asyncio.run(provisioner.provision(project, binding, actor_user_id=None)).state == (
        "needs_reconnect"
    )
    _, service, provisioner, project, binding = _setup(db_uri, connection=None, channel_id="C0002")
    failed = asyncio.run(provisioner.provision(project, binding, actor_user_id=None))
    assert failed.state == "needs_reconnect"
    assert "connect Databricks" in (failed.last_error or "")
    assert service.calls == []


def test_connection_to_another_workspace_is_refused(db_uri: str) -> None:
    other = _Connection(workspace_host="https://other.example.com")
    _, service, provisioner, project, binding = _setup(db_uri, connection=other)
    failed = asyncio.run(provisioner.provision(project, binding, actor_user_id=None))
    assert failed.state == "needs_reconnect"
    assert service.calls == []


def test_undeletable_watcher_is_audited(db_uri: str) -> None:
    store, service, provisioner, project, binding = _setup(db_uri)
    active = asyncio.run(provisioner.provision(project, binding, actor_user_id=None))
    service.status = 500
    asyncio.run(provisioner.remove(project, active, actor_user_id=None))
    assert "automation_orphaned" in [entry.action for entry in store.audit(project.id)]


class _HostConnection:
    def __init__(self, owner: str) -> None:
        self.owner = owner
        self.pending_automation_requests: dict = {}


class _HostRegistry:
    """Answers each host.automation_request frame from a fake automation API."""

    def __init__(self, connection: _HostConnection, service: _FakeService) -> None:
        self.connection = connection
        self.service = service
        self.frames: list = []

    def get(self, host_id: str):
        return self.connection if host_id == "host-1" else None

    def send_text(self, connection, text: str) -> None:
        from omnigent.host.automation_requests import run_automation_request
        from omnigent.host.frames import decode_host_frame

        frame = decode_host_frame(text)
        self.frames.append(frame)

        class _Bearer(httpx.Auth):
            def auth_flow(self, request):
                request.headers["Authorization"] = "Bearer owner-token"
                yield request

        result = run_automation_request(
            frame,
            resolve_auth=lambda host: (_Bearer(), host),
            transport=httpx.MockTransport(self.service.handler),
        )
        future = connection.pending_automation_requests[frame.request_id]
        future.set_result(
            {"status_code": result.status_code, "body": result.body, "error": result.error}
        )


def _via_host(db_uri: str, *, host_owner: str):
    store, service, _, project, binding = _setup(db_uri, connection=None, channel_id="C0H01")
    project.main_session_id = "lead-1"
    registry = _HostRegistry(_HostConnection(host_owner), service)
    conversations = SimpleNamespace(
        get_conversation=lambda session_id: SimpleNamespace(host_id="host-1")
    )
    provisioner = SlackProvisioner(
        CONFIG,
        store,
        connection_store=None,
        connection_client=None,
        host_registry=registry,
        conversations=conversations,
    )
    return store, service, registry, provisioner, project, binding


def test_provisions_through_the_lead_host_without_a_server_credential(db_uri: str) -> None:
    _, service, registry, provisioner, project, binding = _via_host(
        db_uri, host_owner="ann.lee@example.com"
    )
    active = asyncio.run(provisioner.provision(project, binding, actor_user_id=None))
    assert active.state == "active" and active.automation_id == "ann_lee/s-1"
    assert registry.frames[0].url == "https://automation.example.com/api/2.0/agent/scheduled-tasks"
    paused = asyncio.run(provisioner.pause(project, active, actor_user_id=None))
    assert paused.state == "disabled"
    assert [call[0] for call in service.calls] == ["POST", "POST"]


def test_a_lead_host_owned_by_someone_else_is_not_used(db_uri: str) -> None:
    _, service, registry, provisioner, project, binding = _via_host(
        db_uri, host_owner="mallory@example.com"
    )
    failed = asyncio.run(provisioner.provision(project, binding, actor_user_id=None))
    assert failed.state == "needs_reconnect"
    assert registry.frames == [] and service.calls == []


def test_a_link_needs_no_server_settings(db_uri: str) -> None:
    """The link names its automation URL; the relay URL is where it was saved from."""
    store = ProjectSlackStore(db_uri)
    service = _FakeService()
    provisioner = SlackProvisioner(
        SlackAutomationConfig(),
        store,
        connection_store=_Connections(_Connection()),
        connection_client=SimpleNamespace(),
        transport=httpx.MockTransport(service.handler),
    )
    project = SimpleNamespace(id=uuid.uuid4().hex, name="zz-slack (throwaway)", user_id="ann")
    claim = {
        "slack_team_id": None,
        "channel_id": "C0002",
        "channel_name": None,
        "owner_user_id": "ann",
        "actor_user_id": "ann",
    }
    relay = {"relay_url": "https://factory.example.com"}
    bare = store.claim(project.id, destination=relay, **claim)
    waiting = asyncio.run(provisioner.provision(project, bare, actor_user_id="ann"))
    assert waiting.state == "claimed" and "automation API URL" in (waiting.last_error or "")
    assert service.calls == []

    destination = {
        "automation_url": "https://automation.example.com/api/2.0/agent",
        "relay_url": "https://factory.example.com",
    }
    binding = store.claim(project.id, destination=destination, **claim)
    active = asyncio.run(provisioner.provision(project, binding, actor_user_id="ann"))
    assert active.state == "active"
    method, path, body = service.calls[0]
    assert (method, path) == ("POST", "/api/2.0/agent/scheduled-tasks")
    spec = body["scheduled_task"]["spec"]
    assert (spec["org"], spec["repo"]) == ("omnigent-ai", "omnigent")
    assert "https://factory.example.com/v1/slack-relay/" in spec["prompt"]


def test_automation_url_must_be_the_api_not_the_tasks_page() -> None:
    import pytest

    from omnigent.errors import OmnigentError
    from omnigent.server.routes.project_slack import clean_automation_url

    api = "https://automation.example.com/api/2.0/agent"
    assert clean_automation_url(api + "/") == api
    assert clean_automation_url(api + "/scheduled-tasks") == api
    for bad in (
        "https://automation.example.com/tasks/app?o=123",
        "http://automation.example.com/api/2.0/agent",
        "https://automation.example.com/api/2.0/agent?o=1",
    ):
        with pytest.raises(OmnigentError):
            clean_automation_url(bad)
