"""E2E regression: web-chat delivery must survive an unadvertised Claude tmux target.

A web-chat turn to a ``claude-native`` ("Claude Code") session is delivered by
injecting into the session's tmux pane. Before injecting, the harness bridge
waits (``_wait_for_tmux_info``, up to ``_TMUX_READY_TIMEOUT_S`` = 30s) for the
runner to advertise the pane's tmux target by writing ``tmux.json`` into the
bridge directory. If that advertisement is not present when the turn injects,
the wait times out and the turn hard-fails with:

    inner executor error: Claude terminal tmux target is not advertised yet.
    Wait for the terminal to launch before sending messages from the web UI.

In production this fired when the runner was starved enough that the
``tmux.json`` write lagged past the 30s inject wait while the pane was already
registered and alive -- so the turn-time self-heal
(``_ensure_native_terminal_for_turn``) saw a live pane, returned early WITHOUT
re-establishing the advertisement, and the inject raced an absent target.

This test reproduces that read-side condition deterministically and faithfully:
it brings the terminal fully up, then removes the ``tmux.json`` advertisement
while leaving the pane alive (exactly the "pane alive, target not advertised"
state the starved runner produced), and sends a web turn. The pane is alive, so
the turn *can* be delivered once the runner re-advertises the target; a healthy
delivery path must not hard-fail the turn with "tmux target is not advertised".

Assertion direction is fail->pass across the fix:

* On the buggy build the turn hard-fails ~30s after send with the
  "not advertised" error pill -> this test FAILS.
* A fix that re-establishes / waits for the advertisement before injecting
  delivers the turn -> this test PASSES.
"""

from __future__ import annotations

import logging
import time

import pytest
from playwright.sync_api import Page

from omnigent.harnesses.claude_native.bridge import _BRIDGE_ROOT, _TMUX_FILE

_log = logging.getLogger(__name__)

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_TERMINAL_VIEW = '[data-testid="terminal-view"]'
_ERROR_PILL = '[data-testid="error-pill"]'
_NOT_ADVERTISED = "not advertised"

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


def _remove_tmux_advertisement() -> str:
    """Remove the freshest ``tmux.json`` advertisement under the bridge root.

    The runner (a local subprocess sharing this uid) writes each session's
    ``tmux.json`` under ``/tmp/omnigent-<uid>/claude-native/<digest>/``. The
    active session's file is the most recently written; removing it injects the
    fault -- the tmux target is no longer advertised while the pane stays
    alive -- without touching product code.

    :returns: The path of the removed advertisement.
    """
    recent = [
        p for p in _BRIDGE_ROOT.glob(f"*/{_TMUX_FILE}") if time.time() - p.stat().st_mtime < 600
    ]
    recent.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    assert recent, f"no fresh tmux.json advertisement found under {_BRIDGE_ROOT}"
    target = recent[0]
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
    native_claude_session: tuple[str, str],
) -> None:
    """A web turn must not hard-fail when the tmux target is momentarily absent.

    With the pane alive but its ``tmux.json`` advertisement missing, delivering
    a web-chat turn must re-establish the target and deliver the message --
    never surface "Claude terminal tmux target is not advertised yet".
    """
    base_url, session_id = native_claude_session
    _log.info("session ready base=%s id=%s", base_url, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    # Bring the terminal fully up so tmux.json has been written and the pane is
    # alive -- the exact precondition of the production race.
    _wait_terminal_connected(page, _TERMINAL_READY_TIMEOUT_MS)

    # Inject the fault: the tmux target is no longer advertised, pane still alive.
    removed = _remove_tmux_advertisement()
    _log.info("removed tmux advertisement: %s", removed)

    # Switch to the chat composer and send a web-chat turn (the user's action).
    if page.get_by_test_id("view-mode-toggle").count() > 0:
        segment = page.get_by_test_id("view-mode-chat")
        segment.wait_for(state="visible", timeout=30_000)
        segment.click()
    composer = page.get_by_placeholder(_COMPOSER)
    composer.wait_for(state="visible", timeout=30_000)
    composer.fill("Reply with exactly this token and nothing else: advertok")
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
        if page.locator(_ASSISTANT).count() > 0:
            _log.info("turn delivered %.0fs after send", time.monotonic() - t_send)
            return
        page.wait_for_timeout(500)

    raise AssertionError(
        f"web-chat turn was neither delivered nor failed within {_DELIVERY_TIMEOUT_S:.0f}s"
    )
