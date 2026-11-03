"""A clicked canvas tile keeps its selected highlight while the canvas updates.

Clicking a session card on the Canvas selects it: React Flow marks the node
``selected`` and the card shows the accent border + ring. That highlight must
survive the canvas's routine list churn — the 30s poll, the window-focus
refresh, and live session updates arriving over the sessions stream — because
each of those rebuilds the React Flow nodes from the fresh session list.
"""

from __future__ import annotations

import re

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.session import post_session_bundle
from tests.e2e_ui.conftest import _build_hello_world_bundle
from tests.e2e_ui.sessions.test_canvas_page import _stub_server_info

SELECTED_NODE_CLASS = re.compile(r"(?:^|\s)selected(?:\s|$)")
SELECTED_CARD_CLASS = re.compile(r"(?:^|\s)border-brand-accent(?:\s|$)")


def _create_session(live_server: str, title: str) -> str:
    """Create a real top-level session and give it a stable title."""
    create = post_session_bundle(
        httpx.post, f"{live_server}/v1/sessions", _build_hello_world_bundle(), timeout=30.0
    )
    create.raise_for_status()
    session_id = create.json()["session_id"]
    httpx.patch(
        f"{live_server}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    ).raise_for_status()
    return session_id


@pytest.mark.parametrize("filler_count", [0, 24], ids=["duplicate-titles", "virtualized-nodes"])
def test_clicked_card_keeps_selected_highlight_across_live_updates(
    page: Page,
    live_server: str,
    filler_count: int,
) -> None:
    """Clicking a card highlights it, and an unrelated live update keeps it lit."""
    target_id = _create_session(live_server, "Canvas highlight target")
    duplicate_id = _create_session(live_server, "Canvas highlight target")
    neighbor_id = _create_session(live_server, "Canvas neighbor before")
    for index in range(filler_count):
        _create_session(live_server, f"Canvas filler {index}")

    _stub_server_info(page, canvas=True)
    page.goto(f"{live_server}/canvas")

    target_node = page.get_by_test_id(f"rf__node-{target_id}")
    target_card = target_node.get_by_test_id("session-card")
    neighbor_card = page.get_by_test_id(f"rf__node-{neighbor_id}").get_by_test_id("session-card")
    expect(target_card).to_be_visible(timeout=30_000)
    expect(neighbor_card).to_be_visible()
    # The initial full list load has settled once the header spinner is gone.
    expect(page.get_by_role("status", name="Loading sessions")).to_have_count(0)

    target_card.click()
    # Opening the session narrows the canvas and keeps the viewport, so with a
    # long session list cards can fall off-screen (and stop rendering). Refit.
    page.get_by_role("button", name="Fit view").click()
    expect(target_node).to_have_class(SELECTED_NODE_CLASS)
    expect(target_card).to_have_class(SELECTED_CARD_CLASS)
    # Hold briefly so a viewer of the recording sees the highlight before the
    # update lands.
    page.wait_for_timeout(1_000)

    # An unrelated session changes; the update reaches its card over the
    # sessions stream (well inside the 30s poll) and rebuilds the canvas nodes.
    httpx.patch(
        f"{live_server}/v1/sessions/{neighbor_id}",
        json={"title": "Canvas neighbor after"},
        timeout=10.0,
    ).raise_for_status()
    expect(neighbor_card).to_contain_text("Canvas neighbor after", timeout=10_000)
    # List churn can move the selected node outside React Flow's rendered viewport.
    page.get_by_role("button", name="Fit view").click()

    # The clicked card is still the selection; the unrelated update must not
    # clear its highlight.
    expect(target_node).to_have_class(SELECTED_NODE_CLASS)
    expect(target_card).to_have_class(SELECTED_CARD_CLASS)
    expect(page.get_by_test_id(f"rf__node-{duplicate_id}")).not_to_have_class(SELECTED_NODE_CLASS)
