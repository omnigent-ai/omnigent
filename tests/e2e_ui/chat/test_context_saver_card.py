"""E2E: a completed Focused Read explains its worker-model routing."""

from __future__ import annotations

import json

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import seed_committed_items

_CONTEXT_SAVER_CARD = '[data-testid="context-saver-card"]'
_WORKER_ROUTE = "databricks/context-saver-cheap"
_REPORTED_MODEL = "databricks-glm-5-3-flash"


def test_context_saver_card_shows_worker_route_and_reported_model(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Hydrated MCP output keeps the primary and worker models distinct."""
    from omnigent.entities import (
        FunctionCallData,
        FunctionCallOutputData,
        MessageData,
        NewConversationItem,
    )

    base_url, session_id = seeded_session
    response_id = "resp_context_saver"
    call_id = "call_context_saver"
    result = {
        "technique": "focused_read",
        "model_routing": {
            "primary_models": "all",
            "worker_route": _WORKER_ROUTE,
            "worker_model_reported": _REPORTED_MODEL,
            "route_provider": "databricks",
            "non_databricks_source_sharing_allowed": False,
        },
        "content": "The target is on line 351.",
        "failure": None,
    }
    wrapped_output = json.dumps([{"type": "text", "text": json.dumps(result)}])

    seed_committed_items(
        session_id,
        [
            NewConversationItem(
                type="message",
                response_id=response_id,
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "Find the target in large.py."}],
                ),
            ),
            NewConversationItem(
                type="function_call",
                response_id=response_id,
                data=FunctionCallData(
                    agent="claude-native-ui",
                    name="mcp__omnigent__sys_context_read",
                    arguments=json.dumps(
                        {"paths": ["large.py"], "question": "Where is the target?"}
                    ),
                    call_id=call_id,
                ),
            ),
            NewConversationItem(
                type="function_call_output",
                response_id=response_id,
                data=FunctionCallOutputData(call_id=call_id, output=wrapped_output),
            ),
            NewConversationItem(
                type="message",
                response_id=response_id,
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": "Focused read complete."}],
                    agent="claude-native-ui",
                ),
            ),
        ],
    )

    page.goto(f"{base_url}/c/{session_id}")

    card = page.locator(_CONTEXT_SAVER_CARD)
    expect(card).to_have_count(1, timeout=30_000)
    expect(card).to_be_visible()
    expect(card).to_have_attribute("data-state-kind", "complete")
    expect(card).to_contain_text("Context Saver")
    expect(card).to_contain_text("Focused Read complete")
    expect(card.get_by_text("All primary models", exact=True)).to_be_visible()
    expect(card.get_by_text(_WORKER_ROUTE, exact=True)).to_be_visible()
    expect(card.get_by_text("Route provider", exact=True)).to_be_visible()
    expect(card.get_by_text("databricks", exact=True)).to_be_visible()
    expect(card.get_by_text("Provider-reported model", exact=True)).to_be_visible()
    expect(card.get_by_text(_REPORTED_MODEL, exact=True)).to_be_visible()
    expect(
        card.get_by_text("Focused Read only — your primary model does not change.", exact=True)
    ).to_be_visible()

    raw_content = card.get_by_text("The target is on line 351.", exact=False)
    expect(raw_content).to_have_count(0)
    card.get_by_test_id("context-saver-raw-toggle").click()
    expect(raw_content).to_be_visible()
