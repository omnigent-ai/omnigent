"""Slow or failed subtree usage must not prevent opening a conversation."""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, Route, expect


@pytest.mark.min_server_version("0.15.0")
@pytest.mark.parametrize("usage_status", [200, 503], ids=["delayed-success", "failure"])
def test_session_opens_before_subtree_usage(
    page: Page,
    seeded_session: tuple[str, str],
    usage_status: int,
) -> None:
    """The composer works while usage waits, and unknown cost never becomes zero."""
    base_url, session_id = seeded_session
    usage_url = f"{base_url}/v1/sessions/{session_id}/usage"
    pending_routes: list[Route] = []

    def hold_usage(route: Route) -> None:
        pending_routes.append(route)

    page.route(usage_url, hold_usage)
    with page.expect_request(usage_url, timeout=30_000):
        page.goto(f"{base_url}/c/{session_id}", wait_until="domcontentloaded")

    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_editable(timeout=30_000)
    composer.fill("An unsent draft while usage is loading.")
    expect(composer).to_have_value("An unsent draft while usage is loading.")
    assert len(pending_routes) == 1

    trigger = page.get_by_test_id("agent-info-trigger")
    trigger.focus()
    trigger.press("Enter")
    panel = page.get_by_test_id("agent-info-panel")
    expect(panel).to_be_visible()
    expect(panel.get_by_test_id("agent-info-session-cost")).to_have_count(0)
    expect(panel.get_by_test_id("agent-info-usage-by-model")).to_have_count(0)

    body = (
        {
            "id": session_id,
            "total_cost_usd": 3.5,
            "usage_by_model": {
                "parent-model": {"input_tokens": 100, "total_cost_usd": 1.0},
                "child-model": {"input_tokens": 200, "total_cost_usd": 2.5},
            },
        }
        if usage_status == 200
        else {"error": {"code": "internal_error", "message": "Usage temporarily unavailable"}}
    )
    with page.expect_response(usage_url) as response:
        pending_routes[0].fulfill(
            status=usage_status,
            content_type="application/json",
            body=json.dumps(body),
        )
    assert response.value.status == usage_status

    if usage_status == 200:
        expect(panel.get_by_test_id("agent-info-session-cost")).to_have_text("$3.50")
        breakdown = panel.get_by_test_id("agent-info-usage-by-model")
        breakdown.locator("summary").click()
        expect(breakdown.get_by_test_id("agent-info-model-parent-model")).to_contain_text("$1.00")
        expect(breakdown.get_by_test_id("agent-info-model-child-model")).to_contain_text("$2.50")
    else:
        page.keyboard.press("Escape")
        composer.fill("Still editable after the usage request failed.")
        expect(composer).to_have_value("Still editable after the usage request failed.")
        trigger.focus()
        trigger.press("Enter")
        expect(panel).to_be_visible()
        expect(panel.get_by_test_id("agent-info-session-cost")).to_have_count(0)
        expect(panel.get_by_test_id("agent-info-usage-by-model")).to_have_count(0)
