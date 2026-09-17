"""Touch resizing for the inline Workspace panel."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.e2e_ui._touch import touch_drag_between
from tests.e2e_ui.conftest import open_right_rail, seed_committed_turn

_VIEWPORT = {"width": 1280, "height": 700}
# Tablet / unfolded-foldable width: md+ layout with the sidebar open by
# default, where the old clamp left the rail no drag range at all.
_TABLET_VIEWPORT = {"width": 1024, "height": 720}
_GUTTER = "[data-workspace-panel-resize-gutter]"
_STORAGE_KEY = "omnigent:session-workspace-state"


def _panel_width(page: Page) -> float:
    box = page.get_by_role("complementary", name="Workspace").bounding_box()
    assert box is not None
    return box["width"]


def _stored_width(page: Page, session_id: str) -> float | None:
    return page.evaluate(
        """([key, id]) => {
          const entries = JSON.parse(localStorage.getItem(key) || "[]");
          return entries.find((entry) => entry.id === id)?.state?.widthPx ?? null;
        }""",
        [_STORAGE_KEY, session_id],
    )


def test_resize_gutter_owns_only_its_capped_hit_slivers(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The gutter owns its seam slivers without covering adjacent controls."""
    base_url, session_id = seeded_session
    page.set_viewport_size(_VIEWPORT)
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)

    main_box = page.locator("main").bounding_box()
    panel_box = page.get_by_role("complementary", name="Workspace").bounding_box()
    transcript_box = page.get_by_role("log").bounding_box()
    files_box = page.get_by_role("tab", name="Files").bounding_box()
    assert main_box is not None
    assert panel_box is not None
    assert transcript_box is not None
    assert files_box is not None

    hits = page.evaluate(
        """(points) => Object.fromEntries(
          Object.entries(points).map(([name, point]) => {
            const target = document.elementFromPoint(point.x, point.y);
            return [name, {
              gutter: target?.closest('[data-workspace-panel-resize-gutter]') !== null,
              filesTab: target?.closest('[role="tab"]')?.getAttribute('aria-label') === 'Files',
            }];
          }),
        )""",
        {
            "chatSliver": {
                "x": main_box["x"] + main_box["width"] - 3,
                "y": transcript_box["y"] + transcript_box["height"] / 2,
            },
            "chatOutside": {
                "x": main_box["x"] + main_box["width"] - 8,
                "y": transcript_box["y"] + transcript_box["height"] / 2,
            },
            "panelSliver": {
                "x": panel_box["x"] + 4,
                "y": files_box["y"] + files_box["height"] / 2,
            },
            "panelOutside": {
                "x": panel_box["x"] + 9,
                "y": files_box["y"] + files_box["height"] / 2,
            },
        },
    )

    assert hits["chatSliver"]["gutter"] is True, hits
    assert hits["panelSliver"]["gutter"] is True, hits
    assert hits["chatOutside"]["gutter"] is False, hits
    assert hits["panelOutside"] == {"gutter": False, "filesTab": True}, hits
    assert (
        page.locator(_GUTTER).evaluate("element => getComputedStyle(element).position")
        == "relative"
    )


