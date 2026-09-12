"""Browser e2e: a hidden warm terminal's scrollbar must not leak into chat.

ChatPage keeps a session's terminal surface mounted as a hidden
``absolute inset-0`` overlay while chat is frontmost, so flipping back is
instant (the pre-warmed surface in ``MainAgentSurface``). xterm marks its
active scrollbar with a ``visible`` class, which collides with Tailwind's
``.visible { visibility: visible }`` utility: hiding the overlay with
inherited ``visibility`` alone lets the scrollbar opt itself back in and
paint over the chat pane (a ``z-index: 11`` strip near the pane's right
edge) where it also swallows pointer events. The overlay must be hidden
with a mechanism descendants cannot undo — asserted here in a real
browser against the compiled Tailwind CSS.

The terminal is scripted rather than live: the agent-terminal inventory
is mocked with the same resource shape the runner publishes (as
``test_terminal_view_url.py`` does) and the attach WebSocket is served by
the test, so scrollback and streaming output are deterministic. Every
output burst emits a scroll event, and each scroll re-reveals xterm's
scrollbar for its ~500ms hide window — the revealed state is exactly what
used to bleed through over chat while a session streamed.
"""

from __future__ import annotations

import json
import re
import time

import httpx
from playwright.sync_api import Page, Route, WebSocketRoute, expect

_ATTACH_WS = re.compile(r"/resources/terminals/.*/attach")

# Enough lines to overflow the viewport so the scrollbar is needed at all.
_SCROLLBACK_BURST = b"scrollback line\r\n" * 200

# Scripted PTY output while chat is frontmost: each burst scrolls the
# viewport, which re-reveals the scrollbar (revealOnScroll).
_STREAM_BURST = b"streaming output line\r\n" * 3

# How the (possibly leaked) scrollbar presents to a user right now:
# - revealed: xterm currently marks it active (class ``visible``) — the
#   class that collides with Tailwind's utility;
# - paints: the browser would actually render it (its computed visibility
#   AND every ancestor's opacity allow painting);
# - intercepts: it is a pointer target above the chat pane, i.e. a click
#   near the pane's right edge would hit the hidden terminal, not chat.
_SCROLLBAR_STATE_JS = """
el => {
  const surface = el.closest('[data-testid="main-terminal-view"]');
  const box = el.getBoundingClientRect();
  const stack = document.elementsFromPoint(
    box.x + box.width / 2,
    box.y + box.height / 2,
  );
  return {
    revealed: el.classList.contains("visible"),
    paints: el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true }),
    intercepts: surface !== null && stack.some((node) => surface.contains(node)),
  };
}
"""


def test_hidden_terminal_scrollbar_neither_paints_nor_intercepts(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """While chat is frontmost, the warm terminal's scrollbar stays inert.

    Journey: open the terminal view of a terminal-first session, fill it
    with scrollback, switch to chat, and keep the scripted PTY streaming.
    The stream keeps xterm's scrollbar in its revealed state (the
    precondition the test asserts explicitly), and in that state the
    scrollbar must neither paint over the chat pane nor intercept pointer
    events above it. Switching back to the terminal must still show a
    painting terminal — the overlay is hidden, not gone.
    """
    base_url, session_id = seeded_session
    response = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"labels": {"omnigent.ui": "terminal"}},
        timeout=10.0,
    )
    response.raise_for_status()

    # Publish the agent pane deterministically — same resource shape the
    # runner publishes, no live PTY needed (the attach socket is scripted).
    def _serve_agent_terminal(route: Route) -> None:
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
    page.route(terminal_list, _serve_agent_terminal)

    # Serve the attach WebSocket: a burst of scrollback on connect, then
    # whatever the test drips in later. Binary frames are raw PTY bytes.
    sockets: list[WebSocketRoute] = []

    def _serve_attach(ws: WebSocketRoute) -> None:
        sockets.append(ws)
        ws.send(_SCROLLBACK_BURST)

    page.route_web_socket(_ATTACH_WS, _serve_attach)

    page.goto(f"{base_url}/c/{session_id}")
    terminal_button = page.get_by_test_id("view-mode-terminal")
    expect(terminal_button).to_be_enabled(timeout=60_000)
    terminal_button.click()

    # The terminal view fronts, attaches to the scripted socket, and has
    # enough scrollback that its scrollbar is needed.
    shown = page.locator('[data-testid="main-terminal-view"][data-visible="true"]')
    expect(shown).to_be_visible(timeout=30_000)
    expect(shown.get_by_test_id("terminal-view")).to_have_attribute(
        "data-state", "connected", timeout=30_000
    )
    # The vertical scrollbar specifically — the horizontal one is disabled
    # (ScrollbarVisibility.Hidden) and stays a zero-size `invisible` node.
    scrollbar = page.locator(
        '[data-testid="main-terminal-view"] .xterm-scrollable-element > .scrollbar.vertical'
    ).first
    expect(scrollbar).to_be_attached(timeout=30_000)

    # Chat fronts; the warm terminal surface stays mounted but hidden.
    page.get_by_test_id("view-mode-chat").click()
    hidden = page.locator('[data-testid="main-terminal-view"][data-visible="false"]')
    expect(hidden).to_be_attached(timeout=30_000)
    expect(page.get_by_placeholder("Send a message…")).to_be_visible()

    # Keep the scripted PTY streaming and sample the scrollbar whenever
    # xterm has it revealed — the state that used to bleed through.
    revealed_samples: list[dict[str, bool]] = []
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and len(revealed_samples) < 5:
        for socket in list(sockets):
            socket.send(_STREAM_BURST)
        page.wait_for_timeout(120)
        state = scrollbar.evaluate(_SCROLLBAR_STATE_JS)
        if state["revealed"]:
            revealed_samples.append(state)
    assert revealed_samples, (
        "precondition failed: streaming output never put xterm's scrollbar "
        "into its revealed ('visible') state while the terminal was hidden"
    )

    leaked = [s for s in revealed_samples if s["paints"] or s["intercepts"]]
    assert not leaked, (
        "the hidden terminal's revealed scrollbar leaked into the chat pane "
        f"(paints/intercepts per sample: {revealed_samples}) — the warm "
        "overlay's hiding mechanism lets descendants opt back in"
    )

    # The overlay is hidden, not gone: fronting the terminal again must
    # still paint it (guards against over-hiding, e.g. display:none).
    page.get_by_test_id("view-mode-terminal").click()
    expect(shown).to_be_visible(timeout=30_000)
    xterm = shown.locator(".xterm").first
    expect(xterm).to_be_visible(timeout=30_000)
    assert xterm.evaluate(
        "el => el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })"
    ), "the terminal must paint again once it is fronted"
