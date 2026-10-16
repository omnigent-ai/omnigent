"""Browser terminals recover after transport errors and temporary routing misses.

Proxy a real terminal WebSocket and close it twice, letting the browser
reconnect each time. Repeated 4400 (wrong replica) responses can occur while
a rollout changes the destination for a host. An OSS browser must keep
retrying instead of taking the Databricks-only fallback that drops the key.
"""

from __future__ import annotations

import re
import time

import httpx
import pytest
from playwright.sync_api import Page, WebSocketRoute, expect

from tests.e2e_ui.conftest import open_right_rail

_ATTACH_WS = re.compile(r"/resources/terminals/.*/attach")
_OSC11_RGB_RESPONSE = re.compile(
    r"\x1b\]11;rgb:[0-9a-fA-F]{1,4}/[0-9a-fA-F]{1,4}/[0-9a-fA-F]{1,4}(?:\x07|\x1b\\)"
)


def _input_contains_probe(raw: str, probe: str) -> bool:
    """Ignore only complete OSC11 color replies interleaved with the typed probe."""
    return probe in _OSC11_RGB_RESPONSE.sub("", raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("re\x1b]11;rgb:ffff/ffff/ffff\x1b\\connect-1", True, id="captured-st"),
        pytest.param("re\x1b]11;rgb:ffff/ffff/ffff\x07connect-1", True, id="color-bel"),
        pytest.param("r\x1b]11;rgb:ffff/ffff/ffff\x1b\\connect-1", False, id="missing-key"),
        pytest.param("re\x1b]11;rgb:ffff/ffff/ffffconnect-1", False, id="partial-response"),
        pytest.param("re\x1b]11;rgb:ffff/gggg/ffff\x1b\\connect-1", False, id="invalid-hex"),
        pytest.param("re\x1b]12;rgb:ffff/ffff/ffff\x1b\\connect-1", False, id="other-osc"),
        pytest.param("re\x1b]11;rgb:fffff/ffff/ffff\x1b\\connect-1", False, id="wide-channel"),
        pytest.param("re\x1b]11;rgb:ffff/ffff\x1b\\connect-1", False, id="missing-channel"),
    ],
)
def test_input_probe_ignores_only_complete_color_replies(raw: str, expected: bool) -> None:
    assert _input_contains_probe(raw, "reconnect-1") is expected


def _open_new_shell(page: Page) -> None:
    """Create a shell via the Workspace rail's "+" → Shell menu."""
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("button", name="Open new").click()
    page.get_by_role("menuitem", name=re.compile("Shell")).click()


@pytest.mark.parametrize("close_code", [1011, 4400], ids=["transport-error", "wrong-replica"])
@pytest.mark.parametrize("surface", ["shell", "agent-terminal"])
def test_embedded_terminal_reconnects_after_transport_close(
    page: Page, terminal_session: tuple[str, str], close_code: int, surface: str
) -> None:
    """Both terminal surfaces reconnect and accept input after two transient closes."""
    base_url, session_id = terminal_session
    live: dict[str, object] = {"ws": None, "dials": 0, "input": ""}

    def _handle(ws: WebSocketRoute) -> None:
        live["dials"] = int(live["dials"]) + 1
        live["ws"] = ws
        # Transparent proxy to the real terminal bridge (binary frames pass
        # through unchanged); the test drives the drop from outside.
        server = ws.connect_to_server()

        def forward_input(message: str | bytes) -> None:
            text = message.decode(errors="replace") if isinstance(message, bytes) else message
            live["input"] = str(live["input"]) + text
            server.send(message)

        ws.on_message(forward_input)
        server.on_message(lambda message: ws.send(message))

    page.route_web_socket(_ATTACH_WS, _handle)

    if surface == "agent-terminal":
        response = httpx.patch(
            f"{base_url}/v1/sessions/{session_id}",
            json={"labels": {"omnigent.ui": "terminal"}},
            timeout=10,
        )
        response.raise_for_status()
        page.goto(f"{base_url}/c/{session_id}")
        page.get_by_test_id("view-mode-terminal").click()
        terminal_view = page.get_by_test_id("main-terminal-view").get_by_test_id("terminal-view")
    else:
        page.goto(f"{base_url}/c/{session_id}")
        _open_new_shell(page)
        rail = page.get_by_role("complementary", name="Workspace")
        terminal_view = rail.get_by_test_id("terminal-view").last
    expect(terminal_view).to_be_visible(timeout=60_000)
    # The live terminal attaches through the proxy.
    expect(terminal_view).to_have_attribute("data-state", "connected", timeout=30_000)
    for attempt in range(2):
        dials_before = int(live["dials"])
        # Closing in the route handler would block Playwright's sync API.
        assert isinstance(live["ws"], WebSocketRoute)
        live["ws"].close(code=close_code)

        expect(page.get_by_test_id("terminal-reconnecting")).to_be_visible(timeout=20_000)
        expect(page.get_by_text(re.compile("Bridge closed"))).to_have_count(0)
        expect(terminal_view).to_have_attribute("data-state", "connected", timeout=60_000)
        assert int(live["dials"]) > dials_before, "terminal did not reconnect"

        probe = f"reconnect-{attempt}"
        live["input"] = ""
        terminal_view.locator("textarea.xterm-helper-textarea").focus()
        page.keyboard.type(probe)
        deadline = time.monotonic() + 10
        while not _input_contains_probe(str(live["input"]), probe) and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert _input_contains_probe(str(live["input"]), probe), (
            f"terminal did not forward input after reconnecting: {live['input']!r}"
        )
        page.keyboard.press("Control+U")
