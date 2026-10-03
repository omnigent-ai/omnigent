"""E2E: the model advanced-settings menu respects the iOS top safe-area inset.

On a notched iPhone the OS status bar / Dynamic Island overlays the top of the
WKWebView. Opening a harness row's Edit (advanced settings) page in the new-chat
model picker fills the menu with the model catalog, efforts, and options — tall
enough that the popover grows to its full available height. The popover clamps
against the raw viewport edge (a fixed 12px collision padding), not the
safe-area line, so its top — the Back row and the first model options — extends
into the status-bar band where the OS chrome obscures it and taps don't land.

The journey drives the SPA the way the iOS shell does — iPhone viewport, an
injected ``window.omnigentNative`` bridge, and a notch-sized OS safe-area inset.
WKWebView delivers the inset via ``env(safe-area-inset-top)``, which Chromium
cannot emulate, so the test injects the same value into the shared fold var the
layout consumes (``--omnigent-safe-top``, index.css) — the pattern from
``test_ios_server_selector_placement.py``.

The viewport matches Playwright's "iPhone 13" profile (390x664), so a recorder
run with ``--device "iPhone 13"`` films pixel-exact.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from playwright.async_api import Route, async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _register_common_routes,
    _run_in_fresh_loop,
)

# Phone-sized viewport: the iOS shell is an iPhone surface (Playwright's
# "iPhone 13" profile), and the short height is where the tall config page
# collides with the top of the screen.
_MOBILE_VIEWPORT = {"width": 390, "height": 664}

# OS safe-area top inset of a notched iPhone (Dynamic Island class), CSS px.
_SAFE_TOP_PX = 59

# Native bar footprints the iOS shell pushes over the bridge (mirror of
# InsetMetrics in web/ios/Omnigent/WebShellView.swift).
_TOP_BAR_PX = 36
_BOTTOM_BAR_PX = 48

# Minimal stand-in for the iOS WKWebView bridge: `kind` drives isIOSShell(),
# onNativeInsets pushes the shell's cached bar footprints, and the rest keep
# unrelated native calls from throwing under the stub.
_IOS_SHELL_INIT_SCRIPT = f"""
window.omnigentNative = {{
  kind: "ios",
  setBadgeCount: function () {{}},
  notify: function () {{ return Promise.resolve(false); }},
  onNotificationActivated: function () {{ return function () {{}}; }},
  onNativeInsets: function (cb) {{
    cb({{ topBar: {_TOP_BAR_PX}, bottomBar: {_BOTTOM_BAR_PX} }});
    return function () {{}};
  }},
  setServerSwitcherHidden: function () {{}},
  getServerPicker: function () {{
    return Promise.resolve({{
      currentOrigin: location.origin,
      managedServers: [],
      recentServers: [location.origin + "/"],
    }});
  }},
  switchServer: function () {{ return Promise.resolve(); }},
  openServerSetup: function () {{}},
  setViewMode: function () {{}},
  onViewModeChanged: function () {{ return function () {{}}; }},
}};
"""

# Claude host catalog as a real claude-native launch reports it — enough rows
# (with the Default entry and the five effort choices) that the config page
# outgrows a phone screen, as the real catalog does on device.
_CLAUDE_MODEL_ROWS = [
    {"id": "sonnet", "model": "claude-sonnet-5", "displayName": "Sonnet 5", "isDefault": True},
    {
        "id": "sonnet[1m]",
        "model": "claude-sonnet-5[1m]",
        "displayName": "Sonnet 5 (1M context)",
        "isDefault": False,
    },
    {
        "id": "opus",
        "model": "claude-opus-4-8",
        "displayName": "Opus 4.8",
        "isDefault": False,
    },
    {
        "id": "opus[1m]",
        "model": "claude-opus-4-8[1m]",
        "displayName": "Opus 4.8 (1M context)",
        "isDefault": False,
    },
    {
        "id": "haiku",
        "model": "claude-haiku-4-5-20251001",
        "displayName": "Haiku 4.5",
        "isDefault": False,
    },
]

_CODEX_EFFORTS = [
    {"reasoningEffort": "low", "description": "Low"},
    {"reasoningEffort": "medium", "description": "Medium"},
    {"reasoningEffort": "high", "description": "High"},
    {"reasoningEffort": "xhigh", "description": "Extra high"},
]

# Codex host catalog with per-model reasoning efforts, as codex-native reports.
_CODEX_MODEL_ROWS = [
    {
        "id": "gpt-5.3-codex",
        "model": "gpt-5.3-codex",
        "displayName": "GPT-5.3-Codex",
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": _CODEX_EFFORTS,
    },
    {
        "id": "gpt-5.3-codex-mini",
        "model": "gpt-5.3-codex-mini",
        "displayName": "GPT-5.3-Codex-Mini",
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": _CODEX_EFFORTS,
    },
    {
        "id": "gpt-5.2",
        "model": "gpt-5.2",
        "displayName": "GPT-5.2",
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": _CODEX_EFFORTS,
    },
    {
        "id": "gpt-5.1-codex-max",
        "model": "gpt-5.1-codex-max",
        "displayName": "GPT-5.1-Codex-Max",
        "defaultReasoningEffort": "high",
        "supportedReasoningEfforts": _CODEX_EFFORTS,
    },
    {
        "id": "gpt-5.1-codex-mini",
        "model": "gpt-5.1-codex-mini",
        "displayName": "GPT-5.1-Codex-Mini",
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": _CODEX_EFFORTS,
    },
    {
        "id": "gpt-5.1",
        "model": "gpt-5.1",
        "displayName": "GPT-5.1",
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": _CODEX_EFFORTS,
    },
    {
        "id": "codex-mini-latest",
        "model": "codex-mini-latest",
        "displayName": "Codex-Mini-Latest",
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": _CODEX_EFFORTS,
    },
    {
        "id": "gpt-5.1-mini",
        "model": "gpt-5.1-mini",
        "displayName": "GPT-5.1-Mini",
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": _CODEX_EFFORTS,
    },
]

# Both native coding agents, so either harness row can be drilled into.
_AGENTS_BODY = json.dumps(
    {
        "data": [
            {
                "id": "ag_claude_e2e",
                "name": "claude-native-ui",
                "display_name": "Claude Code",
                "description": "Anthropic's coding agent",
                "harness": None,
                "skills": [],
            },
            {
                "id": "ag_codex_e2e",
                "name": "codex-native-ui",
                "display_name": "Codex",
                "description": "OpenAI's coding agent",
                "harness": "codex-native",
                "skills": [],
            },
        ]
    }
)

_HARNESS_CASES = {
    "claude": ("ag_claude_e2e", "claude-native", _CLAUDE_MODEL_ROWS),
    "codex": ("ag_codex_e2e", "codex-native", _CODEX_MODEL_ROWS),
}


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_advanced_settings_menu_clears_the_top_safe_area(
    harness: str, seeded_session: tuple[str, str]
) -> None:
    """The drilled-in advanced-settings page must stay below the notch inset.

    Opens the new-chat model picker at an iPhone viewport under the iOS shell
    stand-in, drills into the harness row's Edit (advanced settings) page, and
    measures the menu popover. The page's content must be tall enough that a
    safe-area-respecting popover would have to cap and scroll it (the reported
    tall-catalog scenario), and its top edge — the Back row — must clear the
    OS safe-area band; on the buggy build it grows past the safe-area line
    toward the raw screen edge, into the status-bar band, where Back and the
    first model options are obscured and not reliably tappable.

    :param harness: Which native harness row to drill into.
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_advanced_settings_inset(base_url, session_id, harness))


