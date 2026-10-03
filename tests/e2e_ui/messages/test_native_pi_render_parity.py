"""Film real Pi in chat and Terminal, with a scripted model and no live login.

Both directions use the installed CLI and its native extension: web composer
into Pi, then a prompt typed into Pi's TUI back into the web transcript.
"""

from __future__ import annotations

import re
import time
import uuid
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm, reset_mock_llm, set_fallback_mock_llm
from tests.helpers.ui_configuration import _CLAUDE_MOCK_MODEL

from .test_message_render_parity import (
    _ASSISTANT,
    _USER,
    _WORKING,
    _assert_no_duplicate_render,
    _assert_transcript_parity,
    _ensure_chat_view,
    _select_view_mode,
    _send,
)


def _terminal(page: Page):
    _select_view_mode(page, "Terminal")
    terminal = page.get_by_test_id("terminal-view").last
    expect(terminal).to_be_visible()
    expect(terminal).to_have_attribute("data-state", "connected", timeout=120_000)
    expect(terminal.locator(".xterm-screen")).to_be_visible()
    return terminal


@pytest.mark.nightly
@pytest.mark.timeout(300)
def test_native_pi_message_render_parity(
    page: Page,
    native_pi_mock_session: tuple[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """Show a composer turn and a TUI turn, then verify the mirrored replies."""
    base_url, session_id = native_pi_mock_session
    nonce = uuid.uuid4().hex[:8]
    markers = [f"pi-web-{nonce}", f"pi-tui-{nonce}"]
    replies = [f"PI-WEB-REPLY-{nonce}", f"PI-TUI-REPLY-{nonce}"]
    reset_mock_llm(mock_llm_server_url)
    for marker, reply in zip(markers, replies, strict=True):
        configure_mock_llm(
            mock_llm_server_url,
            [{"text": reply}],
            key=marker,
            match=marker,
            required_tools=["bash"],
        )
    set_fallback_mock_llm(mock_llm_server_url, "default", "")
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, "")
    terminal_frames: list[str] = []

    def observe_socket(socket):
        if "/terminals/" in socket.url:
            socket.on(
                "framereceived",
                lambda data: terminal_frames.append(
                    data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
                ),
            )

    def terminal_text() -> str:
        # Pi can insert ANSI styling or line wraps inside a streamed reply.
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", "".join(terminal_frames))
        return text.replace("\r", "").replace("\n", "")

    page.on("websocket", observe_socket)
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(timeout=120_000)
    _terminal(page)

    for index, (marker, reply) in enumerate(zip(markers, replies, strict=True)):
        if index == 0:
            _ensure_chat_view(page)
            _send(page, marker)
        else:
            terminal = _terminal(page)
            terminal.locator(".xterm-helper-textarea").focus()
            page.keyboard.type(marker, delay=30)
            page.keyboard.press("Enter")
            deadline = time.monotonic() + 60
            while reply not in terminal_text() and time.monotonic() < deadline:
                page.wait_for_timeout(100)
            assert reply in terminal_text(), "Pi reply never reached the terminal stream"
            # Hold the observed outcome long enough to inspect in the recording.
            page.wait_for_timeout(1000)
            page.screenshot(path=str(tmp_path / "pi-terminal-reply.png"))
        _ensure_chat_view(page)
        expect(page.locator(_ASSISTANT, has_text=reply)).to_be_visible(timeout=60_000)
        expect(page.locator(_USER, has_text=marker)).to_be_visible(timeout=30_000)
        expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)

    _assert_no_duplicate_render(page, markers, replies)
    _assert_transcript_parity(base_url, session_id, markers, replies)
    # Keep the verified Chat outcome visible at the end of the clip.
    page.wait_for_timeout(1000)
