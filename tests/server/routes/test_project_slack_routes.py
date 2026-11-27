"""Only people change a project's Slack link, and only its Lead replies in Slack."""

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.stores.project_slack_relay_store import SlackEvent, SlackRelayStore
from tests.server.routes.test_project_intake import _project
from tests.server.routes.test_project_lead_messages import _lead_headers
from tests.server.routes.test_projects_crud import ALICE, BOB, _as_user
from tests.server.routes.test_projects_crud import multi_user_app as multi_user_app
from tests.server.routes.test_projects_crud import multi_user_client as multi_user_client
from tests.server.routes.test_projects_crud import project_agents as project_agents

LINK = {"channel_link": "https://app.slack.com/client/T0ZZTEST1/C0ZZTEST1"}
THREAD = "1790000001.000100"


async def test_agents_cannot_change_the_link_and_only_the_lead_replies(
    multi_user_app, multi_user_client, db_uri
):
    client = multi_user_client
    project = await _project(client, ALICE, "zz-slack-routes (throwaway)")
    base = f"/v1/projects/{project['id']}/slack"
    await client.post(
        f"/v1/projects/{project['id']}/shares", json={"user_id": BOB}, headers=_as_user(ALICE)
    )
    lead = _lead_headers(db_uri, project, ALICE)

    # An agent, even the owner's own Lead, can't link, pause, resume or unlink.
    assert (await client.put(base, json=LINK, headers=lead)).status_code == 403
    linked = await client.put(
        base, json=LINK, headers={**_as_user(ALICE), "X-Forwarded-Host": "factory.example.com"}
    )
    assert linked.status_code == 200, linked.text
    # With no automation URL yet, it waits; the watcher would call back where it was saved.
    assert linked.json()["state"] == "claimed"
    assert linked.json()["destination"]["relay_url"].endswith("://factory.example.com")
    for method, path in (("POST", "/disable"), ("POST", "/enable"), ("DELETE", "")):
        refused = await client.request(method, base + path, headers=lead)
        assert refused.status_code == 403, (method, path, refused.text)
    # A member who isn't a product approver can't either.
    assert (await client.post(base + "/disable", headers=_as_user(BOB))).status_code == 403

    slack = multi_user_app.state.project_slack_store
    binding = slack.set_state(project["id"], "active", actor_user_id=ALICE)
    SlackRelayStore(db_uri).commit(
        binding.id,
        delivered=[SlackEvent(THREAD, THREAD, "UZZANN", "Ann", "hi", "", True)],
        skipped=[],
        cursor=None,
    )
    reply = {"thread": THREAD, "message": "On it."}
    # Forged: naming the Lead's session in the header without its bound runner.
    forged = {ORCHESTRATION_SOURCE_HEADER: project["main_session_id"]}
    for user in (BOB, ALICE):
        response = await client.post(
            base + "/replies", json=reply, headers={**_as_user(user), **forged}
        )
        assert response.status_code == 403, (user, response.text)
    # A person with no agent header is refused too.
    assert (
        await client.post(base + "/replies", json=reply, headers=_as_user(ALICE))
    ).status_code == 403
    sent = await client.post(base + "/replies", json=reply, headers=lead)
    assert sent.status_code == 201, sent.text


async def test_only_the_owner_chooses_where_the_watcher_runs_and_calls_back(
    multi_user_client, db_uri
):
    client = multi_user_client
    project = await _project(client, ALICE, "zz-slack-owner (throwaway)")
    path = f"/v1/projects/{project['id']}"
    await client.post(f"{path}/shares", json={"user_id": BOB}, headers=_as_user(ALICE))
    made = await client.patch(
        path, json={"config": {"product_approvers": [BOB]}}, headers=_as_user(ALICE)
    )
    assert made.status_code == 200, made.text
    ours = {
        "automation_url": "https://automation.example.com/api/2.0/omnigent",
        "agent": "claude-native-ui",
    }
    owner = await client.put(
        f"{path}/slack",
        json={**LINK, "destination": ours},
        headers={**_as_user(ALICE), "X-Forwarded-Host": "factory.example.com"},
    )
    assert owner.status_code == 200, owner.text
    saved = owner.json()["destination"]

    # A product approver who isn't the owner can't add or change what the
    # owner's watcher runs as, where, or with which connections.
    bob = {**_as_user(BOB), "X-Forwarded-Host": "attacker.example.com"}
    for change in (
        {"automation_url": "https://other.example.com/api/2.0/omnigent"},
        {"agent": "polly"},
        {"agent": "0123456789abcdef0123456789abcdef"},
        {"mcp_services": "slack,github"},
        {"mcp_services": ""},
    ):
        refused = await client.put(
            f"{path}/slack", json={**LINK, "destination": {**ours, **change}}, headers=bob
        )
        assert refused.status_code == 403, (change, refused.text)
    # Resending the owner's values, or leaving them out, passes and keeps them.
    for destination in ({**ours, "model": "m1"}, {"model": "m1"}):
        kept = await client.put(
            f"{path}/slack", json={**LINK, "destination": destination}, headers=bob
        )
        assert kept.status_code == 200, kept.text
        assert kept.json()["destination"] == {**saved, "model": "m1"}

    # An agent, even the owner's own Lead, can't change them either.
    lead = _lead_headers(db_uri, project, ALICE)
    for change in ({"agent": "polly"}, {"mcp_services": "github"}):
        refused = await client.put(
            f"{path}/slack", json={**LINK, "destination": {**ours, **change}}, headers=lead
        )
        assert refused.status_code == 403, (change, refused.text)
    after = await client.get(f"{path}/slack", headers=_as_user(ALICE))
    assert after.json()["destination"] == {**saved, "model": "m1"}
