from uuid import uuid4

from omnigent.entities.project_ticket import CreateTicket, TicketActor, UpdateTicket
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
