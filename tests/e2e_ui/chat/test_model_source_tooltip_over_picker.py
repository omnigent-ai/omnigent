"""E2E: the pill's Model-configuration tooltip must not obscure the selector.

In an existing session whose model catalog rows carry a connection
``source`` (e.g. a Databricks workspace), the composer's model/effort pill
renders inside the ``composer-model-source`` tooltip wrapper, whose hover
tooltip reads ``Model configuration`` / ``Connection: Databricks · …``.
Clicking the pill opens the session selector popover
(``composer-agent-menu``). On the buggy build the tooltip is not suppressed
while the popover is open: clicking the pill gives its trigger focus, and
the Radix tooltip opens on focus in ``instant-open`` mode (which skips the
hover delay), so it appears AFTER the click and stays. Because the tooltip
and the popover are both ``z-50`` popper portals in the same container, the
later-mounted tooltip paints ON TOP of the popover, hiding its lower-right
content (the reporter's ``Advanced settings…`` row).

User journey covered:

1. open an existing session in the web UI,
2. move the pointer onto the composer's model/effort pill (its
   ``Model configuration`` tooltip surface),
3. click the pill to open the selector popover,
4. with the pointer still on the pill, the tooltip must not remain painted
   over the popover,
5. click away to close the popover: Radix hands focus back to the pill's
   trigger, and that programmatic focus must not instantly reopen the
   tooltips — they may only come back on fresh hover/focus intent.

Timing note: a real user clicks the pill in one natural motion, well inside
the tooltip's 600ms hover-open delay, so the tooltip that ends up over the
popover is the focus-driven ``instant-open`` one that appears *after* the
click — NOT a pre-existing delayed-open one. (If you instead wait out the
full 600ms so the delayed-open tooltip is already showing, the click
dismisses that one correctly; that path is not the bug. This test therefore
clicks promptly, matching the reporter's motion.)

The guard passes for either fix shape the report allows — the tooltip is
dismissed/suppressed while the popover is open, or it is layered below the
popover — and fails only when the tooltip is painted above the open menu.

Harness notes: the session is the standard ``seeded_session`` (a real
server-backed ``hello_world`` session); only the browser's
``GET /v1/sessions/{id}`` snapshot is patched into a claude-native session
whose catalog rows carry a ``databricks`` ``source`` — the exact shape the
reporter's session has (same route-patch approach as
``chat/test_claude_model_picker.py``). Paint order is probed structurally
(popper-wrapper z-index, then DOM order for the tie) rather than with
``elementFromPoint``: the open dropdown is modal, so scroll-locking sets
``pointer-events: none`` on ``body`` and hit-testing would skip the tooltip
even while it visibly covers the menu.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry

# The reporter's session shape: a Databricks-served Claude catalog whose rows
# carry a non-secret connection ``source`` — required for the pill to render
# inside the ``composer-model-source`` tooltip wrapper at all. The tooltip
# then reads ``Model configuration`` / ``Connection: Databricks · oss``.
_DATABRICKS_SOURCE = {
    "kind": "databricks",
    "label": "Workspace",
    "name": "oss",
    "host": "ws.example.com",
}
_MODEL_OPTIONS = [
    {
        "id": "opus",
        "model": "system.ai.claude-opus-4-10",
        "displayName": "Opus 4.10",
        "isDefault": False,
        "source": _DATABRICKS_SOURCE,
    },
    {
        "id": "sonnet",
        "model": "system.ai.claude-sonnet-5",
        "displayName": "Sonnet 5",
        "isDefault": True,
        "source": _DATABRICKS_SOURCE,
    },
]


def _patch_session_as_databricks_claude_native(page: Page, session_id: str) -> None:
    """Shape the browser's session snapshot like the reporter's session.

    Patches only ``GET /v1/sessions/{session_id}`` as seen by the browser:
    claude-native wrapper labels and catalog rows carrying a ``databricks``
    ``source``. Everything else — the session, the server, the page — is the
    real spawned app.

    :param page: Playwright page, before navigation.
    :param session_id: Session id to patch, e.g. ``"conv_abc123"``.
    :returns: None.
    """

    def _handle(route: Route) -> None:
        request = route.request
        if urlparse(request.url).path != f"/v1/sessions/{session_id}" or request.method != "GET":
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
        payload["labels"] = {
            **payload.get("labels", {}),
            "omnigent.wrapper": "claude-code-native-ui",
        }
        payload["harness"] = "claude"
        payload["llm_model"] = "system.ai.claude-sonnet-5"
        payload["model_options"] = _MODEL_OPTIONS
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    page.route("**/v1/sessions/**", _handle)


# Structural paint-order probe for the two popper portals. Equal z-index
# fixed-position siblings paint in DOM order, and Radix copies the content's
# computed z-index onto its popper wrapper, so wrapper z-index (then DOM
# order on a tie) is exactly the browser's paint order for these two.
_OCCLUSION_PROBE = """
() => {
  const tooltip = document.querySelector('[data-testid="composer-model-source-tooltip"]');
  const menu = document.querySelector('[data-testid="composer-agent-menu"]');
  if (!menu) return "no-menu";
  if (!tooltip) return "no-tooltip";
  const tooltipStyle = getComputedStyle(tooltip);
  if (
    tooltipStyle.visibility === "hidden" ||
    tooltipStyle.display === "none" ||
    Number(tooltipStyle.opacity) === 0
  )
    return "no-tooltip";
  const t = tooltip.getBoundingClientRect();
  const m = menu.getBoundingClientRect();
  if (t.width === 0 || t.height === 0) return "no-tooltip";
  const left = Math.max(t.left, m.left);
  const right = Math.min(t.right, m.right);
  const top = Math.max(t.top, m.top);
  const bottom = Math.min(t.bottom, m.bottom);
  if (left >= right || top >= bottom) return "no-overlap";
  const wrap = (el) => el.closest("[data-radix-popper-content-wrapper]") ?? el;
  const tooltipWrapper = wrap(tooltip);
  const menuWrapper = wrap(menu);
  const zOf = (el) => {
    const z = Number.parseFloat(getComputedStyle(el).zIndex);
    return Number.isNaN(z) ? 0 : z;
  };
  const tz = zOf(tooltipWrapper);
  const mz = zOf(menuWrapper);
  if (tz !== mz) return tz > mz ? "tooltip-on-top" : "menu-on-top";
  const position = menuWrapper.compareDocumentPosition(tooltipWrapper);
  return position & Node.DOCUMENT_POSITION_FOLLOWING ? "tooltip-on-top" : "menu-on-top";
}
"""


def _screenshot(page: Page, name: str) -> None:
    """Save a demo screenshot when E2E_SCREENSHOT_DIR is set (local runs)."""
    shot_dir = os.environ.get("E2E_SCREENSHOT_DIR")
    if shot_dir:
        page.screenshot(path=str(Path(shot_dir) / f"{name}.png"))


def test_pill_tooltip_does_not_obscure_open_selector(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Opening the selector must dismiss/suppress (or underlay) the tooltip.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real
        server-backed session; the browser snapshot is patched to a
        Databricks-sourced claude-native session.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _patch_session_as_databricks_claude_native(page, session_id)
    try:
        page.goto(f"{base_url}/c/{session_id}")

        pill = page.get_by_test_id("composer-config-gear")
        expect(pill).to_be_visible(timeout=15_000)

        # 2. move the pointer onto the pill's tooltip surface, as a user
        # reaching for it does. Stay briefly (< the 600ms hover-open delay)
        # so the click below lands in one natural motion — the delayed-open
        # tooltip has NOT appeared yet; the click's own focus is what opens
        # the tooltip.
        box = pill.bounding_box()
        assert box is not None, "pill has no bounding box"
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.mouse.move(cx, cy)
        page.wait_for_timeout(200)

        # 3. click the pill: the selector popover opens, and the pill's
        # trigger takes focus.
        pill.click()
        menu = page.get_by_test_id("composer-agent-menu")
        expect(menu).to_be_visible()

        # 4. with the pointer still on the pill, wait out the tooltip's
        # hover/animation window so a focus-driven ``instant-open`` tooltip
        # that appears over the popover has fully done so (and a suppressed
        # one has stayed away) before paint order is probed.
        page.mouse.move(cx + 1, cy)
        page.wait_for_timeout(1_200)

        expect(menu).to_be_visible()
        state = page.evaluate(_OCCLUSION_PROBE)
        _screenshot(page, "pill-tooltip-vs-open-selector")
        assert state != "tooltip-on-top", (
            "the pill's Model-configuration tooltip is painted over the open "
            "selector popover, obscuring the menu; "
            "expected it dismissed, suppressed, or layered below while the "
            "popover is open"
        )

        # 5. click away to close the popover. The open menu is modal, so the
        # outside pointerdown only dismisses it. Radix then returns focus to
        # the pill's trigger; neither the model-source tooltip nor the
        # trigger's own summary tooltip may instantly reopen on that
        # programmatic focus (wait out the hover/animation window before
        # probing).
        page.mouse.click(cx, max(cy - 300, 10))
        expect(menu).not_to_be_visible()
        page.wait_for_timeout(1_200)
        _screenshot(page, "pill-tooltips-after-clicking-away")
        for tooltip_test_id in (
            "composer-model-source-tooltip",
            "composer-config-gear-tooltip",
        ):
            expect(page.get_by_test_id(tooltip_test_id)).not_to_be_visible()
    finally:
        page.unroute_all(behavior="ignoreErrors")
