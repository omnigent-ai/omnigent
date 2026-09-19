"""E2E: Smart Routing must keep a session's prior model across a resume.

The user journey both tests drive, through the same HTTP surface the product
uses (``POST /v1/sessions``, ``PATCH /v1/sessions/{id}``, and the
``hooks/route-turn`` relay a native pane's first-prompt hook fires):

1. create a session with Smart Routing on (model-level routing, native pane);
2. run it on a model — either the router's own first-prompt pick, or a model
   the user chose themselves (the picker / the pane's ``/model`` persist);
3. close the pane and resume the session — the resumed pane's bridge dir is
   fresh, so its first-prompt hook makes the route-turn round trip again;
4. type a prompt: the session must stay on the model it was already using.

The failure being guarded: on resume the hook re-runs routing and PICKS A
MODEL AGAIN, switching the session off the model the user was already on.
A session whose model was pinned by the user (Smart Routing left on, no
routing decision recorded yet) loses that pin to the router's fresh pick.

No real TUI or gateway is needed: the route-turn relay is the exact request a
resumed native pane sends, and the router is the canned
:class:`~tests.server.helpers.FakeRoutingClient`, so the assertions can name
exact models.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from omnigent.server.smart_routing import RoutingResult
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import FakeCaps, FakeRoutingClient, create_test_agent

pytestmark = pytest.mark.asyncio

# The model the user was already using in the session (their own pick).
USER_MODEL = "claude-opus-4-7"
# The router's fresh pick — a different model, so a re-pick is observable.
ROUTER_PICK = "databricks-claude-opus-4-8"


async def _smart_routing_session(client: httpx.AsyncClient, *, agent_name: str) -> str:
    """Create a top-level session with model-level Smart Routing on.

    The same row ``omnigent claude --smart-routing`` and the web landing's
    Model-row toggle create: ``cost_control_mode_override="on"`` and no
    ``model_override``.

    :param client: Test HTTP client.
    :param agent_name: Unique agent name for the session's bundle.
    :returns: The new session id.
    """
    agent = await create_test_agent(client, name=agent_name)
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "cost_control_mode_override": "on"},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _resume_first_prompt(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    live_model: str,
) -> dict[str, object]:
    """Fire the route-turn relay the resumed pane's first prompt sends.

    A resumed pane starts with a fresh bridge dir (no local route-once
    marker), so its UserPromptSubmit hook always makes this round trip; the
    server-side gates decide whether routing runs again.

    :param client: Test HTTP client.
    :param session_id: The resumed session.
    :param live_model: The model the pane is live on, from the hook payload.
    :returns: The decision JSON.
    """
    caps = FakeCaps(
        routing_client=FakeRoutingClient(
            RoutingResult(model=ROUTER_PICK, rationale="fresh pick", harness="claude_code")
        )
    )
    with patch("omnigent.runtime._globals._caps", new=caps):
        resp = await client.post(
            f"/v1/sessions/{session_id}/hooks/route-turn",
            json={
                "harness": "claude-native",
                "prompt": "continue where we left off",
                "model": live_model,
            },
        )
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def _routing_decisions(db_uri: str, session_id: str) -> list[object]:
    store = SqlAlchemyConversationStore(db_uri)
    return [
        item
        for item in store.list_items(session_id).data
        if getattr(item, "type", None) == "routing_decision"
    ]


async def test_resume_keeps_the_users_pinned_model(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A resumed session stays on the model the user picked, not a re-pick.

    The user chose a model themselves (picker PATCH / the pane's ``/model``
    persist) and left Smart Routing on. Resuming and typing must not hand the
    prompt back to the router: the session already HAS the model the user was
    using, and re-picking switches them off it.
    """
    session_id = await _smart_routing_session(client, agent_name="routing-resume-user-pin")

    # The user's own model pick, persisted the way the picker persists it.
    pin = await client.patch(f"/v1/sessions/{session_id}", json={"model_override": USER_MODEL})
    assert pin.status_code == 200, pin.text
    assert pin.json()["model_override"] == USER_MODEL

    # Close + resume, then the first prompt fires the hook round trip.
    decision = await _resume_first_prompt(client, session_id, live_model=USER_MODEL)

    # The session must keep the model the user was already using.
    assert decision["action"] != "route", (
        "smart routing re-picked a model on resume instead of keeping the "
        f"session's prior model: {decision}"
    )
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.get_conversation(session_id)
    assert conv is not None
    assert conv.model_override == USER_MODEL, (
        f"resume overwrote the user's model with the router's fresh pick: {conv.model_override!r}"
    )


async def test_resume_does_not_reroute_an_already_routed_session(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A session the router already decided is not re-decided on resume.

    First prompt routes and pins; the resume's first prompt makes the same
    round trip and must be allowed through on the recorded decision — same
    model, no second decision row.
    """
    session_id = await _smart_routing_session(client, agent_name="routing-resume-routed-once")

    # First prompt of the original run: the router picks and pins.
    first = await _resume_first_prompt(client, session_id, live_model="claude-sonnet-4-5")
    assert first["action"] == "route", first
    assert first["model"] == ROUTER_PICK
    assert len(_routing_decisions(db_uri, session_id)) == 1

    # Close + resume: the fresh pane's first prompt repeats the round trip.
    second = await _resume_first_prompt(client, session_id, live_model=ROUTER_PICK)

    assert second["action"] == "allow", (
        f"resume re-ran routing on an already-routed session: {second}"
    )
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.get_conversation(session_id)
    assert conv is not None
    assert conv.model_override == ROUTER_PICK
    # Still exactly one decision: the resume claimed nothing new.
    assert len(_routing_decisions(db_uri, session_id)) == 1
