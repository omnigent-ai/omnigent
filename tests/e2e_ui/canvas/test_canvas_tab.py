"""UI e2e: the right-rail Canvas tab appears once a canvas is set (#2).

Seeds a canvas via ``PUT /v1/canvas/{id}``, opens the session, and asserts the
Canvas tab is offered in the Workspace rail — covering ``useCanvas`` +
``WorkspacePanel``'s canvas gate + ``CanvasPanel`` in a real browser.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import Page, expect


def test_canvas_tab_appears_when_canvas_set(page: Page, seeded_session: tuple[str, str]) -> None:
    base_url, session_id = seeded_session
    httpx.put(
        f"{base_url}/v1/canvas/{session_id}",
        json={"title": "Demo", "content": "<h1>hello</h1>", "content_type": "html"},
        timeout=10.0,
    ).raise_for_status()

    page.goto(f"{base_url}/c/{session_id}")
    rail = page.get_by_role("complementary", name="Workspace")
    expect(rail.get_by_role("tab", name="Canvas")).to_be_visible(timeout=30_000)
