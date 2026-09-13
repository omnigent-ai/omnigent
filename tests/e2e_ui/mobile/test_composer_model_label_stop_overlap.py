"""E2E: the composer action row collapses its text to icons on a phone.

On a phone-sized viewport (iOS web), the composer's action row cannot fit the
permission chip's text and the ``<Model> <Effort>`` label — e.g.
``databricks-claude-fable-5 xHigh`` on a Databricks-backed claude-native
session — beside the other controls. The row must not wrap the right-hand
controls onto a second line, nor let the model text run beneath the Stop
(Interrupt) button while a turn is running: as soon as the labels stop
fitting on one line, they collapse to their icons, and they come back once
the row is wide enough again.

User journeys covered (the reporter's, on iOS web, plus the landing page):

1. open a session bound to a long Databricks model id at xhigh effort on a
   phone-sized viewport — the model/effort text is collapsed to the harness
   icon and every control sits on one row,
2. send a message — the agent starts working, so the composer's Send button
   becomes the destructive Stop (Interrupt) square — the icon-only model
   trigger stays beside it, never beneath it,
3. widen the window to desktop — the labels reappear,
4. the new-session (landing) composer does the same with its permission and
   model text.

Harness notes:

- The session is the standard ``seeded_session`` (a real server-backed
  ``hello_world`` session); the browser's ``GET /v1/sessions/{id}`` snapshot
  is patched into a claude-native session on a Databricks workspace model at
  ``xhigh`` effort — the exact shape the reporter's session has — the same
  route-patch approach as ``chat/test_claude_model_picker.py``. The catalog
  rows carry a ``databricks`` ``source``, exactly as on a real Databricks
  workspace session.
- The turn is held open with the mock LLM's ``block`` gate (released in the
  ``finally``), so the Stop button is genuinely showing while the geometry
  is measured — the same running-turn state the reporter saw.
- The viewport matches Playwright's "iPhone 13" profile (390x664), so a
  recorder run with ``--device "iPhone 13"`` films pixel-exact.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from playwright.async_api import async_playwright
from playwright.async_api import expect as async_expect
from playwright.sync_api import FloatRect, Locator, Page, Route, expect

from tests.e2e_ui.conftest import configure_mock_llm, fetch_with_retry, reset_mock_llm
from tests.e2e_ui.start_session.test_model_flows_prelaunch import _CLAUDE_HOST_ROWS
from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _register_common_routes,
    _run_in_fresh_loop,
)

# iPhone 13-class portrait viewport (matches Playwright's "iPhone 13" device
# profile, so a recorder run with ``--device "iPhone 13"`` films pixel-exact).
_IPHONE_VIEWPORT = {"width": 390, "height": 664}
# A wide desktop window: the chat page opens the sidebar and the workspace rail
# beside the composer, so this is what it takes for the long Databricks model
# label to fit on one row with room to spare.
_DESKTOP_VIEWPORT = {"width": 1800, "height": 900}

# The reporter's session shape: a Databricks-served Claude model (shown raw —
# the id is not in the alias catalog) at xhigh effort. The composer label
# reads ``databricks-claude-fable-5-extended-thinking xHigh``. Long enough
# (~282px at text-sm) that it can never share a phone-width action row with
# the other controls — so the row MUST collapse it to the harness icon. On the
# buggy build it either extends beneath the Stop button or wraps the whole
# right-hand group onto a second line.
_MODEL_ID = "databricks-claude-fable-5-extended-thinking"
_EFFORT = "xhigh"

# Unique sentinel so the mock LLM's blocking gate fires only for this test's
# turn (content-based routing), never for background LLM traffic.
_SENTINEL = "sentinel-model-label-overlap keep this turn running"

# Databricks workspace catalog rows, as a real claude-native launch reports
# them (aliases + a non-secret ``source``). The bound model id is
# intentionally NOT in the catalog, so the label shows it verbatim — the
# pre-catalog/raw-id read-out the reporter's screenshot shows.
_DATABRICKS_SOURCE = {
    "kind": "databricks",
    "label": "Workspace",
    "name": "production-west",
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

_CODEX_MODEL_ID = "gpt-5.6-sol"
_CODEX_MODEL_OPTIONS = [
    {
        "id": _CODEX_MODEL_ID,
        "model": _CODEX_MODEL_ID,
        "displayName": "GPT-5.6-Sol",
        "defaultReasoningEffort": "high",
        "supportedReasoningEfforts": [
            {"reasoningEffort": "low", "description": "Low"},
            {"reasoningEffort": "medium", "description": "Medium"},
            {"reasoningEffort": "high", "description": "High"},
            {"reasoningEffort": "xhigh", "description": "Extra high"},
        ],
        "isDefault": True,
        "source": _DATABRICKS_SOURCE,
    }
]


def _patch_session_as_databricks_claude_native(page: Page, session_id: str) -> None:
    """Shape the browser's session snapshot like the reporter's session.

    Patches only ``GET /v1/sessions/{session_id}`` as seen by the browser:
    claude-native wrapper labels, a Databricks model id the catalog doesn't
    list (rendered raw by the composer label), catalog rows carrying a
    ``databricks`` source, and ``xhigh`` reasoning effort. Everything else —
    the session, the runner, the turn — is the real spawned server.

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
        payload["llm_model"] = _MODEL_ID
        payload["model_options"] = _MODEL_OPTIONS
        payload["reasoning_effort"] = _EFFORT
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    page.route("**/v1/sessions/**", _handle)


