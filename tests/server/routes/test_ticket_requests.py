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
            # Bob's feature needs product-ok; the owner's own fix doesn't.
            TicketActor(BOB if kind == "feature" else ALICE),
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
            json={"title": "Bob's feature", "labels": [{"key": "kind", "value": "feature"}]},
            headers=_as_user(BOB),
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
    assert [item["title"] for item in bob] == ["Bob's feature"]
    assert bob[0]["question"] == "Bob, can you OK this?" and bob[0]["raised_by_user_id"] == ALICE


async def test_agents_and_inbox_name_exactly_the_configured_product_approvers(multi_user_client):
    client = multi_user_client
    carol = "carol@example.com"
    project = (
        await client.post("/v1/projects", json={"name": "Approvers"}, headers=_as_user(ALICE))
    ).json()
    for member in (BOB, carol):
        await client.post(
            f"/v1/projects/{project['id']}/shares",
            json={"user_id": member},
            headers=_as_user(ALICE),
        )
    await client.patch(
        f"/v1/projects/{project['id']}",
        json={"config": {"product_approvers": [BOB]}},
        headers=_as_user(ALICE),
    )
    base = f"/v1/projects/{project['id']}/tickets"
    labels = [{"key": "kind", "value": "feature"}]
    ticket = (
        await client.post(base, json={"title": "Gated", "labels": labels}, headers=_as_user(BOB))
    ).json()
    path = f"{base}/{ticket['id']}"

    # Ticket reads list the owner first, then the configured approvers; members aren't in it.
    read = (await client.get(path, headers=_as_user(carol))).json()
    assert read["product_approvers"] == [ALICE, BOB]

    # A member who isn't an approver is told who can approve and how to become one.
    refused = await client.patch(
        path,
        json={"labels": [*labels, {"key": "product-ok", "value": "yes"}]},
        headers=_as_user(carol),
    )
    assert refused.status_code == 403, refused.text
    message = refused.json()["error"]["message"]
    assert f"{ALICE}, {BOB}" in message
    assert f"ask {ALICE} to add you under Product approvers in project settings" in message

    # The Inbox routes the product ask to exactly the people agents are told to ask.
    raised = await client.patch(
        path, json={"labels": [*labels, {"key": "needs-human"}]}, headers=_as_user(carol)
    )
    assert raised.status_code == 200, raised.text
    for viewer in (ALICE, BOB):
        items = (await client.get(PATH, headers=_as_user(viewer))).json()["data"]
        assert [item["responders"] for item in items] == [sorted(read["product_approvers"])]
    assert (await client.get(PATH, headers=_as_user(carol))).json()["data"] == []

    # An approver adding product-ok answers the ask for everyone, owner included.
    approved = await client.patch(
        path,
        json={"labels": [*labels, {"key": "needs-human"}, {"key": "product-ok", "value": "yes"}]},
        headers=_as_user(BOB),
    )
    assert approved.status_code == 200, approved.text
    for viewer in (ALICE, BOB):
        assert (await client.get(PATH, headers=_as_user(viewer))).json()["data"] == []


async def test_an_approver_cannot_clear_what_only_the_owner_can_answer(multi_user_client, db_uri):
    client = multi_user_client
    project = (
        await client.post("/v1/projects", json={"name": "Owner gates"}, headers=_as_user(ALICE))
    ).json()
    await client.post(
        f"/v1/projects/{project['id']}/shares", json={"user_id": BOB}, headers=_as_user(ALICE)
    )
    await client.patch(
        f"/v1/projects/{project['id']}",
        json={"config": {"product_approvers": [BOB]}},
        headers=_as_user(ALICE),
    )
    store = ProjectTicketStore(db_uri)
    worker = TicketActor(ALICE, uuid4().hex, uuid4().hex)
    base = f"/v1/projects/{project['id']}/tickets"
    feature = [{"key": "kind", "value": "feature"}]
    product_ok = {"key": "product-ok", "value": "yes"}
    risk = {"key": "risk", "value": "migration"}

    def raise_ask(title: str, labels: list[dict], requester: str = BOB) -> str:
        ticket = store.create(project["id"], CreateTicket(title=title), TicketActor(requester))
        store.update(
            project["id"],
            ticket["id"],
            UpdateTicket(labels=[*labels, {"key": "needs-human"}]),
            worker,
        )
        return f"{base}/{ticket['id']}"

    # One ask covering product-ok and risk-ok: Bob's product-ok leaves Alice the risk-ok part.
    risky = raise_ask("Risky feature", [*feature, risk])
    assert await _requests(client, BOB) == ["Risky feature"]
    approved = await client.patch(
        risky,
        json={"labels": [*feature, risk, {"key": "needs-human"}, product_ok]},
        headers=_as_user(BOB),
    )
    assert approved.status_code == 200, approved.text

    # A question on the owner's own approved feature: Bob's comment doesn't answer it.
    question = raise_ask("Owner question", [*feature, product_ok], requester=ALICE)
    commented = await client.post(
        f"{question}/comments", json={"text": "Looks fine to me."}, headers=_as_user(BOB)
    )
    assert commented.status_code == 201, commented.text

    assert await _requests(client, ALICE) == ["Owner question", "Risky feature"]
    assert await _requests(client, BOB) == []


