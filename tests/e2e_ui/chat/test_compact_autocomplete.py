"""Claude's /compact completes on Tab and follows normal message queueing."""

import re

import pytest
from playwright.sync_api import Page, Route, expect


@pytest.mark.parametrize("busy", [False, True])
@pytest.mark.parametrize("harness", ["claude-native", "claude-sdk"])
def test_tab_completes_compact_until_explicit_submit(
    page: Page, seeded_session: tuple[str, str], busy: bool, harness: str
) -> None:
    base_url, session_id = seeded_session

    response = page.request.get(f"{base_url}/v1/sessions/{session_id}")
    assert response.ok
    payload = response.json()
    payload["harness"] = harness
    if harness == "claude-native":
        payload["labels"] = {
            **payload.get("labels", {}),
            "omnigent.wrapper": "claude-code-native-ui",
        }
    if busy:
        payload["status"] = "running"

    def claude_snapshot(route: Route) -> None:
        if route.request.method == "GET":
            route.fulfill(json=payload)
        else:
            route.continue_()

    page.route(re.compile(rf"/v1/sessions/{session_id}(?:\?.*)?$"), claude_snapshot)
    posts: list[dict] = []

    def accept_message(route: Route) -> None:
        posts.append(route.request.post_data_json)
        route.fulfill(status=202, json={"queued": False})

    page.route(f"**/v1/sessions/{session_id}/events", accept_message)
    page.goto(f"{base_url}/c/{session_id}?view=chat")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("/comp")
    composer.press("Tab")
    expect(composer).to_have_value("/compact ")
    expect(composer).to_be_focused()
    assert posts == []

    if busy:
        composer.press("Enter")
        strip = page.get_by_test_id("composer-queued-strip")
        expect(strip).to_contain_text("/compact")
        assert posts == []
        with page.expect_response(f"**/v1/sessions/{session_id}/events"):
            strip.get_by_role("button", name="Send queued message now").click()
        expect(strip).not_to_be_visible()
    else:
        with page.expect_response(f"**/v1/sessions/{session_id}/events"):
            composer.press("Enter")
    expect(composer).to_have_value("")
    if harness == "claude-sdk":
        assert posts == [{"type": "compact", "data": {}}]
    else:
        assert [post["type"] for post in posts] == ["message"]
        assert posts[0]["data"]["content"] == [{"type": "input_text", "text": "/compact"}]
    page.unroute_all(behavior="wait")
