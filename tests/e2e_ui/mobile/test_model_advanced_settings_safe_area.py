"""E2E: the model advanced-settings sheet stays below the iOS top safe-area inset.

On a notched iPhone the iOS shell loads the SPA edge-to-edge, so the top of the
web viewport lies under the status bar. On the new-chat screen, tapping Edit on
a harness row swaps the model picker to its advanced-settings page (Back +
Models/Effort/...). That page is taller than the room around the composer, so
the menu is capped at its available height; if that height is measured against
the raw viewport instead of the safe area, the Back row and the upper model
options land in the status-bar band, behind the iOS top controls.

The iOS shell is emulated as in ``test_native_shell_plan_header_overlap.py``: a
``window.omnigentNative`` stub makes ``isIOSShell()`` true and CDP
``Emulation.setSafeAreaInsetsOverride`` gives ``env(safe-area-inset-top)`` a
real Dynamic-Island inset. The landing's host, agents and model catalogs are
the ``start_session`` suite's route stubs; the server and SPA are real.
"""

from __future__ import annotations

import os
import re

import pytest
from playwright.sync_api import FloatRect, Locator, Page, expect

from tests.e2e_ui.start_session.test_start_session import _HOST_ID

# Playwright's "iPhone 15 Pro" profile, so ``--device "iPhone 15 Pro"`` films
# pixel-exact; Dynamic-Island phones report a 59pt top inset.
_IPHONE_VIEWPORT = {"width": 393, "height": 659}
_IPHONE_SAFE_AREA = {"top": 59, "left": 0, "bottom": 34, "right": 0}

_IOS_SHELL_INIT_SCRIPT = """
window.omnigentNative = {
  kind: "ios",
  setBadgeCount: function () {},
  notify: function () { return Promise.resolve(false); },
  onNotificationActivated: function () { return function () {}; },
  onNativeInsets: function () { return function () {}; },
  onSidebarDrag: function () { return function () {}; },
  onViewModeChanged: function () { return function () {}; },
  setViewMode: function () {},
  setServerSwitcherHidden: function () {},
  setSidebarOpen: function () {},
};
"""

