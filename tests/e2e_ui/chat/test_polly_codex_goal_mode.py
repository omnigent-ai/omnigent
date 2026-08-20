"""E2E: Polly sends Codex goals through the normal message path."""

from __future__ import annotations

import json
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect


def _patch_session_as_polly_codex(page: Page, session_id: str) -> None:
    """Expose the seeded session as top-level Polly on Codex."""

    def _handle(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != f"/v1/sessions/{session_id}":
            route.continue_()
            return

        response = route.fetch()
        payload = response.json()
        payload["agent_name"] = "polly"
        payload["harness"] = "codex"
        payload["parent_session_id"] = None
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    page.route("**/v1/sessions/**", _handle)


def test_polly_codex_goal_sends_native_command(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The Goal dialog posts the exact Codex slash command."""
    base_url, session_id = seeded_session
    _patch_session_as_polly_codex(page, session_id)

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
    expect(goal_toggle).to_have_attribute("aria-label", "Start Codex goal")
    goal_toggle.click()

    condition = "Finish the implementation and pass all tests"
    page.get_by_test_id("goal-condition").fill(f"  {condition}  ")
    with page.expect_request(
        lambda request: (
            request.method == "POST"
            and urlparse(request.url).path == f"/v1/sessions/{session_id}/events"
        )
    ) as sent:
        page.get_by_test_id("goal-start").click()

    body = sent.value.post_data_json
    assert body["data"]["content"][0]["text"] == f"/goal {condition}"
    expect(page.get_by_role("dialog", name="Goal")).to_have_count(0)
