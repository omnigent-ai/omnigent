"""Tab completes a built-in command without dispatching it."""

from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.chat.test_claude_model_picker import _patch_session_as_claude_native


def test_tab_completes_compact_until_explicit_submit(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    base_url, session_id = seeded_session
    _patch_session_as_claude_native(page, session_id)
    posts: list[dict] = []

    def accept_control(route: Route) -> None:
        posts.append(route.request.post_data_json)
        route.fulfill(status=202, json={"queued": False})

    page.route(f"**/v1/sessions/{session_id}/events", accept_control)
    page.goto(f"{base_url}/c/{session_id}?view=chat")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("/comp")
    composer.press("Tab")
    expect(composer).to_have_value("/compact ")
    expect(composer).to_be_focused()
    assert posts == []

    with page.expect_response(f"**/v1/sessions/{session_id}/events"):
        composer.press("Enter")
    expect(composer).to_have_value("")
    assert [post["type"] for post in posts] == ["compact"]
    page.unroute_all(behavior="wait")
