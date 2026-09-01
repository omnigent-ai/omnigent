r"""E2E: ``/login`` from web chat on a claude-native session with an expired login.

On a claude-native session whose Claude Code login has expired, every web
turn answers ``Login expired · Please run /login`` — and typing ``/login``
in the composer cannot fix it, because ``/login`` is in
``_CLAUDE_CLI_DROPPED_COMMANDS`` (``omnigent/claude_native_bridge.py``) and
not in ``_CLAUDE_NATIVE_ALLOWED_USER_SLASH_COMMANDS``, so
``_escape_unsupported_slash_command`` prefixes it with U+FEFF and Claude
Code receives it as ordinary prose. The CLI then answers it like any other
prompt — on an expired login, with the very same "Please run /login" line —
silently spending a model turn on the exact command the error told the user
to run.

The journey drives the real claude-native stack (a live ``claude`` CLI in
the session terminal, fed by the in-process mock LLM): the mock plays the
expired-login answer to every turn, the test sends a message and then
``/login`` from the web composer, and asserts the ``/login`` turn is NOT
silently forwarded to the model as escaped prose. Today both observable
halves of the bug fire and the test FAILS:

- the canonical transcript records the composer's ``/login`` as the
  FEFF-escaped user message ``"﻿/login"``, and
- the model receives a request whose user text carries that escaped
  ``"﻿/login"`` (the silently-spent turn).

A fix that intercepts ``/login``/``/logout`` before injection — e.g.
answering with what actually re-authenticates (``omni setup`` on the host,
whose anthropic step runs ``claude auth login --claudeai``) — makes both
checks pass without pinning the fix's exact UI shape.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import reset_mock_llm, set_fallback_mock_llm

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_USER = '[data-testid="message-bubble"][data-role="user"]'
_WORKING = '[data-testid="working-indicator"]'

# Must match the model in the mock anthropic provider config written by the
# native_claude_mock_session fixture (conftest._CLAUDE_MOCK_MODEL).
_CLAUDE_MOCK_MODEL = "claude-sonnet-4-20250514"

# The dead-end answer an expired Claude Code login gives every turn. The
# mock LLM plays it as the fallback for all requests, exactly reproducing
# the reported session state.
_LOGIN_EXPIRED_LINE = "Login expired · Please run /login"

# What _escape_unsupported_slash_command turns the composer's /login into
# before the bridge injects it into Claude Code's TUI: a zero-width
# no-break space (U+FEFF) prefixed to defeat command parsing.
_ESCAPED_LOGIN = "﻿/login"

# claude-native auto-launch + first-run pre-accept + first mock turn.
_FIRST_TURN_TIMEOUT_MS = 180_000

# How long to watch for the buggy /login turn to surface after sending.
# On the bug this trips within one mock turn (seconds); on a fixed build
# nothing arrives and the watch simply expires, letting the test pass.
_LOGIN_TURN_WATCH_S = 90.0


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


def _user_item_texts(base_url: str, session_id: str) -> list[str]:
    """Return the canonical transcript's user-message texts, in order.

    Reads ``GET /v1/sessions/{id}/items`` — the same API both the SPA chat
    view and the TUI-parity forwarder render from — so an assertion here is
    an assertion about what the session actually recorded, not about a
    transient DOM state.

    :param base_url: Spawned server base URL.
    :param session_id: The session/conversation id.
    :returns: Each user message item's concatenated text.
    """
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 100, "order": "asc"},
        timeout=15.0,
    )
    resp.raise_for_status()
    texts: list[str] = []
    for item in resp.json().get("data", []):
        if item.get("type") != "message" or item.get("role") != "user":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        texts.append(
            " ".join(
                block["text"]
                for block in content
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            )
        )
    return texts


def _captured_request_blob(mock_url: str) -> str:
    """Return every request body the mock LLM captured, as one JSON string.

    :param mock_url: Mock LLM server base URL.
    :returns: The serialized captured requests (``""`` when none).
    """
    resp = httpx.get(f"{mock_url}/mock/requests", timeout=15.0)
    resp.raise_for_status()
    return json.dumps(resp.json().get("requests", []), ensure_ascii=False)


@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_login_slash_is_not_silently_spent_on_the_model(
    page: Page,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """``/login`` from the composer must not be FEFF-escaped into a model turn.

    Journey: with the Claude Code login expired, send any
    message → the assistant answers "Login expired · Please run /login";
    type ``/login`` → today the bridge escapes it to ``"﻿/login"``,
    Claude Code answers it as prose with the same dead-end line, and a
    model turn is silently spent. The session can never be recovered from
    the web UI, because the remedy the error names is exactly the command
    the UI refuses to send.

    :param page: Playwright page (fresh context per test).
    :param native_claude_mock_session: ``(base_url, session_id)`` on the
        real claude-native wrapper, backed by the mock LLM.
    :param mock_llm_server_url: The mock LLM server base URL.
    """
    base_url, session_id = native_claude_mock_session

    # Every model call answers with the expired-login line — the exact
    # session state the report describes. Fallbacks survive /mock/reset,
    # so Claude's background requests can't drain them.
    set_fallback_mock_llm(mock_llm_server_url, "default", _LOGIN_EXPIRED_LINE)
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, _LOGIN_EXPIRED_LINE)

    page.goto(f"{base_url}/c/{session_id}")
    _ensure_chat_view(page)

    # Turn 1 — any ordinary message comes back as the dead-end line. This
    # both reproduces the reported state and proves the composer → bridge →
    # CLI → mock-LLM → transcript pipeline is live before /login is judged.
    _send(page, "hello, are you there?")
    expect(page.locator(_ASSISTANT, has_text=_LOGIN_EXPIRED_LINE).first).to_be_visible(
        timeout=_FIRST_TURN_TIMEOUT_MS
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)

    # Clear the capture log so the /login verdict below scans only traffic
    # from the /login turn onward (set_fallback survives this reset).
    reset_mock_llm(mock_llm_server_url)

    # Let Claude Code finish turn 1 entirely (its stop hook runs for a few
    # seconds after the working indicator clears). A /login pasted while the
    # CLI is still busy is queued instead of typed: the queue strips the
    # bridge's escaping prefix and dequeues /login as a real slash command,
    # which masks the journey under test.
    time.sleep(15.0)

    # Turn 2 — the user does exactly what the error told them to.
    _send(page, "/login")
    expect(page.locator(_USER, has_text="/login").first).to_be_visible(timeout=60_000)

    # Watch for either observable half of the bug: the transcript records
    # the FEFF-escaped user message, or the model receives it. On a fixed
    # build neither ever arrives and the watch expires cleanly.
    escaped_in_transcript = False
    escaped_at_model = False
    deadline = time.monotonic() + _LOGIN_TURN_WATCH_S
    while time.monotonic() < deadline:
        if any(text == _ESCAPED_LOGIN for text in _user_item_texts(base_url, session_id)):
            escaped_in_transcript = True
            break
        if _ESCAPED_LOGIN in _captured_request_blob(mock_llm_server_url):
            escaped_at_model = True
            break
        time.sleep(2.0)

    if escaped_in_transcript or escaped_at_model:
        where = (
            "the canonical transcript recorded the composer's /login as the "
            f"FEFF-escaped prose message {_ESCAPED_LOGIN!r}"
            if escaped_in_transcript
            else f"the model received a request carrying {_ESCAPED_LOGIN!r}"
        )
        pytest.fail(
            "/login typed into a claude-native session's web "
            f"composer was silently spent as an ordinary model turn — {where}. "
            "On an expired login the model answers with the same "
            f"{_LOGIN_EXPIRED_LINE!r} dead-end, so the session can never be "
            "recovered from the web UI. /login (and /logout) must be "
            "intercepted before injection and answered with what actually "
            "re-authenticates (e.g. `omni setup` on the host) instead."
        )
