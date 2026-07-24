"""E2E: Polly sends Claude SDK goals through the normal message path."""

from __future__ import annotations

import json
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect


def _patch_session_as_polly_claude(page: Page, session_id: str) -> None:
    """Expose the seeded session to the browser as top-level Polly on Claude SDK."""

    def _handle(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != f"/v1/sessions/{session_id}":
            route.continue_()
            return

        response = route.fetch()
        payload = response.json()
        payload["agent_name"] = "polly"
        payload["harness"] = "claude-sdk"
        payload["parent_session_id"] = None
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    page.route("**/v1/sessions/**", _handle)


def test_polly_claude_goal_sends_native_command(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The Goal dialog validates input and posts the exact Claude slash command."""
    base_url, session_id = seeded_session
    _patch_session_as_polly_claude(page, session_id)

    def _ack_event(route: Route) -> None:
        route.fulfill(
            status=202,
            content_type="application/json",
            body=json.dumps({"queued": True, "item_id": "ci_goal_e2e"}),
        )

    page.route(f"**/v1/sessions/{session_id}/events", _ack_event)
    page.goto(f"{base_url}/c/{session_id}")

    goal_toggle = page.get_by_test_id("goal-toggle")
    expect(goal_toggle).to_be_visible(timeout=15_000)
    expect(goal_toggle).to_have_attribute("aria-label", "Start Claude goal")
    goal_toggle.click()

    start_goal = page.get_by_test_id("goal-start")
    start_goal.click()
    expect(page.get_by_text("Goal condition cannot be empty.")).to_be_visible()

    condition = "Finish the implementation and pass all tests"
    page.get_by_test_id("goal-condition").fill(f"  {condition}  ")
    with page.expect_request(
        lambda request: (
            request.method == "POST"
            and urlparse(request.url).path == f"/v1/sessions/{session_id}/events"
        )
    ) as sent:
        start_goal.click()

    body = sent.value.post_data_json
    assert body["data"]["content"][0]["text"] == f"/goal {condition}"
    expect(page.get_by_role("dialog", name="Goal")).to_have_count(0)
