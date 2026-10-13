"""The create kickoff declines a gateway pick an off-gateway pane can't run.

The first turn of a session created with ``initial_items`` routes like any
later turn, so the kickoff must thread the agent spec into the forwarder and
veto a gateway-hosted pick the pane reaches only direct from the vendor.
Without the spec the forwarder would pin that unrunnable model, failing the
kickoff and every later turn -- the same defect the ``POST /events`` path
already guards.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import httpx
import pytest

from omnigent.server.smart_routing import RoutingResult
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import (
    FakeCaps,
    FakeRoutingClient,
    RoutingCaptureRunnerClient,
    create_test_agent,
    routing_decisions,
)

pytestmark = pytest.mark.asyncio

ROUTED_MODEL = "databricks-claude-opus-4-8"


async def test_create_kickoff_declines_a_gateway_pick_an_offgateway_pane_cannot_run(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A kickoff turn routes, so an unrunnable gateway pick is left unpinned.

    A direct-vendor ``claude-sdk`` session created with ``initial_items`` and
    Smart Routing on draws a ``databricks-*`` pick it cannot run. The kickoff
    must thread the spec through the forwarder and decline the pick: the row
    stays clear so later turns route again, and no gateway model reaches the
    runner body.
    """
    import omnigent.server.routes.sessions as sessions_module

    agent = await create_test_agent(
        client,
        name="offgateway-kickoff",
        include_llm=False,
        executor={
            "config": {"harness": "claude-sdk"},
            "model": "claude-sonnet-4-20250514",
            "auth": {"type": "api_key", "api_key": "mock-key"},
        },
    )

    forwarded: list[dict[str, Any]] = []

    async def _stub_runner(*_: Any, **__: Any) -> RoutingCaptureRunnerClient:
        return RoutingCaptureRunnerClient(forwarded)

    async def _ensure_relay_ready(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(sessions_module, "_get_runner_client", _stub_runner)
    monkeypatch.setattr(sessions_module, "_ensure_runner_relay_ready", _ensure_relay_ready)

    routing_client = FakeRoutingClient(
        RoutingResult(model=ROUTED_MODEL, rationale="deep refactor", harness="claude_code")
    )
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=routing_client)):
        resp = await client.post(
            "/v1/sessions",
            json={
                "agent_id": agent["id"],
                "cost_control_mode_override": "on",
                "initial_items": [
                    {
                        "type": "message",
                        "data": {
                            "role": "user",
                            "content": [{"type": "input_text", "text": "refactor auth"}],
                        },
                    }
                ],
            },
        )
    assert resp.status_code == 201, resp.text
    sid = resp.json()["id"]

    conv_store = SqlAlchemyConversationStore(db_uri)
    refreshed = conv_store.get_conversation(sid)
    assert refreshed is not None
    # The unrunnable gateway pick was never pinned by the kickoff turn.
    assert refreshed.model_override is None

    decisions = routing_decisions(conv_store, sid)
    assert len(decisions) == 1, (
        f"Expected one declined decision for the kickoff turn; got {len(decisions)}. "
        "The spec must thread through the kickoff so the turn routes and vetoes."
    )
    assert decisions[0].data.scope == "turn"
    assert decisions[0].data.applied is False
    assert "gateway" in decisions[0].data.rationale.lower()

    assert forwarded, "The kickoff never forwarded a turn to the runner."
    assert all(body.get("model_override") != ROUTED_MODEL for body in forwarded), (
        "An unrunnable gateway pick reached the runner body: "
        f"{[body.get('model_override') for body in forwarded]!r}"
    )