async def test_a_persons_cc_lists_the_ticket_in_a_members_inbox_until_read(
    multi_user_client, db_uri
):
    client = multi_user_client
    project = (
        await client.post("/v1/projects", json={"name": "Cc"}, headers=_as_user(ALICE))
    ).json()
    shared = await client.post(
        f"/v1/projects/{project['id']}/shares", json={"user_id": BOB}, headers=_as_user(ALICE)
    )
    assert shared.status_code == 200, shared.text
    store = ProjectTicketStore(db_uri)
    ticket = store.create(project["id"], CreateTicket(title="Export"), TicketActor(ALICE))
    path = f"/v1/projects/{project['id']}/tickets/{ticket['id']}"

    # An agent naming a person doesn't cc them; agents ask with needs-human instead.
    store.comment(
        project["id"],
        ticket["id"],
        CommentOnTicket(text="@Bob have a look"),
        TicketActor(ALICE, uuid4().hex, uuid4().hex),
    )
    posted = await client.post(
        f"{path}/comments", json={"text": "@Bob can you check the CSV?"}, headers=_as_user(ALICE)
    )
    assert posted.status_code == 201, posted.text

    response = await client.get(PATH, headers=_as_user(BOB))
    [entry] = response.json()["data"]
    assert entry["reason"] == "cc" and entry["title"] == "Export"
    assert entry["raised_by_user_id"] == ALICE
    assert entry["question"] == "@Bob can you check the CSV?"
    # The commenter isn't cc'd, and the cc is recorded on the comment for display.
    assert await _requests(client, ALICE) == []
    events = (await client.get(f"{path}/events", headers=_as_user(BOB))).json()["data"]
    assert events[-1]["payload"]["cc"] == [{"user_id": BOB, "name": "Bob"}]
    assert "mentions" not in events[-1]["payload"]

    # Reading the ticket past the comment clears it.
    seen = await client.put(
        f"{path}/read-state",
        json={"seen_position": entry["ask_position"]},
        headers=_as_user(BOB),
    )
    assert seen.status_code == 200, seen.text
    assert await _requests(client, BOB) == []


async def test_a_new_tickets_description_ccs_the_people_it_names(multi_user_client):
    client = multi_user_client
    project = (
        await client.post("/v1/projects", json={"name": "Filed"}, headers=_as_user(ALICE))
    ).json()
    shared = await client.post(
        f"/v1/projects/{project['id']}/shares", json={"user_id": BOB}, headers=_as_user(ALICE)
    )
    assert shared.status_code == 200, shared.text
    created = await client.post(
        f"/v1/projects/{project['id']}/tickets",
        json={"title": "Stories", "description": "Make sure @Bob reviews these. @Lead, plan it."},
        headers=_as_user(ALICE),
    )
    assert created.status_code == 201, created.text

    [entry] = (await client.get(PATH, headers=_as_user(BOB))).json()["data"]
    assert entry["reason"] == "cc" and entry["title"] == "Stories"
    assert entry["question"] == "Make sure @Bob reviews these. @Lead, plan it."
    assert await _requests(client, ALICE) == []
    path = f"/v1/projects/{project['id']}/tickets/{created.json()['id']}"
    [event] = (await client.get(f"{path}/events", headers=_as_user(BOB))).json()["data"]
    assert event["kind"] == "created"
    assert event["payload"]["cc"] == [{"user_id": BOB, "name": "Bob"}]
    # Team members it names are recorded and woken, as on a comment.
    assert [mention["name"] for mention in event["payload"]["mentions"]] == ["Lead"]