_CLAUDE_AGENT_ID = "58a1bc5bf0bba6d31ceeb7661f8d751c"
_CODEX_AGENT_ID = "16a06503889b0c3034496821afd41b9e"
# The repro server's real agent list, replayed verbatim: with this many entries the
# picker's main list no longer fits below the mid-screen trigger and opens upward,
# the placement the settings page then inherits (a two-agent list opens downward).
_AGENTS = [
    {
        "id": "db097e89797b66fb7e30699813ae09d2",
        "name": "antigravity-native-ui",
        "display_name": None,
        "description": None,
        "harness": "antigravity-native",
        "skills": [],
        "builtin": True,
    },
    {
        "id": "58a1bc5bf0bba6d31ceeb7661f8d751c",
        "name": "claude-native-ui",
        "display_name": None,
        "description": None,
        "harness": "claude-native",
        "skills": [],
        "builtin": True,
    },
    {
        "id": "16a06503889b0c3034496821afd41b9e",
        "name": "codex-native-ui",
        "display_name": None,
        "description": None,
        "harness": "codex-native",
        "skills": [],
        "builtin": True,
    },
    {
        "id": "a5fac3a24c1961af2dbb1cafe0a81425",
        "name": "cursor-native-ui",
        "display_name": None,
        "description": None,
        "harness": "cursor-native",
        "skills": [],
        "builtin": True,
    },
    {
        "id": "eac9e787e68ae6774d77e618031c287a",
        "name": "debby",
        "display_name": None,
        "description": "A two-headed brainstorming partner. Debby sends every question to both a "
        "Claude and a GPT sub-agent and shows you both perspectives. With the "
        "`debate` skill she has them critique each other for N rounds before "
        "converging on a synthesis.",
        "harness": "claude-sdk",
        "skills": [
            {
                "name": "debate",
                "description": "Have the Claude and GPT partners critique each other's answers "
                "across a configurable number of rounds (default 1) before "
                "converging on a synthesis. Use when the user wants the two "
                "perspectives stress-tested against each other, not just shown "
                "side by side.",
            }
        ],
        "builtin": True,
    },
    {
        "id": "010b5eea4b105dc0af6fb62f46065894",
        "name": "devin-native-ui",
        "display_name": None,
        "description": None,
        "harness": "devin-native",
        "skills": [],
        "builtin": True,
    },
    {
        "id": "93dd98c4a79bd26a1dd5dc592ee38afb",
        "name": "goose-native-ui",
        "display_name": None,
        "description": None,
        "harness": "goose-native",
        "skills": [],
        "builtin": True,
    },
    {
        "id": "ae9886388305e5dea605277965797cf1",
        "name": "grok",
        "display_name": None,
        "description": None,
        "harness": "grok",
        "skills": [],
        "builtin": True,
    },
    {
        "id": "32113910cf31fcc63dc96bbde428b97c",
        "name": "hermes-native-ui",
        "display_name": None,
        "description": None,
        "harness": "hermes-native",
        "skills": [],
        "builtin": True,
    },
    {
        "id": "faa173dc95dad9c41a2c36b8fcbb9ce2",
        "name": "jcode",
        "display_name": None,
        "description": None,
        "harness": "jcode",
        "skills": [],
        "builtin": True,
    },
    {
        "id": "9e7d109e7da66e8b5ed4ec3ecb54cef1",
        "name": "kimi-native-ui",
        "display_name": None,
        "description": None,
        "harness": "kimi-native",
        "skills": [],
        "builtin": True,
    },
    {
        "id": "cc8fef6623a8792be9c039cf2673ce93",
        "name": "kiro-native-ui",
        "display_name": None,
        "description": None,
        "harness": "kiro-native",
        "skills": [],
        "builtin": True,
    },
    {
        "id": "cf65137fc096a61a6434956c92093549",
        "name": "opencode-native-ui",
        "display_name": None,
        "description": None,
        "harness": "opencode-native",
        "skills": [],
        "builtin": True,
    },
    {
        "id": "a1b7caa17404f6716180ba69aa37c592",
        "name": "pi-native-ui",
        "display_name": None,
        "description": None,
        "harness": "pi-native",
        "skills": [],
        "builtin": True,
    },
    {
        "id": "057995d1517418e6839f51d340785dd6",
        "name": "polly",
        "display_name": None,
        "description": "A coding orchestrator that breaks your goal into pieces and hands them "
        "to a "
        "team of Claude Code, Codex, OpenCode, Cursor, Hermes, Pi, and Antigravity "
        "sub-agents to build. Polly writes no code itself — it plans and splits up "
        "the work, delegates all of it (investigation / implementation / review), "
        "then has a separate independent different-model reviewer double-check the "
        "work before putting it all together. Best for bigger tasks you want planned "
        "and split up.",
        "harness": "claude-sdk",
        "skills": [
            {
                "name": "cross-review",
                "description": "Verify an implementer's diff with an INDEPENDENT, "
                "different-vendor sub-agent (diff plus contract only); turn "
                "blocking issues into fix-tasks and loop until clean.",
            },
            {
                "name": "fanout",
                "description": "Run independent subtasks in parallel — one git worktree and one "
                "implementation sub-agent per task, each opening its own PR — "
                "then cross-review every PR. polly never merges; the human does.",
            },
            {
                "name": "investigate",
                "description": "Delegate read-only investigation, debugging, audit, search, or "
                "code-understanding tasks to sub-agents; synthesize only from "
                "their structured reports.",
            },
        ],
        "builtin": True,
    },
    {
        "id": "2cffe181a2fc6cf417a49e1cf8b28d77",
        "name": "qwen-native-ui",
        "display_name": None,
        "description": None,
        "harness": "qwen-native",
        "skills": [],
        "builtin": True,
    },
]
_HOSTS = {
    "hosts": [
        {
            "host_id": _HOST_ID,
            "name": "e2e-host",
            "owner": "e2e",
            "status": "online",
            "configured_harnesses": {agent["harness"]: True for agent in _AGENTS},
        }
    ]
}
# Catalogs comparable in length to the reporter's, so the settings page is
# taller than the space above the mid-screen composer and gets height-capped.
_CLAUDE_MODELS = [
    {"id": "opus", "model": "claude-opus-4-8", "displayName": "Opus 4.8", "isDefault": True},
    {
        "id": "opus[1m]",
        "model": "claude-opus-4-8[1m]",
        "displayName": "Opus 4.8 (1M context)",
        "isDefault": False,
    },
    {"id": "sonnet", "model": "claude-sonnet-5", "displayName": "Sonnet 5", "isDefault": False},
    {
        "id": "sonnet-4-6",
        "model": "claude-sonnet-4-6",
        "displayName": "Sonnet 4.6",
        "isDefault": False,
    },
    {"id": "haiku", "model": "claude-haiku-4-5", "displayName": "Haiku 4.5", "isDefault": False},
]
_CODEX_EFFORTS = [
    {"reasoningEffort": effort, "description": effort.title()}
    for effort in ("low", "medium", "high", "xhigh")
]
_CODEX_MODELS = [
    {
        "id": "gpt-5.6-sol",
        "displayName": "GPT-5.6-Sol",
        "isDefault": True,
        "defaultReasoningEffort": "high",
        "supportedReasoningEfforts": _CODEX_EFFORTS,
    },
    {
        "id": "gpt-5.5-codex",
        "displayName": "GPT-5.5-Codex",
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": _CODEX_EFFORTS,
    },
    {
        "id": "gpt-5-mini",
        "displayName": "GPT-5 Mini",
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": _CODEX_EFFORTS[:3],
    },
]


