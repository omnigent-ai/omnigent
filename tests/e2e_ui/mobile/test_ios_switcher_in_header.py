"""iOS shell: the Chat/Terminal switcher must live in the header, not below the composer.

The Chat/Terminal switcher for terminal-first sessions lives in the top-right
header controls (``ViewModeToggle`` in ``web/src/shell/ViewModeToggle.tsx``) on
every shell, iOS included. The iOS shell used to get a different placement: the
SPA suppressed the header toggle under ``isIOSShell()`` and instead commanded
the shell's bottom pill (``web/ios/Omnigent/ChatTerminalBar.swift``) visible
below the composer via the ``setViewMode`` bridge call — leaving web and iPhone
with two inconsistent navigation patterns.

The SPA owns both halves of that placement decision (render the header toggle;
keep the bottom bar hidden), so the contract is fully observable in a browser:
inject a minimal ``window.omnigentNative = {kind: "ios", ...}`` bridge before
any app script runs (the same feature-detection stubbing
``test_android_shell.py`` uses) and record every ``setViewMode`` push.

Parity expectation encoded here (fails if the iOS placement regresses):

1. under the iOS bridge, the header ``view-mode-toggle`` renders like it does
   on the mobile web header; and
2. the shell is never told to float the bottom pill (``setViewMode`` never
   pushes ``visible: true``).

A companion control test proves the same journey in a plain mobile browser
also shows the header switcher, so the iOS-bridge case can only fail on an
iOS-specific gate, not a missing control or mobile-layout difference.
"""

from __future__ import annotations

import json
import re

import httpx
from playwright.sync_api import Page, Route, ViewportSize, expect

# iPhone-sized viewport: matches the report (iPhone) and keeps the SPA in the
# mobile header layout, where the composer does not autofocus (an autofocused
# composer would count as "keyboard visible" and hide the native bar, masking
# the placement decision under test).
_MOBILE_VIEWPORT: ViewportSize = {"width": 390, "height": 844}

# Minimal stand-in for the iOS WKWebView bridge (``web/ios``'s injected
# ``window.omnigentNative``). Runs before any app script on every navigation
# (``add_init_script``) so ``isIOSShell()`` in ``nativeBridge.ts`` sees the iOS
# shell. ``setViewMode`` records every push so the test can assert what the SPA
# told the native Chat/Terminal bar to do; ``onNativeInsets`` immediately pushes
# a realistic footprint (the real shell caches and replays its last emit), so
# the bottom spacer resolves to the pill's true height. Everything else is a
# guarded no-op keeping unrelated native calls from throwing under the stub.
_IOS_SHELL_INIT_SCRIPT = """
window.__omnigentSetViewModeCalls = [];
window.omnigentNative = {
  kind: "ios",
  setBadgeCount: function () {},
  notify: function () { return Promise.resolve(false); },
  onNotificationActivated: function () { return function () {}; },
  onOpenPath: function () { return function () {}; },
  onSidebarDrag: function () { return function () {}; },
  setServerSwitcherHidden: function () {},
  setViewMode: function (params) {
    window.__omnigentSetViewModeCalls.push(params);
  },
  onViewModeChanged: function () { return function () {}; },
  onNativeInsets: function (callback) {
    callback({ topBar: 36, bottomBar: 48 });
    return function () {};
  },
};
"""


def _mark_terminal_first(base_url: str, session_id: str) -> None:
    """Stamp the session terminal-first (``omnigent.ui = terminal``).

    The Chat/Terminal switcher only exists for terminal-first sessions, so the
    label is the journey's precondition — the same one ``omnigent claude`` /
    ``omnigent codex`` sessions carry.

    :param base_url: Spawned server base URL.
    :param session_id: Session to label.
    """
    response = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"labels": {"omnigent.ui": "terminal"}},
        timeout=10.0,
    )
    response.raise_for_status()


