"""UI e2e: the right-rail Canvas tab renders an agent-authored canvas (#2).

Seeds a canvas via ``PUT /v1/canvas/{id}`` (the same endpoint the runner's
``set_canvas`` proxy writes through), opens the conversation, and asserts the
Canvas tab appears and renders the title + content — covering the useCanvas
hook, the tab gating (``showCanvasTab``), and CanvasPanel in a real browser.
Markdown content is used so the body is directly assertable in the DOM (HTML
renders inside a sandboxed iframe).
"""

from __future__ import annotations

import uuid

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail


def test_canvas_tab_renders_seeded_canvas(
    page: Page, live_server: str, seeded_session: tuple[str, str]
) -> None:
    base_url, session_id = seeded_session
    marker = uuid.uuid4().hex[:8]
    httpx.put(
        f"{live_server}/v1/canvas/{session_id}",
        json={
            "title": f"Report {marker}",
            "content": f"canvas body {marker}",
            "content_type": "markdown",
        },
        timeout=10.0,
    ).raise_for_status()

    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    # The Canvas tab only renders once the conversation HAS a canvas.
    tab = page.get_by_role("tab", name="Canvas")
    expect(tab).to_be_visible(timeout=30_000)
    tab.click()
    expect(page.get_by_text(f"Report {marker}")).to_be_visible(timeout=30_000)
    expect(page.get_by_text(f"canvas body {marker}")).to_be_visible(timeout=30_000)
