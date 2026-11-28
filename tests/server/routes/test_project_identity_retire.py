"""The Lead retires a role softly and reversibly; everyone else is refused."""

from __future__ import annotations

import pytest

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from tests.server.integration.test_sessions_attribution import _bind_runner
from tests.server.routes.test_project_file_comments import _agent_session
from tests.server.routes.test_project_file_comments import multi_user_app as multi_user_app
from tests.server.routes.test_projects_crud import ALICE, _as_user
from tests.server.routes.test_projects_crud import multi_user_client as multi_user_client
from tests.server.routes.test_projects_crud import project_agents as project_agents


@pytest.mark.asyncio
async def test_the_lead_retires_a_role_and_the_owner_restores_it(
    multi_user_app, multi_user_client, db_uri
):
    client = multi_user_client
    owner = _as_user(ALICE)
    project = (await client.post("/v1/projects", json={"name": "Team"}, headers=owner)).json()
    project_id, lead_id = project["id"], project["main_session_id"]
    base = f"/v1/projects/{project_id}/identities"

    async def role(name: str) -> dict:
        created = await client.post(
            base, json={"name": name, "instructions": f"You are the {name}."}, headers=owner
        )
        assert created.status_code == 200, created.text
        return created.json()

    designer, reviewer, merger, busy = (
        await role("zz Designer"),
        await role("Reviewer"),
        await role("Merger"),
        await role("zz Busy"),
    )
    designer_session = _agent_session(db_uri, project_id, designer["id"])
    SqlAlchemyProjectStore(db_uri).set_identity_session(
        project_id,
        designer["id"],
        designer_session,
        user_id=ALICE,
        expected_session_id=designer.get("active_session_id"),
    )
    runner = _bind_runner(db_uri, lead_id)
    SqlAlchemyConversationStore(db_uri).set_runner_id(
        designer_session, SqlAlchemyConversationStore(db_uri).get_conversation(lead_id).runner_id
    )
    as_lead = {**owner, **runner, ORCHESTRATION_SOURCE_HEADER: lead_id}
    as_designer = {**owner, **runner, ORCHESTRATION_SOURCE_HEADER: designer_session}
    reason = {"reason": "Merged into the Product Designer, as the owner approved."}

    async def retire(identity: dict, headers: dict) -> tuple[int, str]:
        response = await client.post(
            f"{base}/{identity['id']}/retire", json=reason, headers=headers
        )
        return response.status_code, response.text

    # Only the Lead: another agent is refused, and the Reviewer and Merger stay owner-only.
    assert (await retire(designer, as_designer))[0] == 403
    assert (await retire(reviewer, as_lead))[0] == 403
    assert (await retire(merger, as_lead))[0] == 403
    # A runner call without a source header doesn't get the owner's powers.
    runner_only = {**owner, **runner}
    assert (await retire(reviewer, runner_only))[0] == 403
    # Renaming the Reviewer can't slip it past that guard.
    renamed = await client.patch(
        f"{base}/{reviewer['id']}", json={"name": "zz Helper"}, headers=as_lead
    )
    assert renamed.status_code == 403
    lead_self = await client.post(f"{base}/lead/retire", json=reason, headers=as_lead)
    assert lead_self.status_code in (400, 404)

    # A role with an open ticket waits until it's reassigned.
    ticket = await client.post(
        f"/v1/projects/{project_id}/tickets",
        json={"title": "Still going", "labels": [{"key": "assignee", "value": "zz Busy"}]},
        headers=owner,
    )
    assert ticket.status_code == 201, ticket.text
    status, text = await retire(busy, as_lead)
    assert status == 409 and "Still going" in text

    # The Lead retires the designer: gone from the team, its session closed, listed as retired.
    status, text = await retire(designer, as_lead)
    assert status == 200, text
    listed = (await client.get(base, headers=owner)).json()
    assert designer["id"] not in [entry["id"] for entry in listed["data"]]
    [retired] = [row for row in listed["retired"] if row["id"] == designer["id"]]
    assert (retired["retired_by"], retired["reason"]) == ("lead", reason["reason"])
    assert SqlAlchemyConversationStore(db_uri).get_conversation(designer_session).archived

    # Agents can't restore, and a config write can't drop the retired list.
    refused = await client.post(f"{base}/{designer['id']}/restore", headers=as_lead)
    assert refused.status_code == 403
    runner_restore = await client.post(f"{base}/{designer['id']}/restore", headers=runner_only)
    assert runner_restore.status_code == 403
    patched = await client.patch(f"/v1/projects/{project_id}", json={"config": {}}, headers=owner)
    assert patched.status_code == 200, patched.text
    assert (await client.get(base, headers=owner)).json()["retired"]

    # The owner restores it with the same ID and persona.
    restored = await client.post(f"{base}/{designer['id']}/restore", headers=owner)
    assert restored.status_code == 200, restored.text
    assert (restored.json()["id"], restored.json()["name"]) == (designer["id"], "zz Designer")
    after = (await client.get(base, headers=owner)).json()
    assert designer["id"] in [entry["id"] for entry in after["data"]]
    assert after["retired"] == []
