from uuid import uuid4

from omnigent.entities.project_ticket import (
    CommentOnTicket,
    CreateTicket,
    TicketActor,
    UpdateTicket,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.project_ticket_store import ProjectTicketStore
from tests.server.routes.test_projects_crud import ALICE, BOB, _as_user
from tests.server.routes.test_projects_crud import multi_user_app as multi_user_app
from tests.server.routes.test_projects_crud import multi_user_client as multi_user_client
from tests.server.routes.test_projects_crud import project_agents as project_agents

PATH = "/v1/inbox/ticket-requests"


async def _requests(client, user: str) -> list[str]:
    response = await client.get(PATH, headers=_as_user(user))
    assert response.status_code == 200, response.text
    return sorted(item["title"] for item in response.json()["data"])


async def test_needs_human_asks_reach_only_their_responders_and_clear_for_all(
    multi_user_client, db_uri
):
    client = multi_user_client
    project = (
        await client.post("/v1/projects", json={"name": "Asks"}, headers=_as_user(ALICE))
    ).json()
    shared = await client.post(
        f"/v1/projects/{project['id']}/shares", json={"user_id": BOB}, headers=_as_user(ALICE)
    )
    assert shared.status_code == 200, shared.text
    configured = await client.patch(
        f"/v1/projects/{project['id']}",
        json={"config": {"product_approvers": [BOB]}},
        headers=_as_user(ALICE),
    )
    assert configured.status_code == 200, configured.text

    store = ProjectTicketStore(db_uri)
    worker = TicketActor(ALICE, uuid4().hex, uuid4().hex)
    tickets = {}
    for title, kind in (("Feature", "feature"), ("Fix", "fix")):
        ticket = store.create(
            project["id"],
            CreateTicket(title=title, labels=[{"key": "kind", "value": kind}]),
            TicketActor(ALICE),
        )
        store.update(
            project["id"],
            ticket["id"],
            UpdateTicket(labels=[{"key": "kind", "value": kind}, {"key": "needs-human"}]),
            worker,
        )
        tickets[title] = ticket["id"]

    # A feature awaiting product-ok goes to every approver; other asks only to the owner.
    assert await _requests(client, ALICE) == ["Feature", "Fix"]
    assert await _requests(client, BOB) == ["Feature"]

    # One approver answering clears it for both, while the label is still set.
    base = f"/v1/projects/{project['id']}/tickets"
    answered = await client.post(
        f"{base}/{tickets['Feature']}/comments",
        json={"text": "Looks good, adding product-ok."},
        headers=_as_user(BOB),
    )
    assert answered.status_code in (200, 201), answered.text
    assert await _requests(client, ALICE) == ["Fix"]
    assert await _requests(client, BOB) == []

    # Removing the label clears the owner's ask.
    cleared = await client.patch(
        f"{base}/{tickets['Fix']}",
        json={"labels": [{"key": "kind", "value": "fix"}]},
        headers=_as_user(ALICE),
    )
    assert cleared.status_code == 200, cleared.text
    assert await _requests(client, ALICE) == []


async def test_inbox_sessions_open_their_one_ticket_and_asks_carry_their_question(
    multi_user_client, db_uri
):
    client = multi_user_client
    project = (
        await client.post("/v1/projects", json={"name": "Routes"}, headers=_as_user(ALICE))
    ).json()
    conversations = SqlAlchemyConversationStore(db_uri)
    worker = conversations.create_conversation(project_id=project["id"]).id
    lead = conversations.create_conversation(project_id=project["id"]).id
    store = ProjectTicketStore(db_uri)
    titles = ("Worker's ticket", "Another ticket")
    ids = [
        store.create(project["id"], CreateTicket(title=title), TicketActor(ALICE))["id"]
        for title in titles
    ]
    actor = TicketActor(ALICE, worker)
    store.link_session(project["id"], ids[0], worker, "attach", actor)
    for ticket_id in ids:
        store.link_session(project["id"], ticket_id, lead, "attach", TicketActor(ALICE, lead))

    path = f"{PATH}/sessions"
    mapped = await client.get(
        path, params=[("session_id", worker), ("session_id", lead)], headers=_as_user(ALICE)
    )
    assert mapped.status_code == 200, mapped.text
    # The worker opens its one ticket; a session on several tickets keeps its chat.
    assert mapped.json()["data"] == {
        worker: {
            "ticket_id": ids[0],
            "title": titles[0],
            "project_id": project["id"],
            "project_name": "Routes",
        }
    }
    # Someone without access to the project learns nothing.
    outsider = await client.get(path, params={"session_id": worker}, headers=_as_user(BOB))
    assert outsider.json()["data"] == {}

    # The ask links to the asking session's question, not the label change.
    store.update(project["id"], ids[0], UpdateTicket(labels=[{"key": "needs-human"}]), actor)
    store.comment(project["id"], ids[0], CommentOnTicket(text="Which option?"), actor)
    request = (await client.get(PATH, headers=_as_user(ALICE))).json()["data"][0]
    events = store.events(project["id"], ids[0], user_id=ALICE)["data"]
    question = next(event for event in events if event["payload"].get("text") == "Which option?")
    assert request["question"] == "Which option?"
    assert request["ask_position"] == question["position"]


async def test_a_person_who_raises_an_ask_neither_sees_nor_answers_it(multi_user_client, db_uri):
    client = multi_user_client
    project = (
        await client.post("/v1/projects", json={"name": "Owner asks"}, headers=_as_user(ALICE))
    ).json()
    await client.post(
        f"/v1/projects/{project['id']}/shares", json={"user_id": BOB}, headers=_as_user(ALICE)
    )
    await client.patch(
        f"/v1/projects/{project['id']}",
        json={"config": {"product_approvers": [BOB]}},
        headers=_as_user(ALICE),
    )
    base = f"/v1/projects/{project['id']}/tickets"
    ticket = (
        await client.post(
            base,
            json={"title": "Owner's feature", "labels": [{"key": "kind", "value": "feature"}]},
            headers=_as_user(ALICE),
        )
    ).json()
    raised = await client.patch(
        f"{base}/{ticket['id']}",
        json={
            "labels": [{"key": "kind", "value": "feature"}, {"key": "needs-human"}],
            "comment": "Bob, can you OK this?",
        },
        headers=_as_user(ALICE),
    )
    assert raised.status_code == 200, raised.text
    followup = await client.post(
        f"{base}/{ticket['id']}/comments", json={"text": "Any thoughts?"}, headers=_as_user(ALICE)
    )
    assert followup.status_code == 201, followup.text
    # The owner asked Bob: only Bob sees it, and the owner's follow-up doesn't answer it.
    assert await _requests(client, ALICE) == []
    bob = (await client.get(PATH, headers=_as_user(BOB))).json()["data"]
    assert [item["title"] for item in bob] == ["Owner's feature"]
    assert bob[0]["question"] == "Bob, can you OK this?" and bob[0]["raised_by_user_id"] == ALICE