def test_touch_resize_persists_without_stealing_transcript_scroll(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    base_url, session_id = seeded_session
    for index in range(6):
        seed_committed_turn(
            session_id,
            prompt=f"Question {index}?",
            reply=f"Paragraph {index}. " + ("filler sentence for height. " * 12),
            response_id=f"resp_resize_{index}",
        )

    page.set_viewport_size(_VIEWPORT)
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)

    gutter = page.locator(_GUTTER)
    expect(gutter).to_be_visible()
    gutter_box = gutter.bounding_box()
    assert gutter_box is not None
    initial_width = _panel_width(page)

    touch_drag_between(
        page,
        start=(gutter_box["x"] + gutter_box["width"] / 2, gutter_box["y"] + 200),
        end=(gutter_box["x"] + 120, gutter_box["y"] + 200),
    )

    resized_width = _panel_width(page)
    assert resized_width <= initial_width - 100
    page.wait_for_function(
        """([key, id, width]) => {
          const entries = JSON.parse(localStorage.getItem(key) || "[]");
          const stored = entries.find((entry) => entry.id === id)?.state?.widthPx;
          return Math.abs(stored - width) <= 1;
        }""",
        arg=[_STORAGE_KEY, session_id, resized_width],
    )

    page.reload()
    open_right_rail(page)
    expect(page.get_by_role("log")).to_be_visible(timeout=30_000)
    page.wait_for_function(
        """(width) => {
          const panel = document.querySelector('[aria-label="Workspace"]');
          return Math.abs(panel.getBoundingClientRect().width - width) <= 1;
        }""",
        arg=resized_width,
    )

    transcript_box = page.get_by_role("log").bounding_box()
    assert transcript_box is not None
    # The 8px hit test catches a widened gutter without dragging the scrollbar
    # thumb; the 16px gesture separately proves transcript scrolling stays owned.
    hit = page.evaluate(
        """([x, y]) => {
          const target = document.elementFromPoint(x, y);
          return {
            isGutter: target?.closest('[data-workspace-panel-resize-gutter]') !== null,
            touchAction: target ? getComputedStyle(target).touchAction : null,
          };
        }""",
        arg=[transcript_box["x"] + transcript_box["width"] - 8, transcript_box["y"] + 400],
    )
    assert hit["isGutter"] is False
    assert hit["touchAction"] != "none"

    scroll_top = page.evaluate(
        """() => {
          const log = document.querySelector('[role="log"]');
          const scroller = [...log.querySelectorAll('*')].find(
            (element) => element.scrollHeight > element.clientHeight + 4,
          );
          if (!scroller) return null;
          scroller.dataset.inlineResizeScroller = "true";
          scroller.scrollTop = 0;
          return scroller.scrollTop;
        }"""
    )
    assert scroll_top == 0
    width_before_scroll = _panel_width(page)
    touch_drag_between(
        page,
        start=(transcript_box["x"] + transcript_box["width"] - 16, transcript_box["y"] + 400),
        end=(transcript_box["x"] + transcript_box["width"] - 16, transcript_box["y"] + 280),
    )

    page.wait_for_function("document.querySelector('[data-inline-resize-scroller]').scrollTop > 0")
    assert _panel_width(page) == width_before_scroll
    assert _stored_width(page, session_id) == resized_width


def test_touch_resize_keeps_drag_range_on_tablet_width(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The rail must stay touch-resizable at tablet width with the sidebar open.

    On unfolded-foldable viewports the sidebar defaults open, and reserving it
    plus the chat's full comfort minimum used to pin the clamp's floor onto its
    ceiling — the gutter rendered but every touch drag was a no-op.
    """
    base_url, session_id = seeded_session

    page.set_viewport_size(_TABLET_VIEWPORT)
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)

    gutter = page.locator(_GUTTER)
    expect(gutter).to_be_visible()
    gutter_box = gutter.bounding_box()
    assert gutter_box is not None
    initial_width = _panel_width(page)

    # Shrink toward the rail's floor…
    touch_drag_between(
        page,
        start=(gutter_box["x"] + gutter_box["width"] / 2, gutter_box["y"] + 200),
        end=(gutter_box["x"] + 150, gutter_box["y"] + 200),
    )
    shrunk_width = _panel_width(page)
    assert shrunk_width <= initial_width - 60

    # …then widen back out: the move that used to be a pinned no-op.
    gutter_box = gutter.bounding_box()
    assert gutter_box is not None
    touch_drag_between(
        page,
        start=(gutter_box["x"] + gutter_box["width"] / 2, gutter_box["y"] + 200),
        end=(gutter_box["x"] - 200, gutter_box["y"] + 200),
    )
    widened_width = _panel_width(page)
    assert widened_width >= shrunk_width + 60
