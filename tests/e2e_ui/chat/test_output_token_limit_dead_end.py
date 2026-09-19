r"""E2E: Claude's output-token-limit API error dead-ends a claude-native turn.

When a claude-native ("Claude Code") session's response hits the CLI's
output-token maximum (default 32,000; the API returns ``stop_reason:
"max_tokens"``), Claude Code synthesizes its own API-error record::

    API Error: Claude's response exceeded the 32000 output token maximum.
    To configure this behavior, set the CLAUDE_CODE_MAX_OUTPUT_TOKENS
    environment variable.

and fails the turn. The bridge/forwarder relay that record verbatim: the
web chat shows the raw CLI constant, the ``StopFailure`` hook flips the
session to ``failed``, and the server logs ``session turn failed for
<id>: API Error: ...`` at ERROR (the turn-failure KPI signature,
``omnigent.server.routes.sessions/_publish_status``).

That raw constant is a dead end in the Omnigent web chat: the remedy it
names — setting the ``CLAUDE_CODE_MAX_OUTPUT_TOKENS`` environment
variable of the running CLI — is not something a web user can do, exactly
like the ``/login`` dead-end and the raw ``prompt is too long`` overflow
before it. The bridge already rewrites the context-overflow constant into
actionable guidance (``_CONTEXT_OVERFLOW_REPLACEMENT`` in
``omnigent/harnesses/claude_native/bridge.py``); the output-token-limit
constant has no such handling and surfaces raw.

The journey drives the real claude-native stack (a live ``claude`` CLI in
the session terminal, fed by the in-process mock LLM): a sanity turn
proves the pipeline, then a turn whose scripted response ends with
``stop_reason: "max_tokens"`` — the exact upstream condition — and the
test watches the session's user-visible outcome. Today the raw CLI
constant surfaces (transcript and/or failed-status error pill) and the
test FAILS, reproducing the ticket.

A fix that handles the upstream limit — rewriting the record into
actionable guidance the way the context-overflow path does, retrying with
a raised limit, or otherwise resolving the turn without stranding the
user on the env-var instruction — makes the watch expire cleanly and the
test pass, without pinning the fix's exact UI shape.
"""

from __future__ import annotations

import contextlib
import json
import re
import time

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm, set_fallback_mock_llm

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_USER = '[data-testid="message-bubble"][data-role="user"]'
_WORKING = '[data-testid="working-indicator"]'
_ERROR_PILL = '[data-testid="error-pill"]'

# Must match the model in the mock anthropic provider config written by the
# native_claude_mock_session fixture (conftest._CLAUDE_MOCK_MODEL).
_CLAUDE_MOCK_MODEL = "claude-sonnet-4-20250514"

# Unique content-routing token: any model request whose user text carries it
# draws from the max_tokens fault queue, so only the turn under test (and its
# background copies) hits the fault — never another test's traffic.
_FAULT_TOKEN = "overlong-answer-fault"

# What every non-fault model call answers — proves the composer → bridge →
# CLI → mock-LLM → transcript pipeline is live before the fault is judged.
_SANITY_LINE = "MOCK TURN OK output-limit-check"

# The CLI constant Claude Code synthesizes when a response ends with
# stop_reason "max_tokens" (read out of @anthropic-ai/claude-code 2.1.236;
# the number is the CLI's configured max output tokens, 32000 by default).
# Its only remedy — an environment variable of the running CLI process — is
# unreachable from the Omnigent web chat, so surfacing it raw strands the
# user. Matched loosely (any count, either apostrophe) so a CLI bump doesn't
# silently retire the guard.
_RAW_LIMIT_ERROR_RE = re.compile(
    r"API Error: Claude['’]s response exceeded the [\d,]+ output token "
    r"maximum\. To configure this behavior, set the "
    r"CLAUDE_CODE_MAX_OUTPUT_TOKENS environment variable\."
)

# claude-native auto-launch + first-run pre-accept + first mock turn.
_FIRST_TURN_TIMEOUT_MS = 180_000

# How long to watch for the raw constant to surface after the fault turn.
# On the bug this trips within one mock turn (seconds); on a fixed build
# nothing arrives and the watch simply expires, letting the test pass.
_FAULT_WATCH_S = 150.0


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


def _transcript_blob(base_url: str, session_id: str) -> str:
    """Return the canonical transcript's items as one JSON string.

    Reads ``GET /v1/sessions/{id}/items`` — the same API the SPA chat view
    renders from — so a match here is a match on what the session actually
    shows the user, not a transient DOM state.

    :param base_url: Spawned server base URL.
    :param session_id: The session/conversation id.
    :returns: The serialized transcript items (``""`` when none).
    """
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 200, "order": "asc"},
        timeout=15.0,
    )
    resp.raise_for_status()
    return json.dumps(resp.json().get("data", []), ensure_ascii=False)


