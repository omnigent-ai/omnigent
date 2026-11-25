"""One project's Lead messages another project's Lead, which replies; nothing else opens up."""

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.server.routes import project_lead_messages as lead_messages
from tests.server.integration.test_sessions_attribution import _bind_runner
from tests.server.routes.test_project_intake import CAROL, _agent_headers, _project
from tests.server.routes.test_projects_crud import ALICE, BOB, _as_user
from tests.server.routes.test_projects_crud import multi_user_app as multi_user_app
from tests.server.routes.test_projects_crud import multi_user_client as multi_user_client
from tests.server.routes.test_projects_crud import project_agents as project_agents


def _lead_headers(db_uri: str, project: dict, user: str) -> dict[str, str]:
    """Headers for *project*'s Lead session, on its bound runner."""
    lead = project["main_session_id"]
    return {**_as_user(user), **_bind_runner(db_uri, lead), ORCHESTRATION_SOURCE_HEADER: lead}


async def test_leads_exchange_attributed_messages_across_a_shared_project_and_nothing_more(
    multi_user_app, multi_user_client, db_uri, monkeypatch
):
    client = multi_user_client
    sender = await _project(client, ALICE, "Sender factory")
    receiver = await _project(client, BOB, "Receiver factory")
    private = await _project(client, CAROL, "Private factory")
    await client.post(
        f"/v1/projects/{receiver['id']}/shares", json={"user_id": ALICE}, headers=_as_user(BOB)
    )
    delivered: list[tuple[str, str]] = []

    async def capture(app, project, session_id, *, stable_id, text, automatic=None):
        delivered.append((session_id, text))
        return True

    notifications = multi_user_app.state.project_ticket_notifications
    monkeypatch.setattr(notifications, "deliver_to_session", capture)
    alice_lead = _lead_headers(db_uri, sender, ALICE)
    bob_lead = _lead_headers(db_uri, receiver, BOB)
    question = {"message": "How do you show people when a ticket needs them?"}

    sent = await client.post(
        f"/v1/projects/{receiver['id']}/lead-messages", json=question, headers=alice_lead
    )
    assert sent.status_code == 201, sent.text
    assert set(sent.json()) == {"object", "id", "project_id", "delivered"}
    message_id = sent.json()["id"]
    # Delivered to the receiver's Lead, marked with the sending project and session.
    session_id, text = delivered[-1]
    assert session_id == receiver["main_session_id"]
    assert text.startswith(
        f"[Message from the Sender factory project's Lead (session {sender['main_session_id']})]"
    )
    assert question["message"] in text and f'reply_to="{message_id}"' in text

    # The receiver's Lead replies, though the sender's project isn't shared with Bob.
    reply = f"/v1/projects/{receiver['id']}/lead-messages/{message_id}/reply"
    answered = await client.post(reply, json={"message": "A Needs you tag."}, headers=bob_lead)
    assert answered.status_code == 201, answered.text
    session_id, text = delivered[-1]
    assert session_id == sender["main_session_id"]
    assert text.startswith("[Reply from the Receiver factory project's Lead")
    # Each message takes one reply; the sender can answer the reply in turn.
    assert (await client.post(reply, json=question, headers=bob_lead)).status_code == 404
    back = f"/v1/projects/{sender['id']}/lead-messages/{answered.json()['id']}/reply"
    assert (await client.post(back, json=question, headers=alice_lead)).status_code == 201

    # Only the project the message went to may reply, and only from its own path.
    second = await client.post(
        f"/v1/projects/{receiver['id']}/lead-messages", json=question, headers=alice_lead
    )
    open_reply = f"/v1/projects/{receiver['id']}/lead-messages/{second.json()['id']}/reply"
    assert (await client.post(open_reply, json=question, headers=alice_lead)).status_code == 403
    elsewhere = f"/v1/projects/{sender['id']}/lead-messages/{second.json()['id']}/reply"
    assert (await client.post(elsewhere, json=question, headers=alice_lead)).status_code == 404
    # A misdirected attempt doesn't use up the receiver's one reply.
    assert (await client.post(open_reply, json=question, headers=bob_lead)).status_code == 201

    # Refused: a project not shared with the sender's person, a person without an agent,
    # an agent that isn't its project's Lead, and the sender's own project.
    count = len(delivered)
    not_shared = f"/v1/projects/{private['id']}/lead-messages"
    assert (await client.post(not_shared, json=question, headers=alice_lead)).status_code == 404
    to_receiver = f"/v1/projects/{receiver['id']}/lead-messages"
    assert (
        await client.post(to_receiver, json=question, headers=_as_user(ALICE))
    ).status_code == 403
    worker = _agent_headers(db_uri, sender["id"], ALICE)
    assert (await client.post(to_receiver, json=question, headers=worker)).status_code == 403
    own = f"/v1/projects/{sender['id']}/lead-messages"
    assert (await client.post(own, json=question, headers=alice_lead)).status_code == 400
    assert len(delivered) == count

    # No other access: the receiver's board stays closed to the sender's Lead.
    board = f"/v1/projects/{receiver['id']}/tickets"
    assert (await client.get(board, headers=alice_lead)).status_code == 403

    # Past the hourly limit (one here), a further message is refused.
    monkeypatch.setattr(lead_messages, "_limiter", lead_messages.MentionRateLimiter(limit=1))
    assert (await client.post(to_receiver, json=question, headers=alice_lead)).status_code == 201
    over = await client.post(to_receiver, json=question, headers=alice_lead)
    assert over.status_code == 409 and "last hour" in over.text
