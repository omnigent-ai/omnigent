r"""UI journey: a native Claude Code session renders parity with its TUI (mock LLM).

The native ``claude-native`` ("Claude Code") wrapper is terminal-first: a real
``claude`` CLI runs in the session terminal, the SPA's **Terminal** view
attaches to that live TUI over a WebSocket, and the SPA's **Chat** view renders
the SAME canonical transcript (``GET /v1/sessions/{id}/items``) the TUI prints.
A native bridge forwards web-composer messages INTO the Claude process and
forwards Claude's transcript back OUT as conversation items. This suite asserts
that round-trips both ways and renders exactly once — the three properties the
native forwarder has historically regressed on.

The LLM calls are served by the in-process mock LLM server rather than a real
Anthropic endpoint. Before each test run a mock ``anthropic`` provider config is
written to ``~/.omnigent/config.yaml`` (see ``native_claude_mock_session`` in
``conftest.py``), redirecting the runner's ``ANTHROPIC_BASE_URL`` to the mock
server. Tokens are pre-generated and queued via content-based routing so the
mock returns the expected assistant token for each turn regardless of how many
extra LLM calls Claude Code makes internally.
"""

from __future__ import annotations

import logging
import os
import uuid

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm, reset_mock_llm, set_fallback_mock_llm

# Reuse the custom-agent suite's helpers — both surfaces render from the same
# canonical transcript, so parity / dedup / ordering are asserted identically.
from .test_message_render_parity import (
    _ASSISTANT,
    _USER,
    _WORKING,
    _assert_no_duplicate_render,
    _assert_transcript_parity,
    _ensure_chat_view,
    _send,
    _turn_prompt,
)

_log = logging.getLogger(__name__)

_TERMINAL_VIEW = '[data-testid="terminal-view"]'
# xterm.js routes all keystrokes through a hidden helper <textarea>; focusing it
# and typing is how a user (and Playwright) drives the embedded TUI.
_XTERM_INPUT = ".xterm-helper-textarea"

# Mock LLM responds instantly; budget covers native CLI boot + terminal attach.
_MOCK_TURN_TIMEOUT_MS = 60_000
# claude-native auto-launch + first-run pre-accept + WS attach.
_TERMINAL_READY_TIMEOUT_MS = 120_000

# Must match the model set in the mock anthropic provider config written by the
# native_claude_mock_session fixture (conftest._CLAUDE_MOCK_MODEL).
_CLAUDE_MOCK_MODEL = "claude-3-5-sonnet-20241022"

# Two composer turns (the IN direction) + one TUI turn (the OUT direction).
_COMPOSER_TURNS = 2


def _open_terminal_view(page: Page) -> None:
    """Switch a terminal-first session to its Terminal (TUI) view.

    :param page: The Playwright page, on the session's chat surface.
    """
    view_mode = page.get_by_role("group", name="View mode")
    expect(view_mode).to_be_visible(timeout=_TERMINAL_READY_TIMEOUT_MS)
    terminal_button = view_mode.get_by_role("button", name="Terminal")
    expect(terminal_button).to_be_visible(timeout=30_000)
    terminal_button.click()


def _wait_terminal_connected(page: Page) -> None:
    """Wait until the embedded xterm has attached to the live Claude TUI.

    :param page: The Playwright page, on the Terminal view.
    """
    terminal = page.locator(_TERMINAL_VIEW).last
    expect(terminal).to_have_attribute(
        "data-state", "connected", timeout=_TERMINAL_READY_TIMEOUT_MS
    )


def _type_into_tui(page: Page, text: str) -> None:
    """Type *text* into the embedded Claude Code TUI and submit with Enter.

    :param page: The Playwright page, on the connected Terminal view.
    :param text: The single-line prompt to type into the TUI.
    """
    xterm_input = page.locator(_TERMINAL_VIEW).last.locator(_XTERM_INPUT)
    expect(xterm_input).to_be_attached(timeout=30_000)
    xterm_input.focus()
    page.keyboard.type(text, delay=15)
    page.keyboard.press("Enter")


