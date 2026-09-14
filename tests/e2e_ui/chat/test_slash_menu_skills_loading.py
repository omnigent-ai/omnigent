"""The open slash menu settles skill discovery through the real SSE consumer."""

from __future__ import annotations

from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.chat.test_model_flows_contract import _install_stream_controller, _push_sse


@pytest.mark.parametrize("empty", [False, True], ids=["skills-found", "no-skills"])
def test_open_slash_menu_resolves_skills_on_sse(
    page: Page,
    seeded_session: tuple[str, str],
    empty: bool,
) -> None:
    """A delayed catalog updates an already-open menu, including an empty result."""
    base_url, session_id = seeded_session
    session_path = f"/v1/sessions/{session_id}"
    resolved = False
    snapshot_reads = 0

    def snapshot(route: Route) -> None:
        nonlocal snapshot_reads
        if urlparse(route.request.url).path != session_path or route.request.method != "GET":
            route.continue_()
            return
        response = route.fetch()
        body = response.json()
        snapshot_reads += 1
        body["skills_status"] = "ready" if resolved else "loading"
        body["skills"] = (
            [{"name": "code-review", "description": "Review the current change"}]
            if resolved and not empty
            else []
        )
        route.fulfill(response=response, json=body)

    page.route(f"**{session_path}*", snapshot)
    _install_stream_controller(page, session_id)
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("/")
    expect(page.get_by_test_id("slash-menu-item-help")).to_be_visible()
    expect(page.get_by_text("Loading skills…", exact=True)).to_be_visible()

    # Filter out built-ins: the loading subsection must keep the menu open.
    composer.fill("/review")
    expect(page.get_by_text("Loading skills…", exact=True)).to_be_visible()
    reads_before_event = snapshot_reads
    resolved = True
    _push_sse(page, "session.skills", {"type": "session.skills", "conversation_id": session_id})

    # The fallback read waits five seconds; this must settle directly from SSE.
    expect(page.get_by_text("Loading skills…", exact=True)).not_to_be_visible(timeout=3_000)
    assert snapshot_reads > reads_before_event
    expect(composer).to_have_value("/review")
    if empty:
        expect(page.get_by_text("No matching skills", exact=True)).to_be_visible()
    else:
        expect(page.get_by_test_id("slash-menu-item-code-review")).to_be_visible()
        composer.press("Tab")
        expect(composer).to_have_value("/code-review ")