def _route_agent_terminal(page: Page, session_id: str) -> None:
    """Serve a deterministic agent terminal pane for the session.

    The switcher's placement does not need a live PTY, only the resource shape
    the runner publishes — mirrors ``test_terminal_view_url.py``.

    :param page: Page whose network to intercept.
    :param session_id: Session whose terminals list to stub.
    """

    def _serve(route: Route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "terminal_tui_main",
                            "type": "terminal",
                            "session_id": session_id,
                            "name": "tui:main",
                            "metadata": {
                                "terminal_name": "tui",
                                "session_key": "main",
                                "running": True,
                            },
                        }
                    ],
                    "first_id": "terminal_tui_main",
                    "last_id": "terminal_tui_main",
                    "has_more": False,
                }
            ),
        )

    terminal_list = re.compile(rf"/v1/sessions/{re.escape(session_id)}/resources/terminals\?.*")
    page.route(terminal_list, _serve)


def _open_terminal_first_session(page: Page, seeded_session: tuple[str, str]) -> None:
    """Drive the shared journey: open a terminal-first session on a phone.

    Waits for the composer (the report's "view the chat with the composer
    visible" step) — a wait that holds both before and after any fix, so the
    assertions that follow fail on the bug, not on hydration timing.

    :param page: Playwright page (viewport/bridge already configured).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    """
    base_url, session_id = seeded_session
    _mark_terminal_first(base_url, session_id)
    _route_agent_terminal(page, session_id)
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.locator('textarea[aria-label="Message the agent"]')
    expect(composer).to_be_visible(timeout=60_000)


def test_ios_shell_shows_switcher_in_header_not_below_composer(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Under the iOS bridge the switcher must match the web header placement.

    Drives the journey — open a terminal-first Omnigent session in the iOS
    app, view the chat with the composer visible, look below the composer —
    and asserts the parity contract: the Chat/Terminal switcher renders in the
    header (like the mobile web header) and the native bottom pill is never
    commanded visible below the composer.

    Regression shape this guards: ``ViewModeToggle`` gating itself off under
    ``isIOSShell()`` while the SPA pushes ``setViewMode({visible: true, ...})``,
    floating the retired pill below the composer.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    """
    page.set_viewport_size(_MOBILE_VIEWPORT)
    page.add_init_script(_IOS_SHELL_INIT_SCRIPT)
    _open_terminal_first_session(page, seeded_session)

    # The bridge stub took: the SPA is running its iOS-shell chrome.
    expect(page.locator(".app-shell")).to_have_attribute("data-ios-native", "true")

    # Give the mount-time bridge pushes a beat to land, then log the observed
    # state so a failing run records exactly what the shell was told.
    page.wait_for_timeout(500)
    calls = page.evaluate("() => window.__omnigentSetViewModeCalls")
    toggle_count = page.get_by_test_id("view-mode-toggle").count()
    print(f"[ios-switcher] header view-mode-toggle count: {toggle_count}")
    print(f"[ios-switcher] native setViewMode pushes: {calls}")

    # Expected parity half 1: the switcher lives in the header on iOS too.
    # The composer wait above already settled hydration, so the toggle renders
    # (or never will) well within this window.
    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(timeout=8_000)

    # Expected parity half 2: the obsolete pill below the composer is gone —
    # the SPA never tells the shell to float the native Chat/Terminal bar.
    calls = page.evaluate("() => window.__omnigentSetViewModeCalls")
    shown = [c for c in calls if c.get("visible")]
    assert not shown, (
        "iOS shell was told to float the Chat/Terminal pill below the composer "
        f"(setViewMode pushes with visible=true: {shown}); expected the header "
        "switcher instead, matching the web UI."
    )


def test_mobile_web_browser_shows_switcher_in_header(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Control: the same journey in a plain mobile browser has the header switcher.

    Proves the header ``view-mode-toggle`` exists at the phone viewport when no
    iOS bridge is injected — so the sibling test's failure is the iOS gate, not
    a missing control or a mobile-layout difference.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    """
    page.set_viewport_size(_MOBILE_VIEWPORT)
    _open_terminal_first_session(page, seeded_session)

    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(timeout=8_000)
