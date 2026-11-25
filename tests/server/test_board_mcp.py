"""The server-run board service for hosts that don't relay the board tools."""

from __future__ import annotations

import json

import pytest

from omnigent.host.frames import CAP_PROJECT_TOOLS
from omnigent.server.auth import LEVEL_OWNER
from omnigent.server.board_mcp import BOARD_SERVICE_ID, board_service_wanted
from omnigent.spec.types import AgentSpec
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.integration.test_sessions_attribution import _bind_runner
from tests.server.routes.test_projects_crud import ALICE, BOB, PROJECT_AGENT_ID, _as_user
from tests.server.routes.test_projects_crud import multi_user_app as multi_user_app
from tests.server.routes.test_projects_crud import multi_user_client as multi_user_client
from tests.server.routes.test_projects_crud import project_agents as project_agents

_PATH = f"/v1/mcp/{BOARD_SERVICE_ID}"


def _rpc(method: str, params: dict | None = None) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}


def test_board_is_offered_only_to_project_sessions_on_hosts_without_the_tools(db_uri):
    conversations = SqlAlchemyConversationStore(db_uri)
    in_project = conversations.create_conversation(project_id="a" * 32)
    loose = conversations.create_conversation()
    spec = AgentSpec(spec_version=1, name="lead")
    assert board_service_wanted(in_project, spec, set())
    assert not board_service_wanted(in_project, spec, {CAP_PROJECT_TOOLS})
    assert not board_service_wanted(in_project, spec, None)  # no live host
    assert not board_service_wanted(loose, spec, set())
    assert not board_service_wanted(in_project, None, set())


@pytest.mark.asyncio
async def test_board_writes_as_the_session_only_with_its_runner_token(multi_user_client, db_uri):
    client = multi_user_client
    project = (
        await client.post("/v1/projects", json={"name": "Board"}, headers=_as_user(ALICE))
    ).json()
    lead = SqlAlchemyConversationStore(db_uri).create_conversation(
        project_id=project["id"], agent_id=PROJECT_AGENT_ID
    )
    SqlAlchemyPermissionStore(db_uri).grant(ALICE, lead.id, LEVEL_OWNER)
    session = {**_as_user(ALICE), "X-Omnigent-Session-Id": lead.id}
    runner = {**session, **_bind_runner(db_uri, lead.id)}

    # Without the session's runner token the board isn't served, so nothing writes as it.
    refused = await client.post(_PATH, json=_rpc("tools/list"), headers=session)
    assert refused.status_code == 403
    stranger = await client.post(
        _PATH, json=_rpc("tools/list"), headers={**runner, **_as_user(BOB)}
    )
    assert stranger.status_code in (403, 404)

    listed = (await client.post(_PATH, json=_rpc("tools/list"), headers=runner)).json()
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert {"sys_ticket_create", "sys_ticket_comment", "sys_project_identity_session"} <= names
    assert "sys_project_artifact_save" not in names

    called = (
        await client.post(
            _PATH,
            json=_rpc(
                "tools/call",
                {"name": "sys_ticket_create", "arguments": {"title": "Filed by the Lead"}},
            ),
            headers=runner,
        )
    ).json()
    output = json.loads(called["result"]["content"][0]["text"])
    ticket = output.get("ticket", output)
    events = (
        await client.get(
            f"/v1/projects/{project['id']}/tickets/{ticket['id']}/events",
            headers=_as_user(ALICE),
        )
    ).json()["data"]
    created = next(event for event in events if event["kind"] == "created")
    assert created["session_id"] == lead.id


@pytest.mark.asyncio
async def test_board_keeps_agent_guards_with_a_bound_runner(multi_user_client, db_uri):
    """Through the board an agent still can't sign off on risk or change the mandate."""
    client = multi_user_client
    project = (
        await client.post(
            "/v1/projects",
            json={"name": "Guards", "mandate": "Ship carefully."},
            headers=_as_user(ALICE),
        )
    ).json()
    lead = SqlAlchemyConversationStore(db_uri).create_conversation(
        project_id=project["id"], agent_id=PROJECT_AGENT_ID
    )
    SqlAlchemyPermissionStore(db_uri).grant(ALICE, lead.id, LEVEL_OWNER)
    runner = {**_as_user(ALICE), "X-Omnigent-Session-Id": lead.id, **_bind_runner(db_uri, lead.id)}

    async def call(name: str, arguments: dict) -> dict:
        response = await client.post(
            _PATH, json=_rpc("tools/call", {"name": name, "arguments": arguments}), headers=runner
        )
        assert response.status_code == 200, response.text
        return json.loads(response.json()["result"]["content"][0]["text"])

    created = await call("sys_ticket_create", {"title": "Risky change"})
    ticket = created.get("ticket", created)
    signed = await call(
        "sys_ticket_update",
        {"ticket_id": ticket["id"], "add_labels": [{"key": "risk-ok"}]},
    )
    assert "Only a person can set `risk-ok`" in signed["detail"], signed
    stored = (
        await client.get(
            f"/v1/projects/{project['id']}/tickets/{ticket['id']}", headers=_as_user(ALICE)
        )
    ).json()
    assert "risk-ok" not in {label["key"] for label in stored["labels"]}

    # The board has no tool that writes the mandate; a proposal waits for the owner.
    names = {
        tool["name"]
        for tool in (await client.post(_PATH, json=_rpc("tools/list"), headers=runner)).json()[
            "result"
        ]["tools"]
    }
    assert "sys_project_update" not in names
    proposed = await call("sys_project_mandate_propose", {"text": "Agents may merge."})
    assert proposed.get("status") == "pending_owner_approval", proposed
    after = (await client.get(f"/v1/projects/{project['id']}", headers=_as_user(ALICE))).json()
    assert after["mandate"] == "Ship carefully."
