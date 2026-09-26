"""Clipboard pastes must remain drafts until an explicit send gesture.

These browser journeys exercise the real clipboard in the chat composer and
the embedded Claude terminal. The TUI journey asserts through the chat
transcript — a TUI submission mirrors into it as a user message — so the
check needs no direct access to the runner's tmux socket."""

from __future__ import annotations

import shutil
import sys
import uuid
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Locator, Page, Request, expect

from tests.e2e_ui.conftest import reset_mock_llm, set_fallback_mock_llm
from tests.e2e_ui.messages.test_message_render_parity import (
    _USER,
    _WORKING,
    _ensure_chat_view,
    _select_view_mode,
)
from tests.e2e_ui.messages.test_native_claude_render_parity import (
    _CLAUDE_MOCK_MODEL,
    _TERMINAL_VIEW,
    _XTERM_INPUT,
    _open_terminal_view,
    _wait_terminal_connected,
)

_COMPOSER_LABEL = "Message the agent"

# Budget for a TUI submission to mirror into the chat transcript (browser ->
# WebSocket -> tmux -> Claude Code -> bridge -> chat) with the mock LLM
# replying instantly.
_MIRROR_TIMEOUT_MS = 60_000
# How long a wrongly auto-submitted paste gets to run its turn and mirror
# into the transcript before the no-send judgment.
_SUBMIT_SETTLE_MS = 10_000


def _focus_tui_input(page: Page) -> Locator:
    """Focus the embedded xterm's hidden helper textarea and return it."""
    xterm_input = page.locator(_TERMINAL_VIEW).last.locator(_XTERM_INPUT)
    expect(xterm_input).to_be_attached(timeout=30_000)
    xterm_input.focus()
    return xterm_input


@pytest.mark.nightly
@pytest.mark.timeout(300)
@pytest.mark.skipif(
    shutil.which("claude") is None or shutil.which("tmux") is None,
    reason="requires the claude CLI and tmux",
)
def test_tui_multiline_paste_stays_unsubmitted(
    page: Page,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A multi-line paste into the TUI pane must wait for Enter, not self-send."""
    base_url, session_id = native_claude_mock_session
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)

    # A wrongly-submitted turn should resolve fast against the mock LLM rather
    # than leave the TUI mid-request while the transcript is inspected.
    reset_mock_llm(mock_llm_server_url)
    set_fallback_mock_llm(mock_llm_server_url, "default", "paste-turn-token")
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, "paste-turn-token")

    _focus_tui_input(page)
    page.wait_for_timeout(2_000)

    # Prove the keystroke -> TUI -> chat-mirror path works before trusting the
    # paste result; a paste that never lands would otherwise pass vacuously.
    nonce = uuid.uuid4().hex[:6]
    sanity = f"sanity-{nonce}"
    page.keyboard.type(sanity, delay=30)
    page.keyboard.press("Enter")
    _ensure_chat_view(page)
    expect(page.locator(_USER, has_text=sanity).first).to_be_visible(timeout=_MIRROR_TIMEOUT_MS)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_MIRROR_TIMEOUT_MS)

    # The real user journey: put a two-line block on the clipboard and paste it
    # with the browser's paste gesture (plain Ctrl+V is the terminal's literal
    # ^V byte, so terminals paste via Ctrl+Shift+V / Cmd+V).
    _select_view_mode(page, "Terminal")
    _wait_terminal_connected(page)
    tui_input = _focus_tui_input(page)
    page.wait_for_timeout(1_000)
    first = f"pasteblock-first-{nonce}"
    second = f"pasteblock-second-{nonce}"
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    page.evaluate("([a, b]) => navigator.clipboard.writeText(a + '\\n' + b)", [first, second])
    tui_input.press("Meta+V" if sys.platform == "darwin" else "Control+Shift+V")

    # Give a premature submission time to run its turn and mirror into chat.
    page.wait_for_timeout(_SUBMIT_SETTLE_MS)
    _ensure_chat_view(page)

    # The regression: with the paste treated as raw keystrokes, Claude Code
    # submits the first line immediately — it mirrors into the transcript as
    # a sent user message although Enter was never pressed.
    assert page.locator(_USER, has_text=first).count() == 0, (
        "pasting a multi-line block submitted its first line to the agent without Enter"
    )

    # An explicit Enter must send the whole block as ONE message; this also
    # proves the paste really landed in the TUI composer (had the first line
    # auto-submitted, the message sent here would carry only the second).
    _select_view_mode(page, "Terminal")
    _wait_terminal_connected(page)
    _focus_tui_input(page)
    page.wait_for_timeout(500)
    page.keyboard.press("Enter")
    _ensure_chat_view(page)
    block_message = page.locator(_USER, has_text=second).first
    expect(block_message).to_be_visible(timeout=_MIRROR_TIMEOUT_MS)
    expect(block_message).to_contain_text(first)


def test_composer_multiline_paste_stays_in_composer(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A multi-line paste into the chat composer inserts a draft, never sends."""
    base_url, session_id = seeded_session
    posts: list[str] = []

    def record(request: Request) -> None:
        if request.method != "POST":
            return
        if urlparse(request.url).path != f"/v1/sessions/{session_id}/events":
            return
        body = request.post_data_json
        if isinstance(body, dict) and body.get("type") == "message":
            posts.append(str(body))

    page.on("request", record)

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label(_COMPOSER_LABEL)
    expect(composer).to_be_visible(timeout=30_000)
    composer.click()

    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    page.evaluate(
        "navigator.clipboard.writeText('composer paste line one\\ncomposer paste line two')"
    )
    composer.press("ControlOrMeta+V")

    expect(composer).to_have_value("composer paste line one\ncomposer paste line two")
    page.wait_for_timeout(1_000)
    assert posts == [], f"pasting into the composer sent a message: {posts}"