def _stub_landing_catalog(page: Page) -> None:
    """Serve the landing's host, agents and per-harness model catalogs from stubs.

    :param page: Playwright page, before navigation.
    :returns: None.
    """
    page.route("**/v1/hosts", lambda route: route.fulfill(json=_HOSTS))
    page.route("**/v1/agents", lambda route: route.fulfill(json={"data": _AGENTS}))
    page.route(
        "**/v1/hosts/*/harnesses/*/model-options",
        lambda route: route.fulfill(json={"models": []}),
    )
    # Registered after the generic stub so these win for the two harnesses under test.
    page.route(
        f"**/v1/hosts/{_HOST_ID}/harnesses/claude-native/model-options",
        lambda route: route.fulfill(json={"models": _CLAUDE_MODELS}),
    )
    page.route(
        f"**/v1/hosts/{_HOST_ID}/harnesses/codex-native/model-options",
        lambda route: route.fulfill(json={"models": _CODEX_MODELS}),
    )
    page.route(
        "**/v1/sandbox-providers/*/harnesses/*/model-options*",
        lambda route: route.fulfill(
            json={
                "configured": False,
                "status": "unconfigured",
                "models": [],
                "configuration_revision": None,
                "provider_label": None,
                "default_model": None,
            }
        ),
    )
    page.route("**/v1/hosts/*/worktrees*", lambda route: route.fulfill(json={"data": []}))
    page.route(
        re.compile(r"/v1/sessions\?"),
        lambda route: route.fulfill(json={"data": [], "has_more": False}),
    )


def _probe_height(page: Page, css_height: str) -> float:
    """Resolve a CSS height expression (e.g. ``env(safe-area-inset-top)``) in px.

    :param page: Playwright page.
    :param css_height: CSS ``height`` value to resolve.
    :returns: The rendered height in CSS px.
    """
    return page.evaluate(
        """(height) => {
            const probe = document.createElement("div");
            probe.style.cssText =
              `position:fixed;top:0;left:0;width:1px;pointer-events:none;height:${height}`;
            document.body.appendChild(probe);
            const px = probe.getBoundingClientRect().height;
            probe.remove();
            return px;
        }""",
        css_height,
    )


def _box(locator: Locator) -> FloatRect:
    """Return the element's bounding box, failing loudly when it has none.

    :param locator: A locator resolved to exactly one visible element.
    :returns: The element's bounding box.
    """
    box = locator.bounding_box()
    assert box is not None, f"element {locator} has no bounding box"
    return box


def _settled_box(page: Page, locator: Locator) -> FloatRect:
    """Return the element's bounding box once it is identical across two frames.

    :param page: Playwright page.
    :param locator: A locator resolved to exactly one visible element.
    :returns: The settled bounding box.
    """
    previous = _box(locator)
    for _ in range(60):
        page.evaluate(
            "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"
        )
        current = _box(locator)
        if current == previous:
            return current
        previous = current
    raise AssertionError(f"element {locator} never settled: {previous}")