async def _drive_advanced_settings_inset(base_url: str, session_id: str, harness: str) -> None:
    agent_id, harness_id, model_rows = _HARNESS_CASES[harness]
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport=_MOBILE_VIEWPORT)
        try:
            await page.add_init_script(_IOS_SHELL_INIT_SCRIPT)
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=create_bodies,
                agents_body=_AGENTS_BODY,
            )

            # Hide custom agents left by other tests so only the two stubbed
            # harness rows populate the picker.
            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"data": []}),
                )

            await page.route("**/v1/sessions?*visibility=mine*", handle_agent_scan)

            async def handle_models(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"models": model_rows}),
                )

            await page.route(
                f"**/v1/hosts/{_HOST_ID}/harnesses/{harness_id}/model-options",
                handle_models,
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            await expect(page.locator(".app-shell")).to_have_attribute("data-ios-native", "true")

            # Inject the notch's OS safe-area inset. On device WKWebView
            # supplies it via env(safe-area-inset-top); here it goes straight
            # into the shared fold var every consumer reads (index.css).
            await page.evaluate(
                "() => document.documentElement.style"
                f".setProperty('--omnigent-safe-top', '{_SAFE_TOP_PX}px')"
            )

            picker = page.get_by_test_id("new-chat-landing-agent-select")
            await picker.click()
            await expect(picker).to_have_attribute("aria-expanded", "true")
            await expect(page.get_by_role("menu").first).to_be_visible()

            # Drill into the harness row's Edit (advanced settings) page.
            await (
                page.get_by_test_id(f"new-chat-landing-agent-config-{agent_id}")
                .get_by_text("Edit", exact=True)
                .click()
            )
            back = page.get_by_test_id("new-chat-landing-page-back")
            await expect(back).to_be_visible()
            menu = page.locator('[role="menu"]', has=back)
            await expect(menu).to_be_visible()

            # Let the popover finish repositioning around the swapped-in page.
            await page.wait_for_timeout(500)

            menu_box = await menu.bounding_box()
            back_box = await back.bounding_box()
            trigger_box = await picker.bounding_box()
            assert menu_box is not None and back_box is not None and trigger_box is not None

            # Gate: the page's content must outgrow the room between the
            # trigger and the safe-area line, so a compliant popover would
            # have to cap and scroll it. A shorter page could clear the inset
            # trivially without exercising the clamp.
            content_height = await menu.evaluate("el => el.scrollHeight")
            room_below_inset = trigger_box["y"] - _SAFE_TOP_PX
            assert content_height > room_below_inset, (
                f"expected the advanced-settings page ({content_height}px of "
                f"content) to outgrow the {room_below_inset:.0f}px between the "
                f"picker trigger and the safe-area line (the reported "
                f"tall-catalog scenario); it fits, so the safe-area clamp was "
                f"never exercised"
            )
            assert menu_box["y"] >= _SAFE_TOP_PX, (
                f"the advanced-settings menu must respect the iOS top safe-area "
                f"inset ({_SAFE_TOP_PX}px): its top edge is at {menu_box['y']:.0f}px, "
                f"inside the status-bar band, with the Back row at "
                f"{back_box['y']:.0f}px — obscured by the OS chrome and not "
                f"reliably tappable on device"
            )
        finally:
            await page.close()
            await browser.close()
