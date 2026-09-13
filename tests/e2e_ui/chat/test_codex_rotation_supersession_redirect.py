r"""UI journey: a Codex ``/new`` rotation must notify the old web conversation.

The user path: a native ``codex-native`` session is open in the browser; the
user runs ``/new`` in the embedded Codex TUI. Omnigent rotates the session
binding onto a fresh conversation and transfers the terminal to it -- but on
buggy ``main`` it never tells the OLD conversation. So the browser, still on the
old conversation, is stranded: it does not auto-redirect to the new chat and no
notice message linking there appears. ``claude-native`` posts the supersession
notice (``_post_clear_supersession``); codex/antigravity do not.

This is the web-surface half of the reproduction (the harness-level producer
gap is pinned by ``tests/e2e/test_native_rotation_supersession_notice_e2e.py``).
It drives the REAL Codex TUI in the SPA's terminal pane against the mock LLM
(the ``native_codex_mock_session`` fixture -- no vendor login), runs a turn, then
types ``/new`` into the pane and asserts the open browser follows the redirect the
server emits from ``external_session_superseded`` (proven end-to-end by
``tests/e2e_ui/chat/test_session_superseded_redirect.py``). On buggy ``main`` the
redirect never fires, so this FAILS: the browser stays on ``/c/<old>`` (observed
sitting on ``/c/<old>?view=chat`` for the full redirect window). It passes once
the fix posts the supersession notice from the codex rotation path.
"""

from __future__ import annotations

import logging
import re
import uuid

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm, reset_mock_llm, set_fallback_mock_llm

from ..messages.test_message_render_parity import (
    _ASSISTANT,
    _ensure_chat_view,
    _select_view_mode,
    _send,
    _turn_prompt,
)

_log = logging.getLogger(__name__)

_TERMINAL_VIEW = '[data-testid="terminal-view"]'
_XTERM_INPUT = ".xterm-helper-textarea"
_CODEX_MOCK_MODEL = "gpt-4o"

# Codex boots in the terminal on bind; the auto-launch + first-run pre-accept +
# WS attach can take a while on a cold CI runner.
_TERMINAL_READY_TIMEOUT_MS = 120_000
_MOCK_TURN_TIMEOUT_MS = 60_000
# How long to wait for the post-/new redirect the fix produces. On buggy main it
# never comes; this bound is how long the browser sits stuck before the test
# fails (and the before-footage ends).
_REDIRECT_TIMEOUT_MS = 30_000


def _open_terminal_view(page: Page) -> None:
    """Switch the terminal-first session to its Terminal (TUI) view."""
    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(
        timeout=_TERMINAL_READY_TIMEOUT_MS
    )
    _select_view_mode(page, "Terminal")


def _wait_terminal_connected(page: Page) -> None:
    """Wait until the embedded xterm has attached to the live Codex TUI."""
    terminal = page.locator(_TERMINAL_VIEW).last
    expect(terminal).to_have_attribute(
        "data-state", "connected", timeout=_TERMINAL_READY_TIMEOUT_MS
    )


def _type_into_tui(page: Page, text: str) -> None:
    """Type *text* into the embedded Codex TUI and submit with Enter."""
    xterm_input = page.locator(_TERMINAL_VIEW).last.locator(_XTERM_INPUT)
    expect(xterm_input).to_be_attached(timeout=30_000)
    xterm_input.focus()
    page.keyboard.type(text, delay=15)
    page.keyboard.press("Enter")


@pytest.mark.nightly
@pytest.mark.timeout(360)
def test_codex_new_notifies_and_redirects_old_conversation(
    page: Page,
    native_codex_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Running ``/new`` in the Codex pane must redirect the open old conversation.

    Drives the real journey: open the session, run one turn, then ``/new`` in the
    TUI. Asserts the browser follows the supersession redirect to the new
    conversation. FAILS on buggy main (no redirect / no notice) -- the old web
    view is stranded on ``/c/<old>``.
    """
    base_url, session_id = native_codex_mock_session
    _log.info("native-codex mock session ready: base_url=%s session_id=%s", base_url, session_id)

    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _log.info("Codex TUI attached (terminal-view connected)")

    # One real turn so the old conversation has content to be stranded.
    user_marker = f"usr-{uuid.uuid4().hex[:8]}"
    assistant_token = f"ast-{uuid.uuid4().hex[:8]}"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": assistant_token}],
        key=user_marker,
        match=user_marker,
    )
    set_fallback_mock_llm(mock_llm_server_url, _CODEX_MOCK_MODEL, "")

    _ensure_chat_view(page)
    _send(page, _turn_prompt(1, user_marker, assistant_token))
    expect(page.locator(_ASSISTANT, has_text=assistant_token).first).to_be_visible(
        timeout=_MOCK_TURN_TIMEOUT_MS
    )
    _log.info("first turn settled on the old conversation")

    # Confirm the browser is on the old conversation before /new.
    expect(page).to_have_url(re.compile(rf"/c/{re.escape(session_id)}"), timeout=15_000)

    # The user runs /new in the Codex pane -> Omnigent rotates onto a fresh
    # conversation. The forwarder must notify this (old) conversation.
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _log.info("typing /new into the Codex TUI")
    _type_into_tui(page, "/new")

    # Watch the old conversation from the chat view: the fix redirects it to the
    # NEW conversation (via session.superseded -> the proven redirect in
    # tests/e2e_ui/chat/test_session_superseded_redirect.py). We don't know the
    # rotated id up front, so assert the browser lands on SOME /c/<other> whose id
    # is not the old one. On buggy main the codex rotation path posts no
    # supersession notice, so the browser never leaves /c/<old> and this times
    # out -- the reproduction. (A `$`-anchored not_to_have_url would false-pass on
    # a trailing view query string, so match the new id positively instead.)
    _ensure_chat_view(page)
    redirected = re.compile(rf"/c/(?!{re.escape(session_id)})[0-9a-f]{{6,}}")
    expect(page).to_have_url(redirected, timeout=_REDIRECT_TIMEOUT_MS)
    _log.info("browser followed the supersession redirect to the new conversation: %s", page.url)
