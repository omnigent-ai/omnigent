"""UI API lifecycle over the real scheduler and owner-scoped local checkpoints."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import yaml
from click.testing import CliRunner
from fastapi import FastAPI

from omnigent.db.utils import builtin_agent_id
from omnigent.native.native_coding_agents import CODEX_NATIVE_AGENT_NAME
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.routes.workflows import create_workflows_router, example_definition
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.workflows.__main__ import main
from omnigent.workflows.designs import WorkflowDesigns
from omnigent.workflows.models import Workflow
from omnigent.workflows.service import WorkflowService
from omnigent.workflows.state import ExecutionBinding


@pytest.fixture
def designs(tmp_path: Path, db_uri):
    agents = SqlAlchemyAgentStore(db_uri)
    agents.create(builtin_agent_id(CODEX_NATIVE_AGENT_NAME), CODEX_NATIVE_AGENT_NAME, "unused")
    return WorkflowDesigns(
        tmp_path / "designs",
        SqlAlchemyConversationStore(db_uri),
        agents,
        SqlAlchemyPermissionStore(db_uri),
    )


@pytest.fixture
async def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, designs):
    for key in ("OWNER", "SERVER_URL"):
        monkeypatch.delenv(f"OMNIGENT_WORKFLOW_{key}", raising=False)
    service = WorkflowService(tmp_path)
    app = FastAPI()
    app.include_router(
        create_workflows_router(
            service,
            UnifiedAuthProvider(
                "header", local_single_user=False, header_name="X-Forwarded-Email"
            ),
            designs,
        ),
        prefix="/v1",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Forwarded-Email": "owner@example.test"},
    ) as client:
        try:
            yield client, service
        finally:
            await service.shutdown()


async def test_ui_run_repairs_after_failure_and_releases_dependants(api):
    client, service = api
    response = await client.post(
        "/v1/workflows",
        json={"backend": "simulation", "definition": example_definition()},
    )
    assert response.status_code == 201, response.text
    root_id = response.json()["root_session_id"]
    workflow_id = response.json()["workflow_id"]
    assert root_id
    assert workflow_id
    run_id = response.json()["run_id"]
    path = f"/v1/workflows/{run_id}"
    await asyncio.gather(*service.tasks.values())
    first = (await client.get(path)).json()
    assert first["status"] == "waiting"
    assert first["nodes"]["summarize"]["status"] == "pending"
    session = first["nodes"]["propose"]["members"]["owner"]["session_id"]
    for round_number, passed in [(1, False), (2, True)]:
        event = {
            "kind": "gate_result",
            "id": f"check-{round_number}",
            "node_id": "propose",
            "candidate_id": f"propose-{round_number}",
            "gate_id": "review",
            "outcome": {"passed": passed, "detail": "Reviewed the evidence"},
        }
        assert (await client.post(path + "/events", json=event)).status_code == 202
        assert (await client.post(path + "/resume")).status_code == 200
        await asyncio.gather(*service.tasks.values())
        current = (await client.get(path)).json()
        assert current["nodes"]["propose"]["members"]["owner"]["session_id"] == session
    assert current["root_session_id"] == root_id
    assert current["workflow_id"] == workflow_id
    assert current["status"] == "succeeded"
    assert current["nodes"]["summarize"]["status"] == "succeeded"
    assert len(current["nodes"]["propose"]["candidates"]) == 2
    assert (await client.post(path + "/resume")).status_code == 409
    assert (await client.post(path + "/events", json=event)).status_code == 400
    assert (await client.get("/v1/workflows")).json()["data"][0]["run_id"] == run_id
    other = {"X-Forwarded-Email": "other@example.test"}
    assert (await client.get("/v1/workflows", headers=other)).json() == {"data": []}
    assert (await client.get(path, headers=other)).status_code == 404
    assert (await client.post(path + "/events", json=event, headers=other)).status_code == 404
    assert (await client.post(path + "/resume", headers=other)).status_code == 404
    assert (await client.post(path + "/stop", headers=other)).status_code == 404


async def test_backend_options_require_explicit_owner_and_configuration(api, monkeypatch):
    client, _service = api

    async def enabled():
        return {
            item["id"]: item["enabled"]
            for item in (await client.get("/v1/workflows/options")).json()["backends"]
        }

    assert await enabled() == {"simulation": True, "server": False}
    monkeypatch.setenv("OMNIGENT_WORKFLOW_SERVER_URL", "https://agents.example")
    assert await enabled() == {"simulation": True, "server": False}
    monkeypatch.setenv("OMNIGENT_WORKFLOW_OWNER", "owner@example.test")
    assert await enabled() == {"simulation": True, "server": True}
    response = await client.post(
        "/v1/workflows",
        headers={"X-Forwarded-Email": "other@example.test"},
        json={"backend": "server", "definition": example_definition()},
    )
    assert response.status_code == 400

    monkeypatch.setenv("OMNIGENT_WORKFLOW_SERVER_URL", "https://private-token@workspace.example")
    assert (await enabled())["server"] is False
    response = await client.post(
        "/v1/workflows", json={"backend": "server", "definition": example_definition()}
    )
    assert response.status_code == 400
    assert "private-token" not in response.text


@pytest.mark.parametrize("invalid", ["yaml", "cycle", "command", "placement"])
async def test_invalid_web_definition_starts_no_controller(api, monkeypatch, invalid):
    client, service = api
    definition = yaml.safe_load(example_definition())
    if invalid == "cycle":
        definition["nodes"][0]["depends_on"] = ["summarize"]
    elif invalid == "command":
        definition["nodes"][0]["gates"] = [
            {"id": "check", "kind": "command", "command": ["never-run"], "cwd": "/tmp"}
        ]
    backend = "simulation"
    if invalid == "placement":
        backend = "server"
        definition["teams"][0]["members"][0]["placement"]["provider"] = ""
        monkeypatch.setenv("OMNIGENT_WORKFLOW_SERVER_URL", "https://agents.example")
        monkeypatch.setenv("OMNIGENT_WORKFLOW_OWNER", "owner@example.test")
    response = await client.post(
        "/v1/workflows",
        json={
            "backend": backend,
            "definition": ": invalid: [" if invalid == "yaml" else yaml.safe_dump(definition),
        },
    )
    assert response.status_code == 400, response.text
    assert not service.tasks
    assert (await client.get("/v1/workflows")).json() == {"data": []}


@pytest.mark.parametrize("action", ["stop", "shutdown", "error"])
async def test_controller_lifecycle_retains_checkpoint(api, monkeypatch, action):
    client, service = api
    entered = asyncio.Event()

    @asynccontextmanager
    async def executor(_binding):
        async def execute(_request):
            entered.set()
            if action == "error":
                raise RuntimeError("provider unavailable")
            await asyncio.Event().wait()

        yield execute

    monkeypatch.setattr("omnigent.workflows.service.open_executor", executor)
    response = await client.post(
        "/v1/workflows",
        json={"backend": "simulation", "definition": example_definition()},
    )
    path = "/v1/workflows/" + response.json()["run_id"]
    await entered.wait()
    if action == "stop":
        assert (await client.post(path + "/resume")).status_code == 409
        assert (await client.post(path + "/stop")).status_code == 200
        assert (await client.post(path + "/stop")).status_code == 409
    elif action == "shutdown":
        await service.shutdown()
    else:
        await asyncio.gather(*service.tasks.values())
    assert not service.tasks
    saved = (await client.get(path)).json()
    assert saved["status"] == ("failed" if action == "error" else "cancelled")
    assert saved["active"] is False


async def test_web_controller_never_executes_command_gates(tmp_path, monkeypatch):
    from omnigent.workflows.simulation import simulate_member

    @asynccontextmanager
    async def executor(_binding):
        yield simulate_member

    monkeypatch.setattr("omnigent.workflows.service.open_executor", executor)
    definition = yaml.safe_load(example_definition())
    definition["nodes"][0]["gates"] = [
        {"id": "check", "kind": "command", "command": ["never-run"], "cwd": "/tmp"}
    ]
    service = WorkflowService(tmp_path)
    store = service.create(
        "owner", Workflow.model_validate(definition), ExecutionBinding(mode="server")
    )
    await asyncio.gather(*service.tasks.values())
    state = store.load()
    assert state["status"] == "failed"
    assert "Web workflows require external gates" in state["nodes"]["investigate"]["error"]


async def test_shutdown_before_controller_starts_releases_task(tmp_path):
    service = WorkflowService(tmp_path)
    store = service.create(
        "owner",
        Workflow.model_validate(yaml.safe_load(example_definition())),
        ExecutionBinding(mode="simulation"),
    )
    await service.shutdown()
    assert not service.tasks
    service.start(store)
    await asyncio.gather(*service.tasks.values())
    assert store.load()["status"] == "waiting"


async def test_designer_exports_valid_yaml_without_live_configuration(api, tmp_path):
    client, service = api
    definition = yaml.safe_load(example_definition())
    members = definition["teams"][0]["members"]
    members[0]["placement"]["provider"] = "primary-sandbox"
    members.append(
        {
            **members[0],
            "id": "reviewer",
            "placement": {
                "kind": "sandbox",
                "provider": "review-sandbox",
                "workspace": "https://github.com/example/review",
            },
        }
    )
    definition["name"] = 'Workflow: "quoted"\nwith another line'
    definition["nodes"][0]["name"] = "Investigate: evidence"
    definition["nodes"].append(
        {
            "id": "constraints",
            "name": "Review constraints",
            "team": "delivery",
            "goal": "Review risks",
            "depends_on": ["investigate"],
        }
    )
    definition["nodes"][2]["depends_on"].append("constraints")
    response = await client.post("/v1/workflows/definition", json={"workflow": definition})
    assert response.status_code == 200, response.text
    exported = response.json()["definition"]
    assert Workflow.model_validate(yaml.safe_load(exported)) == Workflow.model_validate(definition)
    assert not service.tasks
    assert (await client.get("/v1/workflows")).json() == {"data": []}
    path = tmp_path / "exported.yaml"
    path.write_text(exported)
    result = CliRunner().invoke(main, [str(path), "--validate"])
    assert result.exit_code == 0, result.output
    response = await client.post(
        "/v1/workflows", json={"backend": "simulation", "definition": exported}
    )
    assert response.status_code == 201, response.text
    await asyncio.gather(*service.tasks.values())
    saved = (await client.get("/v1/workflows/" + response.json()["run_id"])).json()
    assert saved["status"] == "waiting"
    assert saved["nodes"]["constraints"]["status"] == "succeeded"
    assert saved["nodes"]["summarize"]["status"] == "pending"


async def test_live_run_requires_server_connection_and_rechecks_it_on_resume(api, monkeypatch):
    from omnigent.workflows.simulation import simulate_member

    client, service = api
    bindings = []

    @asynccontextmanager
    async def executor(binding):
        bindings.append(binding)
        yield simulate_member

    monkeypatch.setattr("omnigent.workflows.service.open_executor", executor)
    monkeypatch.setenv("OMNIGENT_WORKFLOW_OWNER", "owner@example.test")
    request = {"backend": "server", "definition": example_definition()}
    assert (await client.post("/v1/workflows", json=request)).status_code == 400
    assert not service.tasks
    monkeypatch.setenv("OMNIGENT_WORKFLOW_SERVER_URL", "https://agents.example")
    assert (
        await client.post(
            "/v1/workflows", json=request, headers={"X-Forwarded-Email": "other@example.test"}
        )
    ).status_code == 400
    response = await client.post("/v1/workflows", json=request)
    assert response.status_code == 201, response.text
    path = "/v1/workflows/" + response.json()["run_id"]
    await asyncio.gather(*service.tasks.values())
    assert bindings == [ExecutionBinding(mode="server", server="https://agents.example")]
    monkeypatch.setenv("OMNIGENT_WORKFLOW_SERVER_URL", "https://changed.example")
    response = await client.post(path + "/resume")
    assert response.status_code == 409
    assert "connection changed" in response.text
    monkeypatch.setenv("OMNIGENT_WORKFLOW_SERVER_URL", "https://agents.example")
    assert (await client.post(path + "/resume")).status_code == 200
    await asyncio.gather(*service.tasks.values())
    assert bindings[1] == bindings[0]


@pytest.mark.parametrize("fault", ["cycle", "placement", "command", "empty-name", "oversized"])
async def test_designer_refuses_invalid_exports(api, fault):
    client, service = api
    definition = yaml.safe_load(example_definition())
    if fault == "cycle":
        definition["nodes"][0]["depends_on"] = ["summarize"]
    elif fault == "placement":
        definition["teams"][0]["members"][0]["placement"] = {
            "kind": "sandbox",
            "provider": "",
        }
    elif fault == "command":
        definition["nodes"][0]["gates"] = [
            {"id": "check", "kind": "command", "cwd": "/tmp", "command": ["never-run"]}
        ]
    elif fault == "empty-name":
        definition["nodes"][0]["name"] = " "
    else:
        definition["nodes"][0]["goal"] = "x" * 65536
    response = await client.post("/v1/workflows/definition", json={"workflow": definition})
    assert response.status_code in {400, 422}, response.text
    if fault == "oversized":
        assert "65,536 characters" in response.json()["detail"]
    assert not service.tasks


async def test_saved_design_has_one_private_root_across_saves_restarts_and_runs(api, designs):
    client, service = api
    workflow_id = "a" * 32
    path = f"/v1/workflows/designs/{workflow_id}"
    definition = yaml.safe_load(example_definition())
    responses = await asyncio.gather(
        *(client.post(path, json={"workflow": definition}) for _ in range(2))
    )
    assert all(response.status_code == 200 for response in responses)
    saved = responses[0].json()
    root_id = saved["root_session_id"]
    assert responses[1].json()["root_session_id"] == root_id
    root = designs.conversations.get_conversation(root_id)
    assert root.agent_id == builtin_agent_id(CODEX_NATIVE_AGENT_NAME)
    assert root.parent_conversation_id is None
    assert root.host_id is None and root.runner_id is None
    assert root.labels["omnigent.workflow.id"] == workflow_id
    assert designs.permissions.get("owner@example.test", root_id).level == 4
    assert designs.permissions.get("__public__", root_id) is None
    assert not service.tasks

    definition["name"] = "Revised design"
    updated = await client.post(path, json={"workflow": definition})
    assert updated.json()["root_session_id"] == root_id
    restarted = WorkflowDesigns(
        designs.directory, designs.conversations, designs.agents, designs.permissions
    )
    assert restarted.load("owner@example.test", workflow_id).workflow.name == "Revised design"
    assert (await client.get(path)).json()["workflow"]["name"] == "Revised design"
    other = {"X-Forwarded-Email": "other@example.test"}
    assert (await client.get(path, headers=other)).status_code == 404
    denied = await client.post(
        "/v1/workflows",
        headers=other,
        json={
            "backend": "simulation",
            "workflow_id": workflow_id,
            "definition": yaml.safe_dump(definition),
        },
    )
    assert denied.status_code == 404
    for _ in range(2):
        response = await client.post(
            "/v1/workflows",
            json={
                "backend": "simulation",
                "workflow_id": workflow_id,
                "definition": yaml.safe_dump(definition),
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["root_session_id"] == root_id
        await asyncio.gather(*service.tasks.values())
        checkpoint = service.store("owner@example.test", response.json()["run_id"]).load()
        assert checkpoint["root_session_id"] == root_id
        assert checkpoint["workflow_id"] == workflow_id
    other_design = await client.post(
        "/v1/workflows/designs/" + "b" * 32, json={"workflow": definition}
    )
    assert other_design.json()["root_session_id"] != root_id
    await designs.conversations.delete_conversation(root_id)
    assert (await client.get(path)).status_code == 404
    assert (await client.post(path, json={"workflow": definition})).status_code == 409


async def test_failed_design_save_discards_its_root_and_can_be_retried(api, designs, monkeypatch):
    client, _ = api
    created = []
    original = designs.conversations.create_conversation

    def create(**kwargs):
        root = original(**kwargs)
        created.append(root.id)
        return root

    monkeypatch.setattr(designs.conversations, "create_conversation", create)
    with monkeypatch.context() as patch:

        def fail_save(*_args):
            raise OSError("disk unavailable")

        patch.setattr("omnigent.workflows.designs.atomic_write_json", fail_save)
        with pytest.raises(OSError, match="disk unavailable"):
            await client.post(
                "/v1/workflows/designs/" + "a" * 32,
                json={"workflow": yaml.safe_load(example_definition())},
            )
    assert designs.conversations.get_conversation(created[0]) is None
    response = await client.post(
        "/v1/workflows/designs/" + "a" * 32,
        json={"workflow": yaml.safe_load(example_definition())},
    )
    assert response.status_code == 200
    assert response.json()["root_session_id"] != created[0]
