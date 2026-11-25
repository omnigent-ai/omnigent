"""An agent can't read another project's sessions, even when its person can."""

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.server.auth import LEVEL_OWNER
from omnigent.stores.conversation_store import CREATED_BY_SESSION_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.routes.test_project_intake import _agent_headers, _project
from tests.server.routes.test_projects_crud import ALICE, BOB, _as_user
from tests.server.routes.test_projects_crud import multi_user_app as multi_user_app
from tests.server.routes.test_projects_crud import multi_user_client as multi_user_client
from tests.server.routes.test_projects_crud import project_agents as project_agents


async def test_an_agent_reads_only_its_own_project_and_unfiled_sessions(multi_user_client, db_uri):
    client = multi_user_client
    own = await _project(client, ALICE, "Own factory")
    other = await _project(client, BOB, "Other factory")
    await client.post(
        f"/v1/projects/{other['id']}/shares", json={"user_id": ALICE}, headers=_as_user(BOB)
    )
    conversations = SqlAlchemyConversationStore(db_uri)
    other_lead = other["main_session_id"]
    agent_id = conversations.get_conversation(other_lead).agent_id
    other_child = conversations.create_conversation(
        parent_conversation_id=other_lead, agent_id=agent_id
    )
    unfiled = conversations.create_conversation(agent_id=agent_id)
    SqlAlchemyPermissionStore(db_uri).grant(ALICE, unfiled.id, LEVEL_OWNER)
    agent = _agent_headers(db_uri, own["id"], ALICE)

    # Alice herself can open the shared project's Lead; her agent can't, or its sub-agent.
    for path in (
        f"/v1/sessions/{other_lead}",
        f"/v1/sessions/{other_lead}/items",
        f"/v1/sessions/{other_child.id}",
    ):
        assert (await client.get(path, headers=_as_user(ALICE))).status_code == 200, path
        refused = await client.get(path, headers=agent)
        assert refused.status_code == 404, (path, refused.text)
    fork = await client.post(f"/v1/sessions/{other_lead}/fork", json={}, headers=agent)
    assert fork.status_code == 404, fork.text
    rename = await client.patch(f"/v1/sessions/{other_lead}", json={"title": "x"}, headers=agent)
    assert rename.status_code == 404, rename.text

    # Its own project's sessions, unfiled ones, and a worker it started elsewhere stay readable.
    second = await _project(client, ALICE, "Second factory")
    source_id = agent[ORCHESTRATION_SOURCE_HEADER]
    started = conversations.create_conversation(
        agent_id=agent_id,
        project_id=second["id"],
        labels={CREATED_BY_SESSION_LABEL_KEY: source_id},
    )
    SqlAlchemyPermissionStore(db_uri).grant(ALICE, started.id, LEVEL_OWNER)
    readable = (own["main_session_id"], unfiled.id, started.id)
    for session_id in readable:
        assert (await client.get(f"/v1/sessions/{session_id}", headers=agent)).status_code == 200

    # Listing hides Alice's sessions in her other projects from the agent, not from Alice.

    def listed(response) -> set[str]:
        assert response.status_code == 200, response.text
        return {row["id"] for row in response.json()["data"]}

    query = "/v1/sessions?kind=any&limit=100"
    seen_by_alice = listed(await client.get(query, headers=_as_user(ALICE)))
    seen_by_agent = listed(await client.get(query, headers=agent))
    assert {second["main_session_id"], own["main_session_id"], unfiled.id} <= seen_by_alice
    assert second["main_session_id"] not in seen_by_agent
    assert set(readable) <= seen_by_agent
    # A person sending the header for a session that isn't theirs only narrows their view.
    spoofed = {**_as_user(ALICE), ORCHESTRATION_SOURCE_HEADER: own["main_session_id"]}
    assert (await client.get(f"/v1/sessions/{other_lead}", headers=spoofed)).status_code == 404


def test_every_route_naming_a_session_carries_the_scope(multi_user_app):
    """Session routes on any router, websockets included, refuse other projects' sessions."""
    from fastapi.routing import APIWebSocketRoute
    from starlette.routing import Mount

    def routes(items, prefix=""):
        for route in items:
            if isinstance(route, Mount):
                yield from routes(route.routes, prefix + route.path)
            else:
                yield prefix + getattr(route, "path", ""), route

    named = [
        (path, route)
        for path, route in routes(multi_user_app.routes)
        if "{session_id}" in path or "{source_id}" in path
    ]
    assert len(named) > 100
    assert any(isinstance(route, APIWebSocketRoute) for _, route in named)
    unscoped = [
        path
        for path, route in named
        if not any(
            getattr(dependency.call, "__name__", "") == "refuse_other_project_session"
            for dependency in getattr(getattr(route, "dependant", None), "dependencies", [])
        )
    ]
    assert unscoped == []
