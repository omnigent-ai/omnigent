"""Full-window views beside the macOS desktop shell's window controls.

These views portal outside the app shell, so they must clear its title bar
themselves. The tests emulate the macOS shell in Chromium with ``_mac_shell``.
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.desktop._mac_shell import MAC_SHELL_INIT_SCRIPT, intersects_window_controls

pytestmark = pytest.mark.min_server_version("0.23.0")

_TOPMOST_IS_INSIDE_SCRIPT = """
([x, y, testId]) => Boolean(
  document.elementFromPoint(x, y)?.closest(`[data-testid="${testId}"]`)
)
"""


def test_workflow_graph_view_clears_the_title_bar(page: Page, live_server: str) -> None:
    base_url = live_server
    name = f"Validate release {uuid4().hex[:8]}"
    steps = [
        {"id": "prepare", "name": "Prepare checkout", "goal": "Clean tree", "depends_on": []},
        {"id": "verify", "name": "Verify build", "goal": "Run checks", "depends_on": ["prepare"]},
    ]
    with httpx.Client(base_url=base_url, timeout=10.0) as client:
        response = client.post(
            "/v1/workflows", json={"name": name, "description": "Ship a release.", "steps": steps}
        )
        response.raise_for_status()
        workflow_id = response.json()["id"]
        try:
            page.add_init_script(MAC_SHELL_INIT_SCRIPT)
            page.set_viewport_size({"width": 1280, "height": 860})
            page.goto(f"{base_url}/workflows/{workflow_id}")
            expect(page.locator("html")).to_have_attribute("data-electron-mac", "true")
            graph = page.get_by_test_id("workflow-steps-graph")
            expect(graph.get_by_test_id("workflow-step-node")).to_have_count(2)

            title_bar_actions = page.locator(".electron-sidebar-header-actions")
            expect(title_bar_actions.get_by_role("button").first).to_be_visible()
            action_centers = [
                (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                for button in title_bar_actions.get_by_role("button").all()
                if (box := button.bounding_box())
            ]
            assert action_centers, "no title-bar actions were rendered"

            graph.get_by_role("button", name="View fullscreen", exact=True).click()
            overlay = page.get_by_test_id("workflow-graph-overlay")
            title = overlay.get_by_role("heading", name=name, exact=True)
            expect(title).to_be_visible()

            assert not intersects_window_controls(title.bounding_box()), (
                f"graph view title {title.bounding_box()} sits under the window controls"
            )
            # The app's own title-bar actions must not paint over, or take clicks from, the view.
            covered = [
                center
                for center in action_centers
                if not page.evaluate(
                    _TOPMOST_IS_INSIDE_SCRIPT, [*center, "workflow-graph-overlay"]
                )
            ]
            assert covered == [], f"title-bar actions show over the graph view at {covered}"

            page.keyboard.press("Escape")
            expect(overlay).to_be_hidden()
            expect(title_bar_actions.get_by_role("button").first).to_be_visible()
        finally:
            client.delete(f"/v1/workflows/{workflow_id}")
