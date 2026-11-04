"""Browser e2e: Chat view must not hold a terminal attach to the session's tmux."""

from __future__ import annotations

import time
from collections import Counter

import httpx
import pytest
from playwright.sync_api import Page, WebSocket, expect

from tests._helpers.polling import poll_until
from tests.e2e_ui.messages.test_message_render_parity import _select_view_mode

_TERMINAL_READY_TIMEOUT_S = 120
# Long enough to catch an attach dialed after the terminal inventory loads.
_CHAT_VIEW_HOLD_MS = 10_000
_SHOWN_TERMINAL = (
    '[data-testid="main-terminal-view"][data-visible="true"] [data-testid="terminal-view"]'
)


def _agent_terminal_running(base_url: str, session_id: str) -> bool:
    response = httpx.get(f"{base_url}/v1/sessions/{session_id}/resources", timeout=10.0)
    response.raise_for_status()
    return any(
        row.get("type") == "terminal" and (row.get("metadata") or {}).get("running")
        for row in response.json().get("data", [])
    )


class _AttachSockets:
    def __init__(self, page: Page, session_id: str) -> None:
        self.path_marker = f"/sessions/{session_id}/resources/terminals/"
        self.opened: list[tuple[float, str]] = []
        self.open_counts: Counter[str] = Counter()
        page.on("websocket", self._on_websocket)

    def _on_websocket(self, socket: WebSocket) -> None:
        url = socket.url.split("?", 1)[0]
        if self.path_marker not in url or not url.endswith("/attach"):
            return
        self.opened.append((time.monotonic(), url))
        self.open_counts[url] += 1

        def _on_close(_socket: WebSocket) -> None:
            self.open_counts[url] -= 1

        socket.on("close", _on_close)

    def opened_since(self, start: float) -> list[str]:
        return [url for at, url in self.opened if at >= start]

    def still_open(self) -> list[str]:
        return sorted(url for url, count in self.open_counts.items() if count > 0)


def test_chat_view_does_not_attach_or_hold_the_agent_terminal(
    request: pytest.FixtureRequest,
    native_claude_mock_session: tuple[str, str],
) -> None:
    base_url, session_id = native_claude_mock_session
    poll_until(
        lambda: _agent_terminal_running(base_url, session_id),
        timeout_s=_TERMINAL_READY_TIMEOUT_S,
        interval_s=1.0,
    )

    page: Page = request.getfixturevalue("page")
    sockets = _AttachSockets(page, session_id)
    chat_view_start = time.monotonic()
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_test_id("view-mode-terminal")).to_be_enabled(timeout=60_000)
    expect(page.get_by_test_id("view-mode-chat")).to_have_attribute("aria-pressed", "true")
    page.wait_for_timeout(_CHAT_VIEW_HOLD_MS)
    opened_in_chat_view = sockets.opened_since(chat_view_start)

    _select_view_mode(page, "Terminal")
    expect(page.locator(_SHOWN_TERMINAL).first).to_have_attribute(
        "data-state", "connected", timeout=60_000
    )

    _select_view_mode(page, "Chat")
    expect(page.get_by_test_id("view-mode-chat")).to_have_attribute("aria-pressed", "true")
    page.wait_for_timeout(_CHAT_VIEW_HOLD_MS)
    still_open_after_chat = sockets.still_open()

    problems = []
    if opened_in_chat_view:
        problems.append(f"Chat view opened terminal attaches: {opened_in_chat_view}")
    if still_open_after_chat:
        problems.append(f"Attaches still open after returning to Chat: {still_open_after_chat}")
    assert not problems, "\n".join(problems)