def _error_pill_text(page: Page) -> str:
    """Return the visible error pill's full text (``""`` when no pill).

    The pill's headline carries the failure message the ``failed`` status
    edge delivered (truncated by CSS only, so ``inner_text`` still returns
    it whole); the ``title`` attribute mirrors it untruncated.

    :param page: The Playwright page, on the session's chat surface.
    :returns: Headline title + pill inner text, space-joined.
    """
    pill = page.locator(_ERROR_PILL)
    if pill.count() == 0:
        return ""
    parts: list[str] = []
    headline = pill.first.locator('[data-testid="error-headline"]')
    if headline.count() > 0:
        parts.append(headline.first.get_attribute("title") or "")
    # Pill may detach mid-read; its text is best-effort.
    with contextlib.suppress(Exception):
        parts.append(pill.first.inner_text(timeout=5_000))
    return " ".join(part for part in parts if part)


@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_output_token_limit_turn_is_not_a_raw_dead_end(
    page: Page,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A turn that hits Claude's output-token max must not strand the user on the raw CLI error.

    Journey: send an ordinary message (sanity turn, answered normally);
    then send a request whose scripted response ends with ``stop_reason:
    "max_tokens"`` — the upstream output-token-limit condition. Today the
    turn dies with the raw ``API Error: Claude's response exceeded the
    32000 output token maximum. To configure this behavior, set the
    CLAUDE_CODE_MAX_OUTPUT_TOKENS environment variable.`` surfacing to the
    web user (transcript row and failed-status error pill), whose named
    remedy cannot be performed from the web UI — and the server counts an
    Omnigent turn failure (the turn-failure KPI signature).

    :param page: Playwright page (fresh context per test).
    :param native_claude_mock_session: ``(base_url, session_id)`` on the
        real claude-native wrapper, backed by the mock LLM.
    :param mock_llm_server_url: The mock LLM server base URL.
    """
    base_url, session_id = native_claude_mock_session

    # Every model call outside the fault turn answers normally. Fallbacks
    # survive /mock/reset, so Claude's background requests can't drain them.
    set_fallback_mock_llm(mock_llm_server_url, "default", _SANITY_LINE)
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, _SANITY_LINE)

    # The fault queue: any request whose user text carries the token gets a
    # partial answer that ends with stop_reason "max_tokens" — the wire shape
    # Anthropic returns when a response hits the output-token limit. Scripted
    # deep enough that Claude Code's background requests (title generation
    # etc.), which resend the conversation text and so also match the token,
    # cannot drain the queue before the main-loop request draws its fault.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "text": "Here is the start of the very long report you asked for —",
                "stop_reason": "max_tokens",
            }
        ]
        * 12,
        key="output-token-limit-fault",
        match=_FAULT_TOKEN,
    )

    page.goto(f"{base_url}/c/{session_id}")
    _ensure_chat_view(page)

    # Turn 1 — sanity: an ordinary message round-trips through the live
    # claude CLI and the mock LLM before the fault is judged, so a broken
    # pipeline can never masquerade as a fixed build.
    _send(page, "hello, quick check before the real request")
    expect(page.locator(_ASSISTANT, has_text=_SANITY_LINE).first).to_be_visible(
        timeout=_FIRST_TURN_TIMEOUT_MS
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)

    # Let Claude Code finish turn 1 entirely (its stop hooks run for a few
    # seconds after the working indicator clears) so the fault turn's edges
    # can't interleave with turn 1's.
    time.sleep(10.0)

    # Turn 2 — the user asks for output that exceeds the model's
    # output-token maximum; the scripted response is the API's answer.
    _send(page, f"please write the full report now ({_FAULT_TOKEN})")
    expect(page.locator(_USER, has_text=_FAULT_TOKEN).first).to_be_visible(timeout=60_000)

    # Watch for either user-visible half of the bug: the transcript records
    # the raw CLI constant, or the failed-status error pill carries it. On a
    # fixed build neither ever arrives and the watch expires cleanly.
    raw_in_transcript: str | None = None
    raw_on_status: str | None = None
    deadline = time.monotonic() + _FAULT_WATCH_S
    while time.monotonic() < deadline:
        found = _RAW_LIMIT_ERROR_RE.search(_transcript_blob(base_url, session_id))
        if found:
            raw_in_transcript = found.group(0)
            break
        found = _RAW_LIMIT_ERROR_RE.search(_error_pill_text(page))
        if found:
            raw_on_status = found.group(0)
            break
        time.sleep(2.0)

    if raw_in_transcript or raw_on_status:
        where = (
            f"the canonical transcript recorded the raw CLI error {raw_in_transcript!r}"
            if raw_in_transcript
            else f"the failed turn's error pill surfaced the raw CLI error {raw_on_status!r}"
        )
        pytest.fail(
            "a claude-native turn that hit Claude's output-token maximum "
            f"dead-ended on the raw CLI constant — {where}. The named remedy "
            "(setting CLAUDE_CODE_MAX_OUTPUT_TOKENS on the running CLI) is "
            "not available from the Omnigent web chat, and the server counts "
            "the turn as an Omnigent failure ('session turn failed for "
            "<id>: API Error: ...' — the turn-failure KPI signature). Handle "
            "the upstream limit the way the context-overflow constant is "
            "handled (actionable guidance / recovery), instead of relaying "
            "the raw dead end."
        )