async def test_an_approver_gives_risk_ok_on_others_tickets_only(multi_user_client, db_uri):
    client = multi_user_client
    project = (
        await client.post("/v1/projects", json={"name": "Approver risk"}, headers=_as_user(ALICE))
    ).json()
    await client.post(
        f"/v1/projects/{project['id']}/shares", json={"user_id": BOB}, headers=_as_user(ALICE)
    )
    await client.patch(
        f"/v1/projects/{project['id']}",
        json={"config": {"product_approvers": [BOB]}},
        headers=_as_user(ALICE),
    )
    store = ProjectTicketStore(db_uri)
    worker = TicketActor(ALICE, uuid4().hex, uuid4().hex)
    base = f"/v1/projects/{project['id']}/tickets"
    risk = [{"key": "risk", "value": "migration"}, {"key": "needs-human"}]

    def ask(title: str, requester: str) -> str:
        ticket = store.create(project["id"], CreateTicket(title=title), TicketActor(requester))
        store.update(project["id"], ticket["id"], UpdateTicket(labels=risk), worker)
        return f"{base}/{ticket['id']}"

    alices = ask("Alice's risky fix", ALICE), "Alice's risky fix"
    bobs = ask("Bob's risky fix", BOB), "Bob's risky fix"
    # Bob is asked for risk-ok only where he didn't request the work.
    assert await _requests(client, BOB) == [alices[1]]
    refused = await client.patch(
        bobs[0], json={"add_labels": [{"key": "risk-ok"}]}, headers=_as_user(BOB)
    )
    assert refused.status_code == 403, refused.text
    given = await client.patch(
        alices[0], json={"add_labels": [{"key": "risk-ok"}]}, headers=_as_user(BOB)
    )
    assert given.status_code == 200, given.text
    assert {label["key"] for label in given.json()["labels"]} == {"risk", "risk-ok"}
    assert await _requests(client, ALICE) == [bobs[1]]


async def test_a_plain_question_goes_to_the_member_who_requested_the_ticket(
    multi_user_client, db_uri
):
    client = multi_user_client
    carol = "carol@example.com"
    project = (
        await client.post("/v1/projects", json={"name": "Routing"}, headers=_as_user(ALICE))
    ).json()
    for member in (BOB, carol):
        shared = await client.post(
            f"/v1/projects/{project['id']}/shares",
            json={"user_id": member},
            headers=_as_user(ALICE),
        )
        assert shared.status_code == 200, shared.text
    await client.patch(
        f"/v1/projects/{project['id']}",
        json={"config": {"product_approvers": [carol]}},
        headers=_as_user(ALICE),
    )
    store = ProjectTicketStore(db_uri)
    worker = TicketActor(ALICE, uuid4().hex, uuid4().hex)
    base = f"/v1/projects/{project['id']}/tickets"

    def raise_ask(title: str, question: str, labels: list[dict] | None = None) -> str:
        ticket = store.create(project["id"], CreateTicket(title=title), TicketActor(BOB))
        store.update(
            project["id"],
            ticket["id"],
            UpdateTicket(labels=[*(labels or []), {"key": "needs-human"}], comment=question),
            worker,
        )
        return ticket["id"]

    plain = raise_ask("Bob's export", "CSV or JSON?")
    named = raise_ask("Bob's report", "@Carol can you pick the chart type?")
    risky = raise_ask("Bob's migration", "OK to migrate?", [{"key": "risk", "value": "db"}])

    async def responders(ticket_id: str) -> list[str]:
        got = await client.get(f"{base}/{ticket_id}", headers=_as_user(ALICE))
        return got.json()["waiting_on"]["responders"]

    # A plain question waits on the requester; one that names someone waits on them.
    assert await responders(plain) == [BOB]
    assert await responders(named) == [carol]
    # A sign-off ask keeps its approvers: the owner and the approver who didn't request it.
    assert await responders(risky) == sorted([ALICE, carol])
    assert await _requests(client, BOB) == ["Bob's export"]
    assert await _requests(client, carol) == ["Bob's migration", "Bob's report"]
    assert await _requests(client, ALICE) == ["Bob's migration"]

    # The requester can't clear a sign-off ask by commenting.
    commented = await client.post(
        f"{base}/{risky}/comments", json={"text": "Sure, go ahead."}, headers=_as_user(BOB)
    )
    assert commented.status_code == 201, commented.text
    assert await responders(risky) == sorted([ALICE, carol])

    # The owner isn't asked a member's question but can still answer it.
    answered = await client.post(
        f"{base}/{plain}/comments", json={"text": "JSON, please."}, headers=_as_user(ALICE)
    )
    assert answered.status_code == 201, answered.text
    assert await _requests(client, BOB) == []
