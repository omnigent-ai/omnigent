"""Agents read, answer and resolve review comments on their own project's files."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from omnigent.entities.project import PROJECT_IDENTITY_LABEL_KEY
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.runner.project_tickets import execute_ticket_tool
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_OWNER, UnifiedAuthProvider
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from tests.server.integration.test_sessions_attribution import _bind_runner
from tests.server.routes.test_projects_crud import ALICE, BOB, PROJECT_AGENT_ID, _as_user
from tests.server.routes.test_projects_crud import multi_user_client as multi_user_client
from tests.server.routes.test_projects_crud import project_agents as project_agents


@pytest.fixture
def multi_user_app(
    runtime_init: None, db_uri: str, tmp_path: Path, project_agents: SqlAlchemyAgentStore
) -> FastAPI:
    """The projects test app, with file review comments."""
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=project_agents,
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        project_store=SqlAlchemyProjectStore(db_uri),
        comment_store=SqlAlchemyCommentStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
        permission_store=SqlAlchemyPermissionStore(db_uri),
    )


_DOC = "# Plan\n\nShip the PDF export on Tuesday.\n"


async def _project_with_comment(client: httpx.AsyncClient, name: str) -> tuple[str, str, str]:
    project = (
        await client.post("/v1/projects", json={"name": name}, headers=_as_user(ALICE))
    ).json()
    uploaded = await client.post(
        f"/v1/projects/{project['id']}/files",
        files={"file": ("plan.md", _DOC.encode(), "text/markdown")},
        headers=_as_user(ALICE),
    )
    assert uploaded.status_code == 201, uploaded.text
    file_id = uploaded.json()["id"]
    start = _DOC.index("Tuesday")
    comment = await client.post(
        f"/v1/projects/{project['id']}/files/{file_id}/comments",
        json={
            "path": "plan.md",
            "body": "Can we say Thursday instead?",
            "start_index": start,
            "end_index": start + len("Tuesday"),
            "anchor_content": "Tuesday",
        },
        headers=_as_user(ALICE),
    )
    assert comment.status_code == 200, comment.text
    return project["id"], file_id, comment.json()["id"]


def _agent_session(db_uri: str, project_id: str, identity_id: str | None = None) -> str:
    conversations = SqlAlchemyConversationStore(db_uri)
    session = conversations.create_conversation(project_id=project_id, agent_id=PROJECT_AGENT_ID)
    if identity_id:
        conversations.set_labels(session.id, {PROJECT_IDENTITY_LABEL_KEY: identity_id})
    SqlAlchemyPermissionStore(db_uri).grant(ALICE, session.id, LEVEL_OWNER)
    return session.id


@pytest.mark.asyncio
async def test_agent_reads_replies_and_resolves_a_file_comment(
    multi_user_app, multi_user_client, db_uri
):
    client = multi_user_client
    project_id, file_id, comment_id = await _project_with_comment(client, "Docs")
    identity = (
        await client.post(
            f"/v1/projects/{project_id}/identities",
            json={"name": "Product Designer", "instructions": "Own the docs."},
            headers=_as_user(ALICE),
        )
    ).json()
    session_id = _agent_session(db_uri, project_id, identity["id"])
    token = _bind_runner(db_uri, session_id)
    runner = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=multi_user_app),
        base_url="http://test",
        headers={**_as_user(ALICE), **token},
    )

    async def tool(**arguments) -> dict:
        return json.loads(
            await execute_ticket_tool(
                "sys_project_file_comments", {"file_id": file_id, **arguments}, runner, session_id
            )
        )

    listed = await tool(action="list")
    assert [
        (row["created_by"], row["anchor_content"], row["body"]) for row in listed["comments"]
    ] == [(ALICE, "Tuesday", "Can we say Thursday instead?")]
    reply = await tool(action="reply", comment_id=comment_id, text="Changed it to Thursday.")
    assert reply["created_by"] == "Product Designer (agent)"
    assert reply["anchor_content"] == "Tuesday"
    resolved = await tool(action="resolve", comment_id=comment_id)
    assert resolved["status"] == "addressed"

    # The person sees the reply and the resolution.
    seen = (
        await client.get(
            f"/v1/projects/{project_id}/files/{file_id}/comments", headers=_as_user(ALICE)
        )
    ).json()["comments"]
    assert {(row["created_by"], row["status"]) for row in seen} == {
        (ALICE, "addressed"),
        ("Product Designer (agent)", "addressed"),
    }

    # Agents can't rewrite or delete a person's comment.
    path = f"/v1/projects/{project_id}/files/{file_id}/comments/{comment_id}"
    agent = {**token, ORCHESTRATION_SOURCE_HEADER: session_id}
    rewrite = await client.patch(path, json={"body": "x"}, headers={**_as_user(ALICE), **agent})
    assert rewrite.status_code == 403
    delete = await client.delete(path, headers={**_as_user(ALICE), **agent})
    assert delete.status_code == 403
    await runner.aclose()


@pytest.mark.asyncio
async def test_agents_outside_the_project_or_unbound_are_refused(multi_user_client, db_uri):
    client = multi_user_client
    project_id, file_id, _ = await _project_with_comment(client, "Docs")
    other_project = (
        await client.post("/v1/projects", json={"name": "Other"}, headers=_as_user(ALICE))
    ).json()
    path = f"/v1/projects/{project_id}/files/{file_id}/comments"

    outsider = _agent_session(db_uri, other_project["id"])
    other = {**_bind_runner(db_uri, outsider), ORCHESTRATION_SOURCE_HEADER: outsider}
    assert (await client.get(path, headers={**_as_user(ALICE), **other})).status_code == 403

    # Naming a session without its runner token isn't treated as the person either.
    insider = _agent_session(db_uri, project_id)
    unbound = {ORCHESTRATION_SOURCE_HEADER: insider}
    assert (await client.get(path, headers={**_as_user(ALICE), **unbound})).status_code == 403

    # A person who isn't a member can't read it at all.
    assert (await client.get(path, headers=_as_user(BOB))).status_code == 404


@pytest.mark.asyncio
async def test_address_all_sends_open_comments_to_the_assignee_then_the_lead(
    multi_user_app, multi_user_client, db_uri, monkeypatch
):
    client = multi_user_client
    project_id, file_id, _ = await _project_with_comment(client, "Docs")
    delivered: list[tuple[str, str]] = []

    async def deliver(app, project, session_id, *, stable_id, text, automatic=None):
        delivered.append((session_id, text))
        return True

    monkeypatch.setattr(
        multi_user_app.state.project_ticket_notifications, "deliver_to_session", deliver
    )
    path = f"/v1/projects/{project_id}/files/{file_id}/comments/address"

    # With no ticket and no saving session, the Lead gets them.
    lead_id = (await client.get(f"/v1/projects/{project_id}", headers=_as_user(ALICE))).json()[
        "main_session_id"
    ]
    assert lead_id
    sent = await client.post(path, json={}, headers=_as_user(ALICE))
    assert sent.status_code == 200, sent.text
    assert sent.json()["recipient"] == "Lead" and delivered[-1][0] == lead_id
    assert '"Tuesday"' in delivered[-1][1] and "sys_project_file_comments" in delivered[-1][1]

    # A file linked to a ticket goes to the ticket's assignee.
    identity = (
        await client.post(
            f"/v1/projects/{project_id}/identities",
            json={"name": "Product Designer", "instructions": "Own the docs."},
            headers=_as_user(ALICE),
        )
    ).json()
    session_id = _agent_session(db_uri, project_id, identity["id"])
    SqlAlchemyProjectStore(db_uri).set_identity_session(
        project_id,
        identity["id"],
        session_id,
        user_id=ALICE,
        expected_session_id=identity.get("active_session_id"),
    )
    ticket = (
        await client.post(
            f"/v1/projects/{project_id}/tickets",
            json={
                "title": "Review the plan",
                "labels": [{"key": "assignee", "value": "Product Designer"}],
            },
            headers=_as_user(ALICE),
        )
    ).json()
    linked = await client.post(
        f"/v1/projects/{project_id}/files",
        files={"file": ("plan2.md", _DOC.encode(), "text/markdown")},
        data={"ticket_id": ticket["id"]},
        headers=_as_user(ALICE),
    )
    linked_id = linked.json()["id"]
    await client.post(
        f"/v1/projects/{project_id}/files/{linked_id}/comments",
        json={"path": "plan2.md", "body": "Shorter?", "start_index": 0, "end_index": 6},
        headers=_as_user(ALICE),
    )
    sent = await client.post(
        f"/v1/projects/{project_id}/files/{linked_id}/comments/address",
        json={},
        headers=_as_user(ALICE),
    )
    assert sent.status_code == 200, sent.text
    assert sent.json() == {
        "sent": 1,
        "session_id": session_id,
        "recipient": "Product Designer",
        "queued": False,
    }

    # An offline assignee keeps them queued; no second agent gets the same comments.
    delivered.clear()
    online = False

    async def deliver_offline(app, project, session_id, *, stable_id, text, automatic=None):
        delivered.append((session_id, text))
        return online

    monkeypatch.setattr(
        multi_user_app.state.project_ticket_notifications, "deliver_to_session", deliver_offline
    )
    linked_path = f"/v1/projects/{project_id}/files/{linked_id}/comments/address"
    queued = await client.post(linked_path, json={}, headers=_as_user(ALICE))
    assert queued.json()["queued"] is True and queued.json()["recipient"] == "Product Designer"
    assert [target for target, _ in delivered] == [session_id]

    # A missing (archived) assignee session falls back to the Lead.
    delivered.clear()
    online = True
    SqlAlchemyConversationStore(db_uri).update_conversation(session_id, archived=True)
    fallback = await client.post(linked_path, json={}, headers=_as_user(ALICE))
    assert fallback.json()["recipient"] == "Lead" and fallback.json()["queued"] is False
    assert [target for target, _ in delivered] == [lead_id]

    # Only a person sends them; an agent can't.
    agent = {**_bind_runner(db_uri, session_id), ORCHESTRATION_SOURCE_HEADER: session_id}
    refused = await client.post(path, json={}, headers={**_as_user(ALICE), **agent})
    assert refused.status_code == 403
