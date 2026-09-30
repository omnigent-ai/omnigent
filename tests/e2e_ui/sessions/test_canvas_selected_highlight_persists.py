"""A clicked canvas tile keeps its selected highlight while the canvas updates.

Clicking a session card on the Canvas selects it: React Flow marks the node
``selected`` and the card shows the accent border + ring. That highlight must
survive the canvas's routine list churn — the 30s poll, the window-focus
refresh, and live session updates arriving over the sessions stream — because
each of those rebuilds the React Flow nodes from the fresh session list.
"""

from __future__ import annotations

import json
import re

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _build_hello_world_bundle
from tests.e2e_ui.sessions.test_canvas_page import _stub_server_info

SELECTED_NODE_CLASS = re.compile(r"(?:^|\s)selected(?:\s|$)")
SELECTED_CARD_CLASS = re.compile(r"(?:^|\s)border-brand-accent(?:\s|$)")


def _create_session(live_server: str, title: str) -> str:
    """Create a real top-level session and give it a stable title."""
    create = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", _build_hello_world_bundle(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = create.json()["session_id"]
    httpx.patch(
        f"{live_server}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    ).raise_for_status()
    return session_id


def test_clicked_card_keeps_selected_highlight_across_live_updates(
    page: Page,
    live_server: str,
) -> None:
    """Clicking a card highlights it, and an unrelated live update keeps it lit."""
    _create_session(live_server, "Canvas highlight target")
    neighbor_id = _create_session(live_server, "Canvas neighbor before")

    _stub_server_info(page, canvas=True)
    page.goto(f"{live_server}/canvas")

    target_card = page.get_by_test_id("session-card").filter(has_text="Canvas highlight target")
    expect(target_card).to_be_visible(timeout=30_000)
    expect(
        page.get_by_test_id("session-card").filter(has_text="Canvas neighbor before")
    ).to_be_visible()
    # The initial full list load has settled once the header spinner is gone.
    expect(page.get_by_role("status", name="Loading sessions")).to_have_count(0)

    target_card.click()
    target_node = page.locator(".react-flow__node").filter(has_text="Canvas highlight target")
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
    expect(
        page.get_by_test_id("session-card").filter(has_text="Canvas neighbor after")
    ).to_be_visible(timeout=10_000)

    # The clicked card is still the selection; the unrelated update must not
    # clear its highlight.
    expect(target_node).to_have_class(SELECTED_NODE_CLASS)
    expect(target_card).to_have_class(SELECTED_CARD_CLASS)