@pytest.mark.skipif(
    not os.environ.get("LLM_API_KEY"),
    reason="Native Claude render-parity needs real Anthropic credentials (LLM_API_KEY).",
)
@pytest.mark.timeout(300)
def test_native_claude_message_render_parity(
    page: Page,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Native Claude Code renders parity with its TUI, both ways, with no dupes (mock LLM)."""
    base_url, session_id = native_claude_mock_session
    _log.info("native-claude mock session ready: base_url=%s session_id=%s", base_url, session_id)

    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _log.info("Claude Code TUI attached (terminal-view connected)")
    _ensure_chat_view(page)

    # Pre-generate tokens for all turns so they can be queued in the mock
    # before any message is sent. Content-based routing (match=user_marker)
    # ensures the right token is returned regardless of extra internal calls.
    nonces = [uuid.uuid4().hex[:8] for _ in range(_COMPOSER_TURNS + 1)]
    turns = [
        (f"usr-{i + 1}-{nonces[i]}", f"ast-{i + 1}-{nonces[i]}")
        for i in range(_COMPOSER_TURNS + 1)
    ]
    reset_mock_llm(mock_llm_server_url)
    for user_marker, assistant_token in turns:
        configure_mock_llm(
            mock_llm_server_url,
            [{"text": assistant_token}],
            key=user_marker,
            match=user_marker,
        )
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, "")

    user_markers: list[str] = []
    assistant_tokens: list[str] = []

    # --- Property 1 & 3: composer turns (IN) render parity, no dupes. ---
    for index, (user_marker, assistant_token) in enumerate(turns[:_COMPOSER_TURNS], start=1):
        user_markers.append(user_marker)
        assistant_tokens.append(assistant_token)
        _log.info(
            "composer turn %d: sending (marker=%s token=%s)", index, user_marker, assistant_token
        )
        _send(page, _turn_prompt(index, user_marker, assistant_token))
        expect(page.locator(_ASSISTANT, has_text=assistant_token).first).to_be_visible(
            timeout=_MOCK_TURN_TIMEOUT_MS
        )
        expect(page.locator(_WORKING)).to_have_count(0, timeout=_MOCK_TURN_TIMEOUT_MS)
        expect(page.locator(_USER)).to_have_count(index, timeout=30_000)
        _log.info("composer turn %d: settled", index)

    # --- Property 2 & 3: a TUI-originated turn (OUT) surfaces in the web UI. ---
    tui_index = _COMPOSER_TURNS + 1
    tui_marker, tui_token = turns[_COMPOSER_TURNS]
    user_markers.append(tui_marker)
    assistant_tokens.append(tui_token)
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _log.info(
        "TUI turn %d: typing into xterm (marker=%s token=%s)", tui_index, tui_marker, tui_token
    )
    _type_into_tui(page, _turn_prompt(tui_index, tui_marker, tui_token))

    _ensure_chat_view(page)
    expect(page.locator(_ASSISTANT, has_text=tui_token).first).to_be_visible(
        timeout=_MOCK_TURN_TIMEOUT_MS
    )
    expect(page.locator(_USER, has_text=tui_marker).first).to_be_visible(timeout=30_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_MOCK_TURN_TIMEOUT_MS)
    expect(page.locator(_USER)).to_have_count(len(user_markers), timeout=30_000)
    _log.info("TUI turn %d: surfaced in web UI (user + assistant bubbles present)", tui_index)

    # --- Assert all three properties over every turn. ---
    _assert_no_duplicate_render(page, user_markers, assistant_tokens)
    _assert_transcript_parity(base_url, session_id, user_markers, assistant_tokens)
    _log.info("all turns verified: render parity + no-duplicate-render + transcript parity")
