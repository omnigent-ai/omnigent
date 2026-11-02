"""E2E: opening an agent-suggested command from a chat code block in a new shell.

An assistant reply with a ``bash`` fence gets an "Open in shell" button. Clicking
it launches the agent's default shell (desktop: a Workspace rail tab; phone: the
full-screen shell view) and pastes the command at the first prompt without
running it. The command runs only when the user presses Enter.

xterm draws to a canvas, so the test checks the hand-off on the attach WebSocket
(the browser's ``paste`` frame and the bridge's ``paste-result``) and checks
execution through marker files at absolute ``tmp_path`` paths that the
runner-local shell writes.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import configure_mock_llm


def _wait_for(page: Page, condition, timeout_s: float) -> bool:
    """Poll *condition* while letting Playwright deliver WebSocket frame events."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return True
        page.wait_for_timeout(100)
    return bool(condition())


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "mobile"])
def test_open_in_shell_pastes_command_until_user_runs_it(
    request: pytest.FixtureRequest,
    terminal_session: tuple[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
    width: int,
) -> None:
    """Open in shell pastes a two-line command that runs only after Enter."""
    base_url, session_id = terminal_session
    first_marker = tmp_path / "first.ok"
    second_marker = tmp_path / "second.ok"
    command = f"touch {first_marker}\ntouch {second_marker}"
    prompt = f"How do I create the markers? {session_id}"
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": f"Run these in a shell:\n\n```bash\n{command}\n```\n"}],
        match=prompt,
    )

    page: Page = request.getfixturevalue("page")
    page.set_viewport_size({"width": width, "height": 900})
    sent_text: list[str] = []
    received_text: list[str] = []

    def _on_websocket(websocket) -> None:
        if "/attach" not in websocket.url:
            return
        for event, frames in (("framesent", sent_text), ("framereceived", received_text)):
            websocket.on(
                event,
                lambda payload, frames=frames: (
                    frames.append(payload) if isinstance(payload, str) else None
                ),
            )

    page.on("websocket", _on_websocket)
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(prompt)
    page.get_by_role("button", name="Send", exact=True).click()

    code_block = page.locator(".chat-code-block").filter(has_text=str(second_marker))
    expect(code_block).to_be_visible(timeout=60_000)
    code_block.hover()
    code_block.get_by_role("button", name="Open in shell").click()

    if width < 768:
        surface: Locator = page.locator('[data-testid="main-terminal-view"][data-visible="true"]')
    else:
        surface = page.get_by_role("complementary", name="Workspace")
    terminal_view = surface.get_by_test_id("terminal-view").last
    expect(terminal_view).to_be_visible(timeout=60_000)
    expect(terminal_view).to_have_attribute("data-state", "connected", timeout=30_000)

    def _messages(frames: list[str], kind: str) -> list[dict[str, object]]:
        messages = []
        for frame in list(frames):
            try:
                message = json.loads(frame)
            except ValueError:
                continue
            if isinstance(message, dict) and message.get("type") == kind:
                messages.append(message)
        return messages

    assert _wait_for(page, lambda: _messages(received_text, "paste-result"), timeout_s=20.0), (
        f"the bridge never answered the paste; sent frames: {sent_text!r}"
    )
    assert [message["text"] for message in _messages(sent_text, "paste")] == [command]
    assert _messages(received_text, "paste-result") == [
        {"type": "paste-result", "id": 1, "pasted": True}
    ]
    # Bracketed paste leaves the lines in readline's buffer; nothing runs yet.
    page.wait_for_timeout(2_000)
    assert not first_marker.exists() and not second_marker.exists()

    terminal_view.locator("textarea.xterm-helper-textarea").focus()
    page.keyboard.press("Enter")
    assert _wait_for(
        page, lambda: first_marker.exists() and second_marker.exists(), timeout_s=15.0
    )
    expect(terminal_view).to_have_attribute("data-state", "connected")
