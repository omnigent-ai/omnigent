"""E2E: terminal clipboard frames require consent before writing locally.

The terminal resource and attach WebSocket are browser-route mocks. This keeps
this test tmux-free while exercising the built SPA's real TerminalView,
TerminalSession, xterm input bookkeeping, WebSocket parsing, Sonner consent UI,
and browser Clipboard API.
"""

from __future__ import annotations

import base64
import json
import re
import time
import uuid
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, Route, WebSocketRoute, expect

from tests.e2e_ui.conftest import _build_hello_world_bundle, open_right_rail

_TERMINAL_ID = "terminal_clipboard_e2e"
_TERMINAL_LABEL = "bash · clipboard-e2e"


@pytest.fixture
def clipboard_ui_session(live_server: str) -> Iterator[tuple[str, str]]:
    """Create an unbound session; no runner or tmux terminal is launched."""
    response = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={
            "bundle": (
                "agent.tar.gz",
                _build_hello_world_bundle(),
                "application/gzip",
            )
        },
        timeout=30.0,
    )
    response.raise_for_status()
    session_id = response.json()["session_id"]
    try:
        yield live_server, session_id
    finally:
        httpx.delete(
            f"{live_server}/v1/sessions/{session_id}",
            timeout=10.0,
        ).raise_for_status()


def _clipboard_frame(text: str) -> str:
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return json.dumps(
        {
            "type": "clipboard-write",
            "encoding": "base64",
            "data": encoded,
        },
        separators=(",", ":"),
    )


def _expect_clipboard(page: Page, expected: str) -> None:
    """Poll because navigator.clipboard.writeText resolves asynchronously."""
    deadline = time.monotonic() + 5
    actual = ""
    while time.monotonic() < deadline:
        actual = page.evaluate("() => navigator.clipboard.readText()")
        if actual == expected:
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"clipboard was {actual!r}, expected {expected!r}")


def test_terminal_clipboard_prompt_and_session_grant(
    page: Page,
    clipboard_ui_session: tuple[str, str],
) -> None:
    """The first copy asks; a session grant makes the next copy automatic."""
    base_url, session_id = clipboard_ui_session
    sockets: list[WebSocketRoute] = []

    terminal_list = re.compile(
        rf"/v1/sessions/{re.escape(session_id)}/resources/terminals(?:\?|$)"
    )

    def _serve_terminal(route: Route) -> None:
        if route.request.method != "GET":
            route.continue_()
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": _TERMINAL_ID,
                            "object": "terminal",
                            "name": "bash",
                            "metadata": {
                                "terminal_name": "bash",
                                "session_key": "clipboard-e2e",
                                "running": True,
                                "terminal_transport": "control",
                            },
                        }
                    ],
                    "first_id": _TERMINAL_ID,
                    "last_id": _TERMINAL_ID,
                    "has_more": False,
                }
            ),
        )

    def _attach(ws: WebSocketRoute) -> None:
        sockets.append(ws)
        # Swallow resize/input frames; the test injects server frames below.
        ws.on_message(lambda _message: None)

    page.route(terminal_list, _serve_terminal)
    page.route_web_socket(
        re.compile(rf"/resources/terminals/{re.escape(_TERMINAL_ID)}/attach"),
        _attach,
    )
    page.context.grant_permissions(
        ["clipboard-read", "clipboard-write"],
        origin=base_url,
    )

    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)

    rail = page.get_by_role("complementary", name="Workspace")
    shell_tab = rail.get_by_text(_TERMINAL_LABEL, exact=True)
    expect(shell_tab).to_be_visible(timeout=30_000)
    shell_tab.click()

    terminal = rail.get_by_test_id("terminal-view")
    expect(terminal).to_have_attribute("data-state", "connected", timeout=20_000)
    assert sockets, "mock terminal attach WebSocket did not connect"

    textarea = terminal.locator("textarea.xterm-helper-textarea")
    consent = page.get_by_test_id("terminal-clipboard-consent")

    first = f"terminal-first-{uuid.uuid4().hex}"
    textarea.focus()
    page.keyboard.type("a")  # Satisfy TerminalSession's recent-input gate.
    sockets[-1].send(_clipboard_frame(first))

    expect(consent).to_be_visible()
    expect(consent).to_contain_text("Allow this terminal to copy to your clipboard?")
    expect(consent.get_by_role("button", name="Allow for this session")).to_be_visible()
    expect(consent.get_by_role("button", name="Copy once")).to_be_visible()
    expect(consent.get_by_role("button", name="Block")).to_be_visible()

    consent.get_by_role("button", name="Allow for this session").click()
    _expect_clipboard(page, first)
    expect(consent).to_have_count(0)

    # A second frame in the same mounted TerminalView copies automatically.
    second = f"terminal-second-{uuid.uuid4().hex}"
    textarea.focus()
    page.keyboard.type("b")
    sockets[-1].send(_clipboard_frame(second))

    _expect_clipboard(page, second)
    expect(consent).to_have_count(0)
