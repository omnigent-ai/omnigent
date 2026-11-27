"""The Slack watcher follows the project's link on the managed scheduled-task API.

The fake service checks every body against the managed server's own request
models and schedule rules, so these tests fail if the client drifts from them.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from omnigent.db.utils import builtin_agent_id
from omnigent.server.routes.scheduled_tasks import (
    CreateScheduledTaskRequest,
    UpdateScheduledTaskRequest,
)
from omnigent.server.scheduled.rrule import validate_rrule
from omnigent.server.slack_provisioning import (
    SlackAutomationConfig,
    SlackProvisioner,
    relay_key_hash,
    schedule_rrule,
)
from omnigent.stores.project_slack_store import ProjectSlackStore

API = "https://automation.example.com/api/2.0/omnigent"
TASKS = "/api/2.0/omnigent/v1/scheduled-tasks"
CONFIG = SlackAutomationConfig(api_url=API, relay_url="https://factory.example.com")


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


class _ManagedApi:
    """A managed Omnigent scheduled-task API that remembers its tasks."""

    def __init__(self, status: int = 200) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.tasks: dict[str, dict] = {}
        self.status = status
        self.drop_create_answer = False
        self.detail = "nope"

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        path = request.url.path
        self.calls.append((request.method, path, body))
        assert request.headers["Authorization"] == "Bearer owner-token"
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": self.detail})
        if path == TASKS and request.method == "GET":
            return httpx.Response(200, json={"scheduled_tasks": list(self.tasks.values())})
        if path == TASKS and request.method == "POST":
            try:
                create = CreateScheduledTaskRequest.model_validate(body)
            except ValidationError as exc:
                detail = [{"loc": ["body", *e["loc"]], "msg": e["msg"]} for e in exc.errors()]
                return httpx.Response(422, json={"detail": detail})
            validate_rrule(
                create.rrule,
                session_mode=create.session_mode,
                execution_target=create.execution_target,
            )
            task = {**create.model_dump(), "id": uuid.uuid4().hex, "state": "active"}
            self.tasks[task["id"]] = task
            if self.drop_create_answer:
                self.drop_create_answer = False
                return httpx.Response(502, text="lost")
            return httpx.Response(200, json=task)
        task_id = path.removeprefix(TASKS + "/")
        if task_id not in self.tasks:
            return httpx.Response(404, json={"detail": "Scheduled task not found"})
        if request.method == "PATCH":
            UpdateScheduledTaskRequest.model_validate(body)
            self.tasks[task_id].update(body)
            return httpx.Response(200, json=self.tasks[task_id])
        if request.method == "DELETE":
            del self.tasks[task_id]
            return httpx.Response(200, json={"id": task_id, "deleted": True})
        return httpx.Response(405)


def _setup(
    db_uri: str,
    *,
    status: int = 200,
    connection: _Connection | None = _Connection(),
    channel_id: str = "C0001",
    config: SlackAutomationConfig = CONFIG,
    destination: dict | None = None,
):
    store = ProjectSlackStore(db_uri)
    service = _ManagedApi(status)
    provisioner = SlackProvisioner(
        config,
        store,
        connection_store=_Connections(connection),
        connection_client=SimpleNamespace(),
        transport=httpx.MockTransport(service.handler),
    )
    project = SimpleNamespace(
        id=uuid.uuid4().hex, name="zz-slack (throwaway)", user_id="ann.lee@example.com"
    )
    binding = store.claim(
        project.id,
        slack_team_id="T01",
        channel_id=channel_id,
        channel_name="eng",
        destination=destination or {},
        owner_user_id=project.user_id,
        actor_user_id=project.user_id,
    )
    return store, service, provisioner, project, binding


def _run(coroutine):
    return asyncio.run(coroutine)


def test_create_sends_the_managed_contract(db_uri: str) -> None:
    store, service, provisioner, project, binding = _setup(db_uri)
    active = _run(provisioner.provision(project, binding, actor_user_id=project.user_id))
    assert active.state == "active", active.last_error
    method, path, body = service.calls[-1]
    assert (method, path) == ("POST", TASKS)
    assert "scheduled_task" not in body and "spec" not in body
    assert body["name"].startswith("[omnigent-slack] zz-slack (throwaway)")
    assert body["rrule"] == "FREQ=MINUTELY;INTERVAL=10"
    assert body["agent_id"] == builtin_agent_id("claude-native-ui")
    assert (body["execution_target"], body["session_mode"]) == ("managed_sandbox", "continue")
    # No MCP selection: the automation worker's own Slack configuration is used.
    assert "mcp_services" not in body
    assert active.automation_id == next(iter(service.tasks))
    assert "https://factory.example.com/v1/slack-relay/" in body["prompt"]
    # Only the hash is stored; the key is in the watcher's instructions.
    key = body["prompt"].split("X-Omnigent-Relay-Key: ", 1)[1].split("`", 1)[0]
    assert len(key) >= 40 and store.relay_binding(binding.id, relay_key_hash(key)) is not None


def _relay_key(body: dict) -> str:
    return body["prompt"].split("X-Omnigent-Relay-Key: ", 1)[1].split("`", 1)[0]


def test_retry_updates_the_same_watcher_and_rotates_the_key(db_uri: str) -> None:
    store, service, provisioner, project, binding = _setup(db_uri)
    active = _run(provisioner.provision(project, binding, actor_user_id=None))
    old_key = _relay_key(service.calls[-1][2])
    again = _run(provisioner.provision(project, active, actor_user_id=None))
    assert again.state == "active" and again.automation_id == active.automation_id
    assert len(service.tasks) == 1
    method, path, body = service.calls[-1]
    assert (method, path, body["state"]) == ("PATCH", f"{TASKS}/{active.automation_id}", "active")
    assert store.relay_binding(binding.id, relay_key_hash(old_key)) is None
    assert store.relay_binding(binding.id, relay_key_hash(_relay_key(body))) is not None


def test_a_lost_create_answer_is_found_instead_of_duplicated(db_uri: str) -> None:
    _, service, provisioner, project, binding = _setup(db_uri)
    service.drop_create_answer = True
    failed = _run(provisioner.provision(project, binding, actor_user_id=None))
    assert failed.state == "error" and "502" in (failed.last_error or "")
    assert len(service.tasks) == 1
    retried = _run(provisioner.provision(project, failed, actor_user_id=None))
    assert retried.state == "active" and len(service.tasks) == 1
    assert retried.automation_id == next(iter(service.tasks))


def test_a_watcher_deleted_by_hand_is_recreated(db_uri: str) -> None:
    _, service, provisioner, project, binding = _setup(db_uri)
    active = _run(provisioner.provision(project, binding, actor_user_id=None))
    service.tasks.clear()
    again = _run(provisioner.provision(project, active, actor_user_id=None))
    assert again.state == "active" and again.automation_id != active.automation_id
    assert len(service.tasks) == 1


def test_pause_resume_and_unlink(db_uri: str) -> None:
    _, service, provisioner, project, binding = _setup(db_uri)
    active = _run(provisioner.provision(project, binding, actor_user_id=None))
    task_id = active.automation_id
    paused = _run(provisioner.pause(project, active, actor_user_id=None))
    assert paused.state == "disabled" and service.tasks[task_id]["state"] == "paused"
    assert service.calls[-1] == ("PATCH", f"{TASKS}/{task_id}", {"state": "paused"})
    resumed = _run(provisioner.provision(project, paused, actor_user_id=None))
    assert resumed.state == "active" and service.tasks[task_id]["state"] == "active"
    _run(provisioner.remove(project, resumed, actor_user_id=None))
    assert service.calls[-1][:2] == ("DELETE", f"{TASKS}/{task_id}") and not service.tasks


def test_refusals_are_explained_and_never_reported_active(db_uri: str) -> None:
    _, _, provisioner, project, binding = _setup(db_uri, status=422)
    failed = _run(provisioner.provision(project, binding, actor_user_id=None))
    assert failed.state == "error" and "422" in (failed.last_error or "")
    assert "nope" in (failed.last_error or "")
    _, _, provisioner, project, binding = _setup(db_uri, status=401, channel_id="C0003")
    assert _run(provisioner.provision(project, binding, actor_user_id=None)).state == (
        "needs_reconnect"
    )


def test_the_old_wrapped_body_is_what_the_managed_api_refuses() -> None:
    with pytest.raises(ValidationError):
        CreateScheduledTaskRequest.model_validate({"scheduled_task": {"spec": {}}})


def test_missing_or_mismatched_connection_needs_reconnect(db_uri: str) -> None:
    _, service, provisioner, project, binding = _setup(db_uri, connection=None)
    failed = _run(provisioner.provision(project, binding, actor_user_id=None))
    assert failed.state == "needs_reconnect" and "connect Databricks" in (failed.last_error or "")
    other = _Connection(workspace_host="https://other.example.com")
    _, service, provisioner, project, binding = _setup(db_uri, connection=other, channel_id="C02")
    assert _run(provisioner.provision(project, binding, actor_user_id=None)).state == (
        "needs_reconnect"
    )
    assert service.calls == []


def test_undeletable_watcher_is_audited(db_uri: str) -> None:
    store, service, provisioner, project, binding = _setup(db_uri)
    active = _run(provisioner.provision(project, binding, actor_user_id=None))
    service.status = 500
    _run(provisioner.remove(project, active, actor_user_id=None))
    assert "automation_orphaned" in [entry.action for entry in store.audit(project.id)]


def test_overlapping_watchers_are_named(db_uri: str) -> None:
    _, service, provisioner, project, binding = _setup(db_uri)
    active = _run(provisioner.provision(project, binding, actor_user_id=None))
    service.tasks["other"] = {"id": "other", "name": "hand-made", "prompt": "watch C0001"}
    assert _run(provisioner.overlaps(project, active)) == ["hand-made"]


def test_link_settings_override_the_app_defaults(db_uri: str) -> None:
    agent = "0123456789abcdef0123456789abcdef"
    _, service, provisioner, project, binding = _setup(
        db_uri,
        config=SlackAutomationConfig(),
        destination={
            "automation_url": API,
            "relay_url": "https://factory.example.com",
            "agent": agent,
            "mcp_services": "slack, github",
            "model": "m1",
        },
    )
    assert _run(provisioner.provision(project, binding, actor_user_id=None)).state == "active"
    body = service.calls[-1][2]
    assert (body["agent_id"], body["mcp_services"], body["model_override"]) == (
        agent,
        ["slack", "github"],
        "m1",
    )


def test_a_link_without_an_automation_url_waits(db_uri: str) -> None:
    _, service, provisioner, project, binding = _setup(
        db_uri, config=SlackAutomationConfig(), destination={"relay_url": "https://f.example.com"}
    )
    waiting = _run(provisioner.provision(project, binding, actor_user_id=None))
    assert waiting.state == "claimed" and "automation API URL" in (waiting.last_error or "")
    assert service.calls == []


def test_schedules_become_rrules() -> None:
    assert schedule_rrule("") == "FREQ=MINUTELY;INTERVAL=10"
    assert schedule_rrule("*/15 * * * *") == "FREQ=MINUTELY;INTERVAL=15"
    assert schedule_rrule("RRULE:FREQ=HOURLY") == "FREQ=HOURLY"


def test_automation_url_must_be_the_api_not_the_tasks_page() -> None:
    from omnigent.errors import OmnigentError
    from omnigent.server.routes.project_slack import clean_automation_url

    assert clean_automation_url(API + "/") == API
    assert clean_automation_url(API + "/v1/scheduled-tasks") == API
    for bad in (
        "https://automation.example.com/omnigent/tasks?o=123",
        "http://automation.example.com/api/2.0/omnigent",
        "https://automation.example.com/api/2.0/omnigent?o=1",
    ):
        with pytest.raises(OmnigentError):
            clean_automation_url(bad)


class _HostConnection:
    def __init__(self, owner: str) -> None:
        self.owner = owner
        self.pending_automation_requests: dict = {}


class _HostRegistry:
    """Answers each host.automation_request frame from a fake automation API."""

    def __init__(self, connection: _HostConnection, service: _ManagedApi) -> None:
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
        transport=httpx.MockTransport(service.handler),
        host_registry=registry,
        conversations=conversations,
    )
    return store, service, registry, provisioner, project, binding


def test_provisions_through_the_lead_host_without_a_server_credential(db_uri: str) -> None:
    _, service, registry, provisioner, project, binding = _via_host(
        db_uri, host_owner="ann.lee@example.com"
    )
    active = _run(provisioner.provision(project, binding, actor_user_id=None))
    assert active.state == "active", active.last_error
    assert [frame.url for frame in registry.frames] == [
        f"https://automation.example.com{TASKS}",
        f"https://automation.example.com{TASKS}",
    ]
    paused = _run(provisioner.pause(project, active, actor_user_id=None))
    assert paused.state == "disabled"
    assert [call[0] for call in service.calls] == ["GET", "POST", "PATCH"]


def test_a_lead_host_owned_by_someone_else_is_not_used(db_uri: str) -> None:
    _, service, registry, provisioner, project, binding = _via_host(
        db_uri, host_owner="mallory@example.com"
    )
    failed = _run(provisioner.provision(project, binding, actor_user_id=None))
    assert failed.state == "needs_reconnect"
    assert registry.frames == [] and service.calls == []


def test_a_403_setup_problem_is_not_reported_as_an_expired_login(db_uri: str) -> None:
    _, service, provisioner, project, binding = _setup(db_uri, status=403)
    service.detail = "MCP registry is not configured"
    failed = _run(provisioner.provision(project, binding, actor_user_id=None))
    assert failed.state == "error"
    assert "MCP registry is not configured" in (failed.last_error or "")
    assert "not an expired login" in (failed.last_error or "")
    _, service, provisioner, project, binding = _setup(db_uri, status=403, channel_id="C0403")
    service.detail = "Token expired"
    assert _run(provisioner.provision(project, binding, actor_user_id=None)).state == (
        "needs_reconnect"
    )


def test_update_clears_services_an_earlier_version_chose(db_uri: str) -> None:
    _, service, provisioner, project, binding = _setup(db_uri)
    active = _run(provisioner.provision(project, binding, actor_user_id=None))
    service.tasks[active.automation_id]["mcp_services"] = ["slack"]
    _run(provisioner.provision(project, active, actor_user_id=None))
    assert service.calls[-1][2]["mcp_services"] == []


def test_the_watcher_link_opens_its_page_on_the_managed_server(db_uri: str) -> None:
    _, _, provisioner, project, binding = _setup(db_uri)
    assert provisioner.config.automation_link(binding) is None
    active = _run(provisioner.provision(project, binding, actor_user_id=None))
    assert provisioner.config.automation_link(active) == (
        f"https://automation.example.com/omnigent/tasks/{active.automation_id}"
    )
