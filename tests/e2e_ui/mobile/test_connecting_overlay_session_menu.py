"""E2E: the terminal "Connecting…" overlay must not bury the session menu.

The iOS shell renders this same SPA in a WebView, so a phone-sized
viewport reproduces its layout. A terminal-first session whose terminal
bridge is still dialing paints the full-surface "Connecting…" status
overlay — a translucent layer over the whole terminal area. The header's
session menu is a dropdown portaled to ``<body>``; while the overlay is
up, an open menu must stay usable (its items hit-testable and tappable),
or the menu must not present as open at all. The broken state is a menu
that is open yet painted over by the translucent overlay, leaving its
rows faintly visible but untappable.

The terminal-attach WebSocket is replaced with a dial that never
completes — what a phone on a stalled network sees — so the terminal
stays in "Connecting…" deterministically while the menu is exercised.
"""

from __future__ import annotations

import json
import re
import time

import httpx
from playwright.sync_api import Page, Route, ViewportSize, expect

_MOBILE_VIEWPORT: ViewportSize = {"width": 390, "height": 844}

# Replace the terminal-attach WebSocket with a dial that never completes
# (readyState stays CONNECTING), pinning the status overlay to "Connecting…".
_HOLD_ATTACH_DIAL = """
(() => {
  const NativeWebSocket = window.WebSocket;
  const heldUrl = /\\/resources\\/terminals\\/[^/]+\\/attach/;
  function HeldWebSocket(url, protocols) {
    if (!heldUrl.test(String(url))) {
      return protocols === undefined
        ? new NativeWebSocket(url)
        : new NativeWebSocket(url, protocols);
    }
    const stub = new EventTarget();
    return Object.assign(stub, {
      url: String(url),
      readyState: NativeWebSocket.CONNECTING,
      bufferedAmount: 0,
      extensions: "",
      protocol: "",
      binaryType: "arraybuffer",
      onopen: null,
      onmessage: null,
      onerror: null,
      onclose: null,
      send() {},
      close() {
        stub.readyState = NativeWebSocket.CLOSED;
      },
    });
  }
  HeldWebSocket.prototype = NativeWebSocket.prototype;
  for (const k of ["CONNECTING", "OPEN", "CLOSING", "CLOSED"]) {
    HeldWebSocket[k] = NativeWebSocket[k];
  }
  window.WebSocket = HeldWebSocket;
})();
"""


def _serve_agent_terminal(route: Route) -> None:
    """Publish one running agent terminal, the shape the runner reports."""
    match = re.search(r"/v1/sessions/([^/]+)/", route.request.url)
    assert match is not None
    session_id = match.group(1)
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


def test_connecting_overlay_keeps_session_menu_usable(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """An open session menu stays tappable while the terminal is connecting.

    With the attach dial held open the terminal surface shows the
    "Connecting…" status overlay. Opening the header's session menu must
    then leave the menu items as the top hit-target at their own centers;
    a translucent overlay painting above the open menu (items visible but
    taps landing on the overlay) is the regression.
    """
    base_url, session_id = seeded_session
    httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"labels": {"omnigent.ui": "terminal"}},
        timeout=10.0,
    ).raise_for_status()

    page.set_viewport_size(_MOBILE_VIEWPORT)
    page.add_init_script(_HOLD_ATTACH_DIAL)
    terminal_list = re.compile(rf"/v1/sessions/{re.escape(session_id)}/resources/terminals\?.*")
    page.route(terminal_list, _serve_agent_terminal)

    page.goto(f"{base_url}/c/{session_id}?view=terminal")

    terminal_view = page.get_by_test_id("main-terminal-view").get_by_test_id("terminal-view")
    expect(terminal_view).to_be_visible(timeout=60_000)
    expect(terminal_view).to_have_attribute("data-state", "connecting", timeout=30_000)
    expect(terminal_view.get_by_text("Connecting…", exact=True)).to_be_visible()

    page.get_by_test_id("header-conversation-actions").click()
    menu = page.get_by_role("menu")
    try:
        expect(menu).to_be_visible(timeout=3_000)
    except AssertionError:
        # Keeping the menu closed while connecting is a valid resolution;
        # the broken state is an open menu painted over.
        return

    first_item = menu.get_by_role("menuitem").first
    expect(first_item).to_be_visible()

    # elementFromPoint mirrors where a tap lands. Poll past the menu's
    # entrance animation; a covered menu never becomes tappable.
    blocker: str | None = None
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        blocker = first_item.evaluate(
            """(item) => {
              const menu = item.closest('[role="menu"]');
              const rect = item.getBoundingClientRect();
              const hit = document.elementFromPoint(
                rect.x + rect.width / 2, rect.y + rect.height / 2);
              if (hit && menu && menu.contains(hit)) return null;
              if (!hit) return "<nothing>";
              const cls = typeof hit.className === "string" ? hit.className : "";
              return `<${hit.tagName.toLowerCase()} class="${cls.slice(0, 160)}">`;
            }"""
        )
        if blocker is None:
            break
        time.sleep(0.1)

    assert blocker is None, (
        "The open session menu is covered by another element — taps on its "
        f"items land on {blocker} instead of the menu"
    )
