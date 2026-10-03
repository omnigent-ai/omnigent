"""E2E coverage for the workspace push-panel resize seam."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.e2e_ui._touch import touch_drag_between

_FINE_GUTTER_PX = 10


def _open_execution_logs_panel(page: Page) -> None:
    expect(page.get_by_test_id("execution-logs-card")).to_be_visible(timeout=30_000)
    page.get_by_test_id("execution-log-row-main").click()
    expect(page.get_by_test_id("execution-logs-panel")).to_be_visible()


def test_workspace_panel_pointer_resize_persists_without_annexing_chat(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Resize the workspace panel while adjacent chat input stays inert."""
    base_url, session_id = seeded_session
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{base_url}/c/{session_id}?debug=1")
    _open_execution_logs_panel(page)

    panel = page.get_by_test_id("execution-logs-panel")
    handle = page.get_by_role("separator", name="Resize panel")
    initial_width = panel.bounding_box()["width"]
    handle_box = handle.bounding_box()

    touch_drag_between(
        page,
        start=(handle_box["x"] + handle_box["width"] / 2, handle_box["y"] + 100),
        end=(handle_box["x"] - 80, handle_box["y"] + 100),
    )

    resized_width = panel.bounding_box()["width"]
    assert resized_width >= initial_width + 70

    panel_box = panel.bounding_box()
    probe_x = panel_box["x"] - _FINE_GUTTER_PX - 8
    probe_y = panel_box["y"] + panel_box["height"] / 2
    assert panel.evaluate(
        """(panel, point) => {
            const target = document.elementFromPoint(point.x, point.y);
            if (!target || panel.contains(target) || target.closest('[role="separator"]')) {
                return false;
            }
            target.addEventListener(
                'pointerdown',
                () => document.documentElement.dataset.resizeProbeReceived = 'true',
                { once: true },
            );
            return true;
        }""",
        {"x": probe_x, "y": probe_y},
    ), "chat-side probe landed on the panel resize handle"
    page.mouse.move(probe_x, probe_y)
    page.mouse.down()
    page.mouse.move(probe_x - 12, probe_y, steps=2)
    page.mouse.up()
    expect(page.locator("html")).to_have_attribute("data-resize-probe-received", "true")
    assert abs(panel.bounding_box()["width"] - resized_width) <= 1

    page.reload()
    _open_execution_logs_panel(page)
    persisted_width = page.get_by_test_id("execution-logs-panel").bounding_box()["width"]
    assert abs(persisted_width - resized_width) <= 1
