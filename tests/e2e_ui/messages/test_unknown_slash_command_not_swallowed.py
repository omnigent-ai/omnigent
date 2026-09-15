r"""UI journey: an unrecognized slash command sent from the web composer.

A claude-native session is terminal-first: a web-composer message is injected
into the real Claude Code TUI by the native bridge
(``omnigent/claude_native_bridge.py``). The bridge escapes *known* built-in
commands it cannot drive (``/help``, ``/exit``, ...) but passes any *unknown*
``/name`` through verbatim on the assumption it is a skill. Claude Code
rejects a slash command it does not recognize ("Unknown command: /<name>")
and drops the input without ever calling the model, so the user's message is
silently swallowed: no turn, no assistant reply.

This test sends ``/definitely-not-a-real-command <marker>`` from the web
composer and asserts the turn completes with the mock assistant's reply. On a
build where the bridge recovers from the rejection (re-delivering the text
escaped), the message reaches the model as plain user text and the reply
renders; on the buggy build Claude Code eats the message and the assertion
times out with no assistant bubble.
"""

from __future__ import annotations

import uuid

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import reset_mock_llm, set_fallback_mock_llm

from .test_message_render_parity import _ASSISTANT, _WORKING, _ensure_chat_view, _send
from .test_native_claude_render_parity import (
    _CLAUDE_MOCK_MODEL,
    _MOCK_TURN_TIMEOUT_MS,
    _open_terminal_view,
    _wait_terminal_connected,
)

# A slash command that is neither a Claude Code built-in, an Omnigent-allowed
# command, nor a plausible skill name -- Claude Code cannot recognize it.
_UNKNOWN_COMMAND = "/definitely-not-a-real-command"


@pytest.mark.nightly
@pytest.mark.timeout(300)
def test_unknown_slash_command_is_not_swallowed(
    page: Page,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A web-composer message starting with an unknown ``/command`` still gets a reply."""
    base_url, session_id = native_claude_mock_session
    page.goto(f"{base_url}/c/{session_id}")

    # Wait for the live Claude Code TUI to attach before sending, exactly
    # like the parity suite: the bridge can only inject into a booted TUI.
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)

    nonce = uuid.uuid4().hex[:8]
    token = f"ast-{nonce}"
    reset_mock_llm(mock_llm_server_url)
    set_fallback_mock_llm(mock_llm_server_url, "default", token)
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, token)

    # The body doubles as an echo instruction so the test also passes against
    # a real LLM backend (the fixture uses one when LLM_API_KEY is set).
    _send(
        page,
        f"{_UNKNOWN_COMMAND} Reply with exactly this token and nothing else: {token}",
    )

    # Peek at the Terminal view like a user checking why nothing happened.
    # On the buggy build the TUI shows "Unknown command:
    # /definitely-not-a-real-command"; after a fix it shows a normal turn.
    page.wait_for_timeout(6_000)
    _open_terminal_view(page)
    page.wait_for_timeout(4_000)
    _ensure_chat_view(page)

    # The message must not be silently swallowed: Claude Code has to receive
    # it as plain text (escaped), call the model, and the reply must render.
    # On the buggy build Claude Code rejects the command, never calls the
    # model, and this times out with no assistant bubble.
    expect(page.locator(_ASSISTANT, has_text=token).first).to_be_visible(
        timeout=_MOCK_TURN_TIMEOUT_MS
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_MOCK_TURN_TIMEOUT_MS)
