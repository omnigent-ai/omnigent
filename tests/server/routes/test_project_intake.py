"""An agent in one project files intake tickets on another project shared with its person."""

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.server.auth import LEVEL_OWNER
from omnigent.server.routes import project_tickets as ticket_routes
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.integration.test_sessions_attribution import _bind_runner
from tests.server.routes.test_projects_crud import ALICE, BOB, _as_user
from tests.server.routes.test_projects_crud import multi_user_app as multi_user_app
from tests.server.routes.test_projects_crud import multi_user_client as multi_user_client
from tests.server.routes.test_projects_crud import project_agents as project_agents

CAROL = "carol@example.com"


async def _project(client, owner: str, name: str) -> dict:
    created = await client.post("/v1/projects", json={"name": name}, headers=_as_user(owner))
    assert created.status_code in (200, 201), created.text
    return created.json()


def _agent_headers(db_uri: str, project_id: str, user: str) -> dict[str, str]:
    """Headers for an agent session filed in *project_id*, owned by *user*, on its bound runner."""
    source = SqlAlchemyConversationStore(db_uri).create_conversation(project_id=project_id)
    SqlAlchemyPermissionStore(db_uri).grant(user, source.id, LEVEL_OWNER)
    return {
        **_as_user(user),
        **_bind_runner(db_uri, source.id),
        ORCHESTRATION_SOURCE_HEADER: source.id,
    }


async def test_an_agent_files_an_attributed_ticket_on_a_shared_board_and_nothing_more(
    multi_user_client, db_uri, monkeypatch
):
    client = multi_user_client
    sender = await _project(client, ALICE, "Sender factory")
    receiver = await _project(client, BOB, "Receiver factory")
    private = await _project(client, CAROL, "Private factory")
    await client.post(
        f"/v1/projects/{receiver['id']}/shares", json={"user_id": ALICE}, headers=_as_user(BOB)
    )
    agent = _agent_headers(db_uri, sender["id"], ALICE)
    intake = f"/v1/projects/{receiver['id']}/tickets/intake"
    body = {"title": "Please look at our setup", "description": "Details."}

    filed = await client.post(intake, json=body, headers=agent)
    assert filed.status_code == 201, filed.text
    assert set(filed.json()) == {"object", "id", "project_id", "title", "stage_id"}

    # The receiver sees who sent it; no labels, its first stage, no session of the sender's.
    tickets = f"/v1/projects/{receiver['id']}/tickets"
    ticket = (await client.get(f"{tickets}/{filed.json()['id']}", headers=_as_user(BOB))).json()
    assert ticket["requested_by"] == f"project:{sender['id']}"
    assert ticket["labels"] == []
    assert ticket["description"].startswith("> **From the Sender factory project.**")
    assert ticket["description"].endswith("Details.")
    created = (await client.get(f"{tickets}/{ticket['id']}/events", headers=_as_user(BOB))).json()[
        "data"
    ][0]
    assert created["session_id"] is None and created["agent_id"] is None
    assert created["payload"]["source"]["project_name"] == "Sender factory"
    sessions = (
        await client.get(f"{tickets}/{ticket['id']}/sessions", headers=_as_user(BOB))
    ).json()
    assert sessions["data"] == []

    # No other access: the agent's ordinary ticket tools stay scoped to its own project.
    for method, path in (
        ("GET", tickets),
        ("GET", f"{tickets}/{ticket['id']}"),
        ("POST", tickets),
    ):
        refused = await client.request(method, path, headers=agent, json={"title": "x"})
        assert refused.status_code == 403, (method, path, refused.text)

    # Not shared with the agent's person, a person without an agent, the agent's own board,
    # or sender-chosen labels: all refused.
    not_shared = f"/v1/projects/{private['id']}/tickets/intake"
    assert (await client.post(not_shared, json=body, headers=agent)).status_code == 404
    assert (await client.post(intake, json=body, headers=_as_user(ALICE))).status_code == 403
    own = f"/v1/projects/{sender['id']}/tickets/intake"
    assert (await client.post(own, json=body, headers=agent)).status_code == 400
    labeled = {**body, "labels": [{"key": "risk-ok"}]}
    assert (await client.post(intake, json=labeled, headers=agent)).status_code == 422

    # One more is over a limit of two per hour.
    monkeypatch.setattr(ticket_routes, "INTAKE_TICKETS_PER_HOUR", 2)
    assert (await client.post(intake, json=body, headers=agent)).status_code == 201
    over = await client.post(intake, json=body, headers=agent)
    assert over.status_code == 409 and "limit 2" in over.text