def _open_advanced_settings(page: Page, agent_id: str) -> Locator:
    """Tap the model picker, then Edit on the agent's row; return the open sheet.

    :param page: Playwright page on the new-chat landing.
    :param agent_id: Stubbed agent id whose row to edit.
    :returns: Locator of the open dropdown content showing the settings page.
    """
    picker = page.get_by_test_id("new-chat-landing-agent-select")
    expect(picker).to_be_visible(timeout=30_000)
    expect(picker).to_be_enabled()
    picker.click()
    expect(picker).to_have_attribute("aria-expanded", "true")
    row = page.get_by_test_id(f"new-chat-landing-agent-{agent_id}")
    if row.count() == 0:
        page.get_by_test_id("new-chat-landing-harness-more").click()
    expect(row).to_be_visible()
    page.get_by_test_id(f"new-chat-landing-agent-config-{agent_id}").click()
    back = page.get_by_test_id("new-chat-landing-page-back")
    expect(back).to_be_visible()
    models = page.get_by_test_id("new-chat-landing-agent-models")
    expect(models).to_be_visible()
    expect(models.get_by_role("menuitemcheckbox").first).to_be_visible()
    sheet = page.locator('[data-slot="dropdown-menu-content"][data-state="open"]').filter(has=back)
    expect(sheet).to_be_visible()
    return sheet


@pytest.mark.parametrize(
    ("agent_id", "label"),
    [(_CLAUDE_AGENT_ID, "Claude Code"), (_CODEX_AGENT_ID, "Codex")],
    ids=["claude", "codex"],
)
def test_advanced_settings_sheet_stays_below_ios_safe_area(
    request: pytest.FixtureRequest,
    live_server: str,
    agent_id: str,
    label: str,
) -> None:
    """The advanced-settings sheet and its Back row start below the top inset.

    :param request: Pytest request, used to open the recorded page after setup.
    :param live_server: Base URL of the e2e server.
    :param agent_id: Stubbed agent whose settings to open.
    :param label: Harness display name, for assertion messages.
    :returns: None.
    """
    page: Page = request.getfixturevalue("page")
    if page.viewport_size != _IPHONE_VIEWPORT:
        page.set_viewport_size(_IPHONE_VIEWPORT)
    page.add_init_script(_IOS_SHELL_INIT_SCRIPT)
    cdp = page.context.new_cdp_session(page)
    cdp.send("Emulation.setSafeAreaInsetsOverride", {"insets": _IPHONE_SAFE_AREA})
    _stub_landing_catalog(page)
    safe_top = _IPHONE_SAFE_AREA["top"]

    try:
        page.goto(f"{live_server}/")
        expect(page.get_by_test_id("new-chat-landing-input")).to_be_visible(timeout=30_000)
        expect(page.locator(".app-shell").first).to_have_attribute("data-ios-native", "true")
        assert _probe_height(page, "env(safe-area-inset-top, 0px)") == safe_top
        assert _probe_height(page, "var(--omnigent-safe-top)") == safe_top

        sheet = _open_advanced_settings(page, agent_id)
        back = page.get_by_test_id("new-chat-landing-page-back")
        sheet_box = _settled_box(page, sheet)
        back_box = _settled_box(page, back)
        # Hold the open sheet so the outcome is readable in a recording.
        if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
            page.wait_for_timeout(1_500)

        assert sheet.evaluate("el => el.scrollHeight > el.clientHeight"), (
            f"the {label} advanced-settings page fits the menu, so the safe-area cap is "
            "not exercised"
        )
        assert sheet_box["y"] >= safe_top, (
            f"the {label} advanced-settings sheet starts at y={sheet_box['y']:.0f}px, inside "
            f"the {safe_top}px iOS status-bar safe area; its Back row is at "
            f"y={back_box['y']:.0f}px"
        )
        assert back_box["y"] >= safe_top, (
            f"the {label} sheet's Back row starts at y={back_box['y']:.0f}px, inside the "
            f"{safe_top}px iOS status-bar safe area"
        )
    finally:
        page.unroute_all(behavior="ignoreErrors")
