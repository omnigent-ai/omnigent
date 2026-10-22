"""Chat view must hold no terminal attach; Terminal view attaches on demand.

Each attach is a tmux client whose size can resize a window the user also has
open in a terminal. The sessions and transcript are real, but their terminal
inventory and attach WebSockets are browser mocks. Binary output exercises the
real xterm scrollbar without launching a CLI or calling an LLM.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterator

import httpx
import pytest
from playwright.sync_api import Browser, Locator, Page, Route, WebSocketRoute, expect

from tests.e2e_ui.conftest import _build_hello_world_bundle, fetch_with_retry

_TERMINAL_ID = "terminal_codex_main"
_BAR = ".xterm .scrollbar.vertical"
_SLIDER = f"{_BAR} .slider"
# A surface mounted in Chat view dials as soon as the inventory loads.
_CHAT_VIEW_HOLD_MS = 3_000
_PAINT = """el => {
  let opacity = 1;
  let displayed = true;
  for (let node = el; node; node = node.parentElement) {
    const style = getComputedStyle(node);
    opacity *= Number(style.opacity);
    displayed &&= style.display !== 'none';
  }
  const rect = el.getBoundingClientRect();
  return {opacity, displayed, visibility: getComputedStyle(el).visibility,
    width: rect.width, height: rect.height};
}"""
# WebSocketRoute.on_close raises KeyError for a close without a code, so read
# the page's own socket state instead.
_TRACK_ATTACH_SOCKETS = """() => {
  const sockets = (window.__terminalAttachSockets = []);
  window.WebSocket = class extends window.WebSocket {
    constructor(...args) {
      super(...args);
      if (String(args[0]).includes('/attach')) sockets.push(this);
    }
  };
}"""
_WEBSOCKET_CLOSED = 3


@pytest.fixture
def terminal_scrollbar_session(live_server: str) -> Iterator[tuple[str, str, str]]:
    """Create an unbound terminal-first session with an overflowing transcript.

    Also creates a plain chat session to switch to.
    """
    with httpx.Client(base_url=live_server, timeout=30) as client:
        created: list[str] = []
        try:
            for metadata in (
                {"labels": {"omnigent.ui": "terminal", "omnigent.wrapper": "codex-native-ui"}},
                {},
            ):
                response = client.post(
                    "/v1/sessions",
                    data={"metadata": json.dumps(metadata)},
                    files={"bundle": ("agent.tar.gz", _build_hello_world_bundle())},
                )
                response.raise_for_status()
                created.append(response.json()["session_id"])
            session_id, other_session_id = created
            for role, text in (
                ("user", "Inspect the chat layout."),
                (
                    "assistant",
                    "\n\n".join(f"Transcript paragraph {n}." for n in range(32)),
                ),
            ):
                client.post(
                    f"/v1/sessions/{session_id}/events",
                    json={
                        "type": "external_conversation_item",
                        "data": {
                            "item_type": "message",
                            "response_id": "resp_scrollbar",
                            "item_data": {
                                "role": role,
                                "agent": "hello_world",
                                "content": [
                                    {
                                        "type": "input_text" if role == "user" else "output_text",
                                        "text": text,
                                    }
                                ],
                            },
                        },
                    },
                ).raise_for_status()
            yield live_server, session_id, other_session_id
        finally:
            responses = [client.delete(f"/v1/sessions/{created_id}") for created_id in created]
            for response in responses:
                response.raise_for_status()


class _TerminalMock:
    def __init__(self) -> None:
        self.inventory_requests = 0
        self.sockets: list[WebSocketRoute] = []


def _wait_for(page: Page, predicate: Callable[[], bool], *, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"condition not met within {timeout_s:.0f}s")


def _mock_terminal_attach(page: Page, session_id: str) -> _TerminalMock:
    terminal = _TerminalMock()

    def session_snapshot(route: Route) -> None:
        response = fetch_with_retry(route)
        payload = response.json()
        rows = payload.get("data", [payload])
        for row in rows:
            if row.get("id") == session_id:
                row.update(runner_id="runner_scrollbar_mock", runner_online=True)
        route.fulfill(response=response, json=payload)

    def terminal_inventory(route: Route) -> None:
        terminal.inventory_requests += 1
        route.fulfill(
            json={
                "object": "list",
                "data": [
                    {
                        "id": _TERMINAL_ID,
                        "object": "terminal",
                        "name": "codex",
                        "metadata": {
                            "terminal_name": "codex",
                            "session_key": "main",
                            "running": True,
                        },
                    }
                ],
                "has_more": False,
            }
        )

    def attach(ws: WebSocketRoute) -> None:
        terminal.sockets.append(ws)
        ws.on_message(lambda _message: None)

    escaped_id = re.escape(session_id)
    page.route(re.compile(rf"/v1/sessions(?:/{escaped_id})?(?:\?|$)"), session_snapshot)
    page.route(
        re.compile(rf"/v1/sessions/{escaped_id}/resources/terminals(?:\?|$)"),
        terminal_inventory,
    )
    page.route(
        "**/health?session_ids=*",
        lambda route: route.fulfill(
            json={"sessions": {session_id: {"runner_online": True, "host_online": None}}}
        ),
    )
    page.route_web_socket(
        re.compile(rf"/v1/sessions/{escaped_id}/resources/terminals/{_TERMINAL_ID}/attach"),
        attach,
    )
    # The fake online PTY must not be contradicted by the unbound server row.
    page.route_web_socket("**/v1/sessions/updates*", lambda ws: ws.on_message(lambda _msg: None))
    return terminal


def _select_view(page: Page, view: str) -> None:
    segment = page.get_by_test_id(f"view-mode-{view}")
    if segment.is_visible():
        segment.click()
    else:
        page.get_by_role("button", name=re.compile(r"^(Session|Conversation) actions$")).filter(
            visible=True
        ).click()
        page.get_by_test_id(f"view-mode-menu-{view}").click()
        expect(page.get_by_role("menu")).to_have_count(0)


def _open_from_sidebar(page: Page, session_id: str) -> None:
    sidebar = page.locator("aside[aria-label='Conversations']")
    if sidebar.get_attribute("aria-hidden") == "true":
        page.get_by_role("button", name="Open sidebar", exact=True).click()
    sidebar.locator(f'a[href="/c/{session_id}"]').click()
    expect(page).to_have_url(re.compile(rf"/c/{re.escape(session_id)}(?:[?#]|$)"))


def _attach_socket_states(page: Page) -> list[int]:
    return page.evaluate("() => window.__terminalAttachSockets.map(socket => socket.readyState)")


def _emit_output(page: Page, surface: Locator, socket: WebSocketRoute) -> None:
    previous_height = surface.locator(_SLIDER).evaluate("el => el.getBoundingClientRect().height")
    socket.send("".join(f"Terminal line {n:03d}\r\n" for n in range(96)).encode())
    expect(surface.locator(f"{_BAR}.visible")).to_be_attached(timeout=10_000)
    page.wait_for_function(
        "({el, previous}) => el.getBoundingClientRect().height > 0 && "
        "el.getBoundingClientRect().height !== previous && "
        "Number(getComputedStyle(el.parentElement).opacity) > 0.99",
        arg={"el": surface.locator(_SLIDER).element_handle(), "previous": previous_height},
        timeout=10_000,
    )


@pytest.mark.parametrize("mobile", [True, False], ids=["mobile", "desktop"])
def test_terminal_attaches_only_while_terminal_view_is_shown(
    browser: Browser,
    terminal_scrollbar_session: tuple[str, str, str],
    mobile: bool,
) -> None:
    """Chat view holds no attach; each Terminal visit attaches and leaving it releases it."""
    if mobile and browser.browser_type.name == "firefox":
        pytest.skip("Playwright Firefox does not support mobile contexts")
    base_url, session_id, other_session_id = terminal_scrollbar_session
    context = browser.new_context(
        viewport={"width": 402, "height": 714} if mobile else {"width": 1400, "height": 900},
        is_mobile=mobile,
        has_touch=mobile,
        color_scheme="light",
    )
    page = context.new_page()
    terminal = _mock_terminal_attach(page, session_id)
    try:
        page.goto(f"{base_url}/c/{session_id}")
        composer = page.get_by_label("Message the agent")
        expect(composer).to_be_visible(timeout=30_000)
        expect(page.get_by_test_id("transcript-scrollbar-thumb")).to_be_visible()
        _wait_for(page, lambda: terminal.inventory_requests > 0)
        page.wait_for_timeout(_CHAT_VIEW_HOLD_MS)
        surface = page.get_by_test_id("main-terminal-view")
        expect(surface).to_have_count(0)
        assert terminal.sockets == [], "Chat view opened a terminal attach"

        page.evaluate(_TRACK_ATTACH_SOCKETS)
        _select_view(page, "terminal")
        expect(surface.get_by_test_id("terminal-view")).to_have_attribute(
            "data-state", "connected", timeout=20_000
        )
        assert len(terminal.sockets) == 1
        _emit_output(page, surface, terminal.sockets[0])
        slider = surface.locator(_SLIDER)
        assert slider.evaluate(_PAINT)["opacity"] > 0.99
        before_drag = slider.bounding_box()
        assert before_drag is not None
        x = before_drag["x"] + before_drag["width"] / 2
        y = before_drag["y"] + before_drag["height"] / 2
        assert page.evaluate(
            "({x, y}) => !!document.elementFromPoint(x, y)?.closest('.scrollbar .slider')",
            {"x": x, "y": y},
        ), "The terminal scrollbar must have a reachable drag target"
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x, y - 100, steps=10)
        page.mouse.up()
        page.wait_for_function(
            "({el, previous}) => el.getBoundingClientRect().y < previous - 30",
            arg={"el": slider.element_handle(), "previous": before_drag["y"]},
        )

        # Unmounting the surface disposes its socket; Chat view must not dial again.
        _select_view(page, "chat")
        expect(composer).to_be_visible()
        expect(surface).to_have_count(0)
        expect(page.get_by_test_id("terminal-view")).to_have_count(0)
        page.wait_for_timeout(_CHAT_VIEW_HOLD_MS)
        assert len(terminal.sockets) == 1, "Chat view re-dialed the terminal attach"
        assert _attach_socket_states(page) == [_WEBSOCKET_CLOSED], (
            "Chat view kept the terminal attach open"
        )
        expect(page.get_by_test_id("transcript-scrollbar-thumb")).to_be_visible()

        _select_view(page, "terminal")
        expect(surface.get_by_test_id("terminal-view")).to_have_attribute(
            "data-state", "connected", timeout=20_000
        )
        assert len(terminal.sockets) == 2, "Terminal view did not attach again"

        _open_from_sidebar(page, other_session_id)
        expect(surface).to_have_count(0)
        page.wait_for_timeout(_CHAT_VIEW_HOLD_MS)
        assert len(terminal.sockets) == 2
        assert _attach_socket_states(page) == [_WEBSOCKET_CLOSED] * 2, (
            "Switching sessions kept the terminal attach open"
        )
    finally:
        context.close()
