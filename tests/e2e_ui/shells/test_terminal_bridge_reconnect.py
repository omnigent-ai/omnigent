"""Browser terminals recover after transport errors and temporary routing misses.

Proxy a real terminal WebSocket and close it twice, letting the browser
reconnect each time. Repeated 4400 (wrong replica) responses can occur while
a rollout changes the destination for a host. An OSS browser must keep
retrying instead of taking the Databricks-only fallback that drops the key.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, WebSocketRoute, expect

from tests.e2e_ui.conftest import open_right_rail

_ATTACH_WS = re.compile(r"/resources/terminals/.*/attach")


def _open_new_shell(page: Page) -> None:
    """Create a shell via the Workspace rail's "+" → Shell menu."""
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("button", name="Open new").click()
    page.get_by_role("menuitem", name=re.compile("Shell")).click()


@pytest.mark.parametrize("close_code", [1011, 4400], ids=["transport-error", "wrong-replica"])
def test_embedded_terminal_reconnects_after_transport_close(
    page: Page, terminal_session: tuple[str, str], close_code: int
) -> None:
    """A shell reconnects automatically after each of two transient closes."""
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
    for _ in range(2):
        dials_before = int(live["dials"])
        # Closing in the route handler would block Playwright's sync API.
        assert isinstance(live["ws"], WebSocketRoute)
        live["ws"].close(code=close_code)

        expect(page.get_by_test_id("terminal-reconnecting")).to_be_visible(timeout=20_000)
        expect(page.get_by_text(re.compile("Bridge closed"))).to_have_count(0)
        expect(terminal_view).to_have_attribute("data-state", "connected", timeout=60_000)
        assert int(live["dials"]) > dials_before, "terminal did not reconnect"
