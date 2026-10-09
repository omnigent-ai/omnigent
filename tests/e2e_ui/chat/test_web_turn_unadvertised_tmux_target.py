"""Web-chat delivery restores a missing advertisement for its live Claude pane."""

from __future__ import annotations

import logging
import time

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, expect

from tests._helpers.claude_native_advertisement import remove_tmux_advertisement
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
# Ceiling for delivery: the pane is alive, so once the target is
# re-advertised Claude injects and replies.
_DELIVERY_TIMEOUT_S = 200.0


def _wait_terminal_connected(page: Page, timeout_ms: int) -> None:
    """Wait until the session's terminal pane reports ``connected``.

    The ``terminal-view`` element stays mounted (hidden) after switching to
    chat, so this asserts its ``data-state`` rather than its visibility.

    :param page: The Playwright page on the session surface.
    :param timeout_ms: Milliseconds to wait for the connected state.
    """
    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(timeout=timeout_ms)
    expect(page.locator(_TERMINAL_VIEW).last).to_have_attribute(
        "data-state", "connected", timeout=timeout_ms
    )


def _error_pill_text(page: Page) -> str | None:
    """Return the first error pill's message, expanding the pill when collapsed.

    :param page: The Playwright page on the session surface.
    :returns: The pill's message text, or None when no pill is readable yet.
    """
    pill = page.locator(_ERROR_PILL).first
    if pill.count() == 0:
        return None
    content = pill.get_by_test_id("error-message-content")
    try:
        if not content.is_visible():
            pill.click()  # a click toggles the pill, so expand only a collapsed one
        content.wait_for(state="visible", timeout=10_000)
        return content.inner_text()
    except PlaywrightError as exc:  # pill detached or not expanded yet
        _log.debug("error pill not readable yet: %s", exc)
        return None


@pytest.mark.timeout(420)
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
    removed = remove_tmux_advertisement(session_id)
    _log.info("removed tmux advertisement: %s", removed)

    # Switch to the chat composer and send a web-chat turn (the user's action).
    segment = page.get_by_test_id("view-mode-chat")
    segment.wait_for(state="visible", timeout=30_000)
    segment.click()
    composer = page.get_by_placeholder(_COMPOSER)
    composer.wait_for(state="visible", timeout=30_000)
    composer.fill(f"Reply with exactly this token and nothing else: {_ECHO_TOKEN}")
    page.get_by_role("button", name="Send", exact=True).click()
    t_send = time.monotonic()
    _log.info("sent web-chat turn")

    # Fail fast on the not-advertised error pill; otherwise wait for delivery.
    deadline = time.monotonic() + _DELIVERY_TIMEOUT_S
    last_pill_text: str | None = None
    while time.monotonic() < deadline:
        message = _error_pill_text(page)
        if message is not None:
            last_pill_text = message
            if _NOT_ADVERTISED in message.lower():
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
        f" (last error pill text: {last_pill_text!r})"
    )
