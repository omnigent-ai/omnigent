r"""E2E: a web ``/fork`` on claude-native settles instead of being reported lost.

Claude Code records some slash commands — ``/fork``, ``/branch``, and a
``/model`` it rejects — as top-level ``type="system"`` ``local_command``
records rather than ``role=user`` messages. When the transcript reader
skipped those records, the web message that ran the command stayed queued,
and the next message the user sent reported it as never recorded in
Claude's transcript.

The journey drives the real claude-native stack (a live ``claude`` CLI in the
session terminal, fed by the in-process mock LLM): send a bare ``/fork``,
which Claude answers locally with its usage line, then send an ordinary
message and check that no "never recorded this message" error appears.
"""

from __future__ import annotations

import time

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import set_fallback_mock_llm

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'

# Must match the model in the mock anthropic provider config written by the
# native_claude_mock_session fixture (conftest._CLAUDE_MOCK_MODEL).
_CLAUDE_MOCK_MODEL = "claude-sonnet-4-20250514"
_REPLY = "Here is the reply to the follow-up."
_LOST_MESSAGE_TEXT = "never recorded this message"

# claude-native auto-launch + first-run pre-accept + first mock turn.
_FIRST_TURN_TIMEOUT_MS = 180_000


def _send(page: Page, text: str) -> None:
    """Type *text* into the web composer and click Send.

    :param page: The Playwright page, on the session's chat surface.
    :param text: The message body to send.
    """
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _ensure_chat_view(page: Page) -> None:
    """Switch the terminal-first native session to its chat bubble view.

    :param page: The Playwright page, on the session's chat surface.
    """
    toggle = page.get_by_test_id("view-mode-toggle")
    expect(toggle).to_be_visible(timeout=_FIRST_TURN_TIMEOUT_MS)
    segment = page.get_by_test_id("view-mode-chat")
    expect(segment).to_be_enabled(timeout=30_000)
    segment.click()


def _session_items(base_url: str, session_id: str) -> list[dict[str, object]]:
    """Return the session's canonical transcript items, oldest first.

    :param base_url: Spawned server base URL.
    :param session_id: The session/conversation id.
    :returns: Items from ``GET /v1/sessions/{id}/items``.
    """
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 100, "order": "asc"},
        timeout=15.0,
    )
    resp.raise_for_status()
    return list(resp.json().get("data", []))


@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_web_fork_command_is_not_reported_lost(
    page: Page,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A web ``/fork`` Claude ran locally must not surface as a lost message.

    :param page: Playwright page (fresh context per test).
    :param native_claude_mock_session: ``(base_url, session_id)`` on the
        real claude-native wrapper, backed by the mock LLM.
    :param mock_llm_server_url: The mock LLM server base URL.
    """
    base_url, session_id = native_claude_mock_session
    set_fallback_mock_llm(mock_llm_server_url, "default", _REPLY)
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, _REPLY)

    page.goto(f"{base_url}/c/{session_id}")
    _ensure_chat_view(page)

    # Turn 1 proves the composer → bridge → CLI → mock → transcript pipeline is
    # live, so the /fork verdict below is not judged against a cold pane.
    _send(page, "hello")
    expect(page.locator(_ASSISTANT).first).to_be_visible(timeout=_FIRST_TURN_TIMEOUT_MS)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)
    # Let Claude Code finish turn 1 (its stop hook runs after the indicator
    # clears) so /fork is typed at an idle prompt instead of queued.
    time.sleep(15.0)

    # A bare /fork runs locally: Claude prints its usage line, no model turn.
    _send(page, "/fork")
    deadline = time.monotonic() + 90.0
    while time.monotonic() < deadline:
        if any(
            item.get("type") == "slash_command" and item.get("name") == "fork"
            for item in _session_items(base_url, session_id)
        ):
            break
        time.sleep(1.0)
    time.sleep(5.0)

    # The follow-up is what used to brand the /fork message as lost.
    replies = page.locator(_ASSISTANT).count()
    _send(page, "thanks, carry on")
    expect(page.locator(_ASSISTANT)).to_have_count(replies + 1, timeout=120_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)
    time.sleep(3.0)

    items = _session_items(base_url, session_id)
    lost = [
        item
        for item in items
        if item.get("type") == "error" and item.get("code") == "native_prompt_not_recorded"
    ]
    assert lost == [], f"/fork was reported as a lost message: {lost!r}"
    assert any(
        item.get("type") == "slash_command" and item.get("name") == "fork" for item in items
    ), f"/fork was never mirrored: {[item.get('type') for item in items]!r}"
    expect(page.get_by_text(_LOST_MESSAGE_TEXT)).to_have_count(0)
