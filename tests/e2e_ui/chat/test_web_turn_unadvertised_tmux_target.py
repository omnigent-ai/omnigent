"""Web-chat delivery restores a missing advertisement for its live Claude pane."""

from __future__ import annotations

import logging
import time

import pytest
from playwright.sync_api import Page

from omnigent.harnesses.claude_native.bridge import (
    _BRIDGE_ROOT,
    _TMUX_FILE,
    read_active_session_id,
)
from tests.e2e_ui.conftest import configure_mock_llm, set_fallback_mock_llm

_log = logging.getLogger(__name__)

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_TERMINAL_VIEW = '[data-testid="terminal-view"]'
_ERROR_PILL = '[data-testid="error-pill"]'
_NOT_ADVERTISED = "not advertised"
_ECHO_TOKEN = "advertok"

# The pane's tmux target must advertise within this long after connect. A
# healthy runner writes tmux.json within ~1s of the pane launching.
_TERMINAL_READY_TIMEOUT_MS = 120_000
# Generous ceiling for the turn to be delivered post-fix: the pane is alive, so
# once the target is re-advertised Claude injects and replies. Well past the
# 30s inject wait that gates the buggy hard-fail.
_DELIVERY_TIMEOUT_S = 200.0


def _wait_terminal_connected(page: Page, timeout_ms: int) -> None:
    """Wait until the session's terminal pane reports ``connected``.

    Native sessions default to the terminal view, but the ``terminal-view``
    element stays mounted (hidden) after switching to chat, so this polls its
    ``data-state`` attribute directly rather than waiting on visibility.

    :param page: The Playwright page on the session surface.
    :param timeout_ms: Milliseconds to wait for the connected state.
    """
    page.get_by_test_id("view-mode-toggle").wait_for(state="visible", timeout=timeout_ms)
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        state = page.locator(_TERMINAL_VIEW).last.get_attribute("data-state")
        if state == "connected":
            return
        page.wait_for_timeout(500)
    raise AssertionError(f"terminal never reached 'connected' within {timeout_ms}ms")


def _remove_tmux_advertisement(session_id: str) -> str:
    """Remove the advertisement owned by the fixture's session.

    :param session_id: Session whose pane should lose its advertisement.
    :returns: The path of the removed advertisement.
    """
    matches = [
        p
        for p in _BRIDGE_ROOT.glob(f"*/{_TMUX_FILE}")
        if read_active_session_id(p.parent) == session_id
    ]
    assert len(matches) == 1, "expected exactly one advertisement for the fixture session"
    target = matches[0]
    target.unlink()
    assert not target.exists()
    return str(target)


def _not_advertised_error_text(page: Page) -> str | None:
    """Return the error-pill's message if it is the tmux-not-advertised failure.

    The pill renders its detailed message only when expanded, so this expands it
    before reading ``error-message-content``.

    :param page: The Playwright page on the session surface.
    :returns: The message text if it names the not-advertised failure, else None.
    """
    pill = page.locator(_ERROR_PILL).first
    if pill.count() == 0:
        return None
    pill.click()
    try:
        content = page.get_by_test_id("error-message-content").first
        content.wait_for(state="visible", timeout=10_000)
        text = content.inner_text()
    except Exception:  # an unreadable body means the pill isn't ready yet
        return None
    return text if _NOT_ADVERTISED in text.lower() else None


@pytest.mark.timeout(300)
def test_web_turn_survives_unadvertised_tmux_target(
    page: Page,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A web turn must not hard-fail when the tmux target is momentarily absent.

    With the pane alive but its ``tmux.json`` advertisement missing, delivering
    a web-chat turn must re-establish the target and deliver the message --
    never surface "Claude terminal tmux target is not advertised yet".
    """
    base_url, session_id = native_claude_mock_session
    configure_mock_llm(
        mock_llm_server_url,
        [],
        key="unadvertised-tmux",
        match=_ECHO_TOKEN,
    )
    # Native background requests can consume a queued reply before the visible turn.
    set_fallback_mock_llm(mock_llm_server_url, "unadvertised-tmux", _ECHO_TOKEN)
    _log.info("session ready base=%s id=%s", base_url, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    # Bring the terminal fully up so tmux.json has been written and the pane is
    # alive before removing this session's advertisement.
    _wait_terminal_connected(page, _TERMINAL_READY_TIMEOUT_MS)

    # Inject the fault: the tmux target is no longer advertised, pane still alive.
    removed = _remove_tmux_advertisement(session_id)
    _log.info("removed tmux advertisement: %s", removed)

    # Switch to the chat composer and send a web-chat turn (the user's action).
    if page.get_by_test_id("view-mode-toggle").count() > 0:
        segment = page.get_by_test_id("view-mode-chat")
        segment.wait_for(state="visible", timeout=30_000)
        segment.click()
    composer = page.get_by_placeholder(_COMPOSER)
    composer.wait_for(state="visible", timeout=30_000)
    composer.fill(f"Reply with exactly this token and nothing else: {_ECHO_TOKEN}")
    page.get_by_role("button", name="Send", exact=True).click()
    t_send = time.monotonic()
    _log.info("sent web-chat turn")

    # The turn must be delivered (assistant reply) and must NOT hard-fail with
    # the tmux-not-advertised error. On the buggy build the error pill appears
    # ~30s after send (the inject wait) -> we fail here specifically on it.
    deadline = time.monotonic() + _DELIVERY_TIMEOUT_S
    while time.monotonic() < deadline:
        message = _not_advertised_error_text(page)
        if message is not None:
            pytest.fail(
                "web-chat turn hard-failed "
                f"{time.monotonic() - t_send:.0f}s after send because the tmux "
                f"target was not advertised -- {message!r}"
            )
        if page.locator(_ASSISTANT).filter(has_text=_ECHO_TOKEN).count() > 0:
            _log.info("turn delivered %.0fs after send", time.monotonic() - t_send)
            return
        page.wait_for_timeout(500)

    raise AssertionError(
        f"web-chat turn was neither delivered nor failed within {_DELIVERY_TIMEOUT_S:.0f}s"
    )