def _patch_session_as_databricks_codex_native(page: Page, session_id: str) -> None:
    """Shape the browser snapshot like the compact Codex iOS composer.

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
            "omnigent.wrapper": "codex-native-ui",
        }
        payload["harness"] = "codex-native"
        payload["llm_model"] = _CODEX_MODEL_ID
        payload["model_options"] = _CODEX_MODEL_OPTIONS
        payload["reasoning_effort"] = _EFFORT
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    page.route("**/v1/sessions/**", _handle)


def _box(locator: Locator) -> FloatRect:
    """Return the element's bounding box, failing loudly when it has none.

    :param locator: A locator resolved to exactly one visible element.
    :returns: The element's bounding box.
    """
    box = locator.bounding_box()
    assert box is not None, f"element {locator} has no bounding box"
    return box


def _intersection(a: FloatRect, b: FloatRect) -> tuple[float, float]:
    """Return the (horizontal, vertical) overlap in px between two boxes.

    :param a: First bounding box.
    :param b: Second bounding box.
    :returns: ``(x_overlap, y_overlap)``; both positive iff the boxes intersect.
    """
    x_overlap = min(a["x"] + a["width"], b["x"] + b["width"]) - max(a["x"], b["x"])
    y_overlap = min(a["y"] + a["height"], b["y"] + b["height"]) - max(a["y"], b["y"])
    return (x_overlap, y_overlap)


def _assert_same_row(a: FloatRect, b: FloatRect) -> None:
    """Fail unless two boxes are vertically centred on the same line.

    :param a: First bounding box.
    :param b: Second bounding box.
    :returns: None.
    """
    centre_a = a["y"] + a["height"] / 2
    centre_b = b["y"] + b["height"] / 2
    assert abs(centre_a - centre_b) <= 1.0, (
        f"controls wrapped onto separate rows: centres at y={centre_a:.0f} and y={centre_b:.0f}"
    )


def _screenshot(page: Page, name: str) -> None:
    """Save a demo screenshot when E2E_SCREENSHOT_DIR is set (local runs)."""
    shot_dir = os.environ.get("E2E_SCREENSHOT_DIR")
    if shot_dir:
        page.screenshot(path=str(Path(shot_dir) / f"{name}.png"))


def _release_gates(mock_url: str) -> None:
    """Release any turn still parked on the mock LLM's blocking gate.

    :param mock_url: Mock LLM server base URL.
    :returns: None.
    """
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        pending = httpx.get(f"{mock_url}/gate/pending", timeout=5.0, trust_env=False)
        pending.raise_for_status()
        if not pending.json().get("pending"):
            return
        httpx.post(f"{mock_url}/gate/release", timeout=5.0, trust_env=False).raise_for_status()
        time.sleep(0.2)


def test_composer_model_label_stays_clear_of_stop_button_on_mobile(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """On a phone viewport the model/effort text collapses to the harness icon,
    which stays beside the Stop (Interrupt) button on a single row.

    Asserts the label is rendered but hidden while the row is collapsed, then
    sends a message whose turn the mock LLM holds open, waits for the
    composer's destructive Stop square, and asserts the icon-only model
    trigger neither intersects the Stop button's bounding box nor pushes it
    off-screen, and that the leftmost and rightmost controls share one row. A
    ≤1px touch is tolerated (border rounding / antialiasing); anything more is
    the reported overlap. Finally widens the window to a laptop viewport and
    asserts the label text comes back.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    :param mock_llm_server_url: Session-scoped mock LLM server URL.
    :returns: None.
    """
    base_url, session_id = seeded_session

    page.set_viewport_size(_IPHONE_VIEWPORT)
    _patch_session_as_databricks_claude_native(page, session_id)

    # Hold this test's turn open so the Stop button is genuinely showing
    # while the geometry is measured. Content-matched to the sentinel on a
    # dedicated queue key; several blocked responses are queued because
    # background LLM traffic that embeds the user message (e.g. title
    # generation) can match the sentinel too and would otherwise consume
    # the only gate, letting the agent turn fall through to the instant
    # ``gpt-4o-mini`` fallback and finish before the geometry is measured.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "done", "block": True}] * 4,
        key="model-label-overlap-gate",
        match=_SENTINEL,
    )

    try:
        page.goto(f"{base_url}/c/{session_id}")

        composer = page.get_by_label("Message the agent")
        expect(composer).to_be_visible(timeout=15_000)

        # The reporter's label is rendered, but a phone-width row cannot fit
        # it beside the other controls, so the row collapses every label to
        # its icon: the text is in the DOM yet hidden, never wrapped.
        trigger = page.get_by_test_id("composer-config-gear")
        label = page.get_by_test_id("composer-agent-config-value")
        row = page.get_by_test_id("composer-action-row")
        expect(trigger).to_be_visible(timeout=15_000)
        expect(label).to_contain_text(_MODEL_ID)
        expect(row).to_have_attribute("data-labels", "collapsed")
        expect(label).to_be_hidden()
        _screenshot(page, "chat-composer-phone-collapsed")

        composer.fill(_SENTINEL)
        page.get_by_role("button", name="Send", exact=True).click()

        # The turn is running (gated open on the mock LLM) and the draft is
        # cleared, so the Send button is now the destructive Stop square.
        stop = page.get_by_role("button", name="Interrupt")
        expect(stop).to_be_visible(timeout=60_000)

        # Hold the running state briefly so the failure is observable in a
        # recording before the geometry assertions run.
        page.wait_for_timeout(1_500)
        _screenshot(page, "chat-composer-phone-collapsed-running")

        expect(label).to_be_hidden()
        trigger_box = _box(trigger)
        stop_box = _box(stop)

        # The Stop button itself must be usable: fully on-screen. A row that
        # refuses to shrink pushes it past the right edge instead.
        viewport_right = _IPHONE_VIEWPORT["width"]
        assert stop_box["x"] + stop_box["width"] <= viewport_right + 1.0, (
            f"the Stop button is pushed off-screen: button at "
            f"(x={stop_box['x']:.0f}, w={stop_box['width']:.0f}) vs viewport "
            f"width {viewport_right}"
        )

        # The reported failure: the model trigger extends beneath the Stop
        # button instead of sitting beside it.
        x_overlap, y_overlap = _intersection(trigger_box, stop_box)
        assert not (x_overlap > 1.0 and y_overlap > 1.0), (
            f"the composer model trigger overlaps the Stop button by "
            f"{x_overlap:.0f}x{y_overlap:.0f}px: trigger at "
            f"(x={trigger_box['x']:.0f}, y={trigger_box['y']:.0f}, "
            f"w={trigger_box['width']:.0f}, h={trigger_box['height']:.0f}), "
            f"Stop button at (x={stop_box['x']:.0f}, y={stop_box['y']:.0f}, "
            f"w={stop_box['width']:.0f}, h={stop_box['height']:.0f})"
        )
        # Collapsing to icons is what keeps the row on one line: the leftmost
        # control (Add) and the rightmost (Stop) share it.
        _assert_same_row(_box(page.get_by_test_id("composer-attach")), stop_box)

        # Given room again, the text comes back: the collapse is measured
        # against the row's width, not pinned to a breakpoint.
        page.set_viewport_size(_DESKTOP_VIEWPORT)
        expect(label).to_be_visible()
        expect(label).to_contain_text(_MODEL_ID)
        expect(row).not_to_have_attribute("data-labels", "collapsed")
        # Measured against the model trigger, not the Stop button: the gated turn
        # may have finished by now, and the row check doesn't depend on it.
        _assert_same_row(_box(page.get_by_test_id("composer-attach")), _box(trigger))
        _screenshot(page, "chat-composer-desktop-expanded")
    finally:
        # Drop the snapshot route before teardown so an in-flight fetch
        # doesn't error against the closing context, then let the gated
        # turn finish so the shared server tears down clean.
        page.unroute_all(behavior="ignoreErrors")
        _release_gates(mock_llm_server_url)
        reset_mock_llm(mock_llm_server_url)


def test_composer_plan_and_goal_actions_fit_mobile_and_desktop(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Keep Goal and Plan reachable in the shared Add menu at both widths."""
    base_url, session_id = seeded_session
    page.set_viewport_size(_IPHONE_VIEWPORT)
    _patch_session_as_databricks_codex_native(page, session_id)
    try:
        page.goto(f"{base_url}/c/{session_id}")
        for viewport in [_IPHONE_VIEWPORT, {"width": 1200, "height": 852}]:
            page.set_viewport_size(viewport)
            trigger = page.get_by_test_id("composer-attach")
            expect(trigger).to_be_visible()
            expect(page.get_by_test_id("composer-config-gear")).to_be_visible()
            trigger.click()
            for action_id in ["composer-plan-action", "composer-goal-action"]:
                action = page.get_by_test_id(action_id)
                expect(action).to_be_visible()
                expect(action).to_be_enabled()
                bounds = _box(action)
                assert bounds["x"] >= 0
                assert bounds["x"] + bounds["width"] <= viewport["width"]
            page.keyboard.press("Escape")
            expect(trigger).to_be_focused()
    finally:
        page.unroute_all(behavior="ignoreErrors")


