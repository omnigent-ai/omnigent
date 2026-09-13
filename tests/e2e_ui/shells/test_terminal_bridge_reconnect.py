"""E2E: the embedded terminal reconnects after a code-less bridge close.

A server redeploy behind the Databricks Apps ingress tears the terminal
attach WebSocket down without a clean app close code reaching the browser,
which reports ``1005`` ("no status"). The client must treat that transport
drop like the ``1006`` it already handled — show a "Reconnecting…" overlay
and re-attach — instead of dead-ending on "Bridge closed: code 1005"
(``isUnexpectedTerminalClose`` in
``web/src/components/blocks/TerminalSession.ts``; the retry budget in
``TerminalView.tsx``).

The attach WebSocket is proxied through ``page.route_web_socket`` so the test
holds a handle to the live connection; once the terminal is connected, the
test closes that connection with ``1011`` (server internal error) and the
next dial is proxied straight back to the real bridge — so the terminal
recovers on its own. 1011 stands in for the whole newly-reconnectable set
(1005/1011/1014): it is a real on-the-wire code the browser reports verbatim,
whereas the reported ``1005`` is a reserved sentinel a proxy synthesizes that
Playwright cannot reproduce end to end (its classification is pinned by the
``TerminalSession`` unit test). Before the fix every one of these was
classified deliberate and the terminal dead-ended; the
``data-state="connected"`` assertion AFTER the drop is what that regression
would fail. Closing from the test body (not the route handler) avoids the
sync-API deadlock of a blocking call inside the handler.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, WebSocketRoute, expect

from tests.e2e_ui.conftest import open_right_rail

_ATTACH_WS = re.compile(r"/resources/terminals/.*/attach")


def _open_new_shell(page: Page) -> None:
    """Create a shell via the Workspace rail's "+" → Shell menu."""
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("button", name="Open new").click()
    page.get_by_role("menuitem", name=re.compile("Shell")).click()


def test_embedded_terminal_reconnects_after_transport_close(
    page: Page, terminal_session: tuple[str, str]
) -> None:
    """A transport-level attach close (1011) recovers instead of dead-ending.

    Proxy the attach WebSocket, let the terminal connect, then close that
    connection with ``1011`` — a newly-reconnectable transport code (same
    class as the reported ``1005`` a redeploy behind the ingress produces).
    The overlay must read "Reconnecting…" (never the dead-end "Bridge
    closed"), and the proxied retry must re-attach the terminal back to
    ``connected``.

    :param page: Playwright page fixture.
    :param terminal_session: ``(base_url, session_id)`` with the terminal agent.
    :returns: None.
    """
    base_url, session_id = terminal_session
    live: dict[str, object] = {"ws": None, "dials": 0}

    def _handle(ws: WebSocketRoute) -> None:
        live["dials"] = int(live["dials"]) + 1
        live["ws"] = ws
        # Transparent proxy to the real terminal bridge (binary frames pass
        # through unchanged); the test drives the drop from outside.
        server = ws.connect_to_server()
        ws.on_message(lambda message: server.send(message))
        server.on_message(lambda message: ws.send(message))

    page.route_web_socket(_ATTACH_WS, _handle)

    page.goto(f"{base_url}/c/{session_id}")
    _open_new_shell(page)

    rail = page.get_by_role("complementary", name="Workspace")
    terminal_view = rail.get_by_test_id("terminal-view").last
    expect(terminal_view).to_be_visible(timeout=60_000)
    # The live terminal attaches through the proxy.
    expect(terminal_view).to_have_attribute("data-state", "connected", timeout=30_000)
    dials_before = int(live["dials"])

    # Simulate the redeploy: drop the attach socket with a server-error code
    # from the test body (a blocking close inside the route handler deadlocks
    # the sync API). 1011 is a real on-the-wire code in the newly-reconnectable
    # set — the browser reports it verbatim, unlike the reserved 1005/1006
    # sentinels a proxy synthesizes (covered by the TerminalSession unit test).
    assert isinstance(live["ws"], WebSocketRoute)
    live["ws"].close(code=1011)

    # Recovery is presented AS recovery, and the dead-end message never shows.
    # Before the fix, a 1005 close was terminal and this overlay never appeared.
    expect(page.get_by_test_id("terminal-reconnecting")).to_be_visible(timeout=20_000)
    expect(page.get_by_text(re.compile("Bridge closed"))).to_have_count(0)

    # The proxied retry re-attaches the terminal on its own — no manual refresh.
    expect(terminal_view).to_have_attribute("data-state", "connected", timeout=60_000)
    assert int(live["dials"]) > dials_before, "terminal did not re-dial after the code-less drop"