def test_new_session_composer_collapses_labels_to_icons_on_mobile(
    seeded_session: tuple[str, str],
) -> None:
    """The landing composer hides its permission and model text on a phone
    viewport, keeps every control on one row, and shows the text again on a
    laptop-width window.

    :param seeded_session: ``(base_url, session_id)``; the landing page's
        create POST is stubbed to return this real session id.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_landing_collapse(base_url, session_id))


async def _drive_landing_collapse(base_url: str, session_id: str) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(viewport=_IPHONE_VIEWPORT)
        page = await context.new_page()
        try:
            await _register_common_routes(page, created_session_id=session_id, create_bodies=[])
            await page.route(
                re.compile(r"/v1/sessions\?"),
                lambda route: route.fulfill(json={"data": [], "has_more": False}),
            )
            # A model catalog for the auto-selected Claude Code agent, so the
            # trigger shows a real ``Opus 4.8 (1M context)`` label to collapse.
            await page.route(
                f"**/v1/hosts/{_HOST_ID}/harnesses/claude-native/model-options",
                lambda route: route.fulfill(json={"models": _CLAUDE_HOST_ROWS}),
            )
            await page.goto(f"{base_url}/")
            await async_expect(page.get_by_test_id("new-chat-landing-input")).to_be_visible(
                timeout=30_000
            )
            row = page.get_by_test_id("new-chat-landing-actions")
            permission_text = page.get_by_test_id("new-chat-landing-permission-chip").locator(
                "span"
            )
            model_text = page.get_by_test_id("new-chat-landing-agent-config-value")
            await async_expect(
                page.get_by_test_id("new-chat-landing-agent-select")
            ).to_be_visible()
            await async_expect(permission_text).not_to_be_empty()
            await async_expect(model_text).not_to_be_empty()

            # Phone width: both labels collapse to icons and the row stays single-line.
            await async_expect(row).to_have_attribute("data-labels", "collapsed")
            await async_expect(permission_text).to_be_hidden()
            await async_expect(model_text).to_be_hidden()
            add_box = await page.get_by_test_id("new-chat-landing-attach").bounding_box()
            submit_box = await page.get_by_test_id("new-chat-landing-submit").bounding_box()
            assert add_box is not None and submit_box is not None
            assert submit_box["x"] + submit_box["width"] <= _IPHONE_VIEWPORT["width"] + 1.0, (
                submit_box
            )
            _assert_same_row(add_box, submit_box)
            shot_dir = os.environ.get("E2E_SCREENSHOT_DIR")
            if shot_dir:
                await page.screenshot(path=str(Path(shot_dir) / "landing-composer-phone.png"))

            # Laptop width: the text is back and nothing is collapsed.
            await page.set_viewport_size(_DESKTOP_VIEWPORT)
            await async_expect(permission_text).to_be_visible()
            await async_expect(model_text).to_be_visible()
            await async_expect(row).not_to_have_attribute("data-labels", "collapsed")
            if shot_dir:
                await page.screenshot(path=str(Path(shot_dir) / "landing-composer-desktop.png"))
        finally:
            await context.close()
            await browser.close()
