"""E2E: Claude's output-token limit must not dead-end a claude-native turn.

Claude Code fails a turn whose reply ends with ``stop_reason: "max_tokens"`` using its
CLAUDE_CODE_MAX_OUTPUT_TOKENS error, a remedy the web user cannot apply.
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

# Must match the mock anthropic provider model the native_claude_mock_session
# fixture configures (conftest._CLAUDE_MOCK_MODEL).
_CLAUDE_MOCK_MODEL = "claude-sonnet-4-20250514"

# Only requests carrying this token draw from the max_tokens fault queue.
_FAULT_TOKEN = "overlong-report-fault"
_SANITY_LINE = "MOCK TURN OK output-limit-sanity"

# Claude Code's constant for a response that ended with stop_reason "max_tokens".
_RAW_LIMIT_ERROR_RE = re.compile(
    r"Claude['’]s response exceeded the [\d,]+ output token maximum\. "
    r"To configure this behavior, set the CLAUDE_CODE_MAX_OUTPUT_TOKENS "
    r"environment variable\."
)

# claude-native auto-launch + first-run pre-accept + first mock turn.
_FIRST_TURN_TIMEOUT_MS = 180_000
# The fault turn must reach a terminal state (idle/failed) within this window.
_FAULT_SETTLE_S = 150.0
# The transcript mirror and the error pill can trail the failed status edge.
_MIRROR_GRACE_S = 8.0


def _send(page: Page, text: str) -> None:
    """Type *text* into the web composer and click Send."""
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _ensure_chat_view(page: Page) -> None:
    """Switch the terminal-first native session to its chat bubble view."""
    toggle = page.get_by_test_id("view-mode-toggle")
    expect(toggle).to_be_visible(timeout=_FIRST_TURN_TIMEOUT_MS)
    segment = page.get_by_test_id("view-mode-chat")
    expect(segment).to_be_enabled(timeout=30_000)
    segment.click()


def _transcript_blob(base_url: str, session_id: str) -> str:
    """Return the canonical transcript items, which the SPA renders, as one JSON string."""
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 200, "order": "asc"},
        timeout=15.0,
    )
    resp.raise_for_status()
    return json.dumps(resp.json().get("data", []), ensure_ascii=False)


def _session_snapshot(base_url: str, session_id: str) -> tuple[str, str]:
    """Return the session status and its ``last_task_error`` message (``""`` when none)."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=15.0)
    resp.raise_for_status()
    body = resp.json()
    error = body.get("last_task_error") or {}
    return str(body.get("status") or ""), str(error.get("message") or "")


def _error_pill_text(page: Page) -> str:
    """Return the error pill's headline title plus its visible text (``""`` when no pill)."""
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


def _assistant_bubble_text(page: Page) -> str:
    """Return every assistant bubble's visible text, newline-joined (``""`` when none)."""
    # Bubbles can re-render mid-read; their text is best-effort.
    with contextlib.suppress(Exception):
        return "\n".join(page.locator(_ASSISTANT).all_inner_texts())
    return ""


def _raw_constant_surfaces(page: Page, base_url: str, session_id: str) -> list[tuple[str, str]]:
    """Return ``(surface, matched_text)`` for each web-visible surface carrying the constant."""
    _, failure_reason = _session_snapshot(base_url, session_id)
    surfaces = (
        ("the chat view's assistant bubbles", _assistant_bubble_text(page)),
        ("the failed turn's error pill", _error_pill_text(page)),
        ("the canonical transcript", _transcript_blob(base_url, session_id)),
        ("the failed session's last_task_error", failure_reason),
    )
    hits: list[tuple[str, str]] = []
    for where, text in surfaces:
        found = _RAW_LIMIT_ERROR_RE.search(text)
        if found:
            hits.append((where, found.group(0)))
    return hits


def _turn_settled(page: Page, status: str) -> bool:
    """Whether the turn is over: ``failed``, or ``idle`` with a second reply and no work."""
    if status == "failed":
        return True
    return (
        status == "idle"
        and page.locator(_ASSISTANT).count() >= 2
        and page.locator(_WORKING).count() == 0
    )


def _expand_error_pill(page: Page) -> None:
    """Best-effort: open the error pill so its full message is on screen."""
    with contextlib.suppress(Exception):
        pill = page.locator(_ERROR_PILL).first
        pill.locator('button[aria-expanded="false"]').first.click(timeout=5_000)
        expect(pill.get_by_test_id("error-message-content")).to_be_visible(timeout=5_000)
        page.wait_for_timeout(2_000)


@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_output_token_limit_turn_is_not_a_raw_dead_end(
    page: Page,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A turn that hits Claude's output-token max must not strand the user on the raw CLI error.

    After a sanity turn, the fault turn must settle with no raw constant on any web surface.
    """
    base_url, session_id = native_claude_mock_session

    # Fallbacks survive /mock/reset, so Claude's background requests answer normally.
    set_fallback_mock_llm(mock_llm_server_url, "default", _SANITY_LINE)
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, _SANITY_LINE)

    # Deep enough that background requests (title generation etc.) carrying the
    # token cannot drain the queue before the main-loop request draws it.
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

    # Turn 1 — sanity: a broken pipeline can never masquerade as a fixed build.
    _send(page, "hello, quick check before the real request")
    expect(page.locator(_ASSISTANT, has_text=_SANITY_LINE).first).to_be_visible(
        timeout=_FIRST_TURN_TIMEOUT_MS
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)

    # Claude Code's stop hooks run for a few seconds after the indicator clears.
    time.sleep(10.0)

    # Turn 2 — the scripted reply to this request ends with stop_reason "max_tokens".
    _send(page, f"please write the full 50-page report now ({_FAULT_TOKEN})")
    expect(page.locator(_USER, has_text=_FAULT_TOKEN).first).to_be_visible(timeout=60_000)

    hits: list[tuple[str, str]] = []
    settled_at: float | None = None
    deadline = time.monotonic() + _FAULT_SETTLE_S
    while time.monotonic() < deadline:
        hits = _raw_constant_surfaces(page, base_url, session_id)
        if hits:
            # Let the mirror and the pill catch up so every affected surface is named.
            time.sleep(_MIRROR_GRACE_S)
            hits = _raw_constant_surfaces(page, base_url, session_id)
            break
        status, _ = _session_snapshot(base_url, session_id)
        if settled_at is None and _turn_settled(page, status):
            settled_at = time.monotonic()
        if settled_at is not None and time.monotonic() - settled_at >= _MIRROR_GRACE_S:
            break
        time.sleep(2.0)

    if hits:
        _expand_error_pill(page)
        where = "; ".join(f"{surface} carried {text!r}" for surface, text in hits)
        pytest.fail(
            "a claude-native turn that hit Claude's output-token maximum "
            f"dead-ended on the raw CLI constant — {where}. "
            "The named remedy (setting CLAUDE_CODE_MAX_OUTPUT_TOKENS on the "
            "running CLI) is not available from the Omnigent web chat, and the "
            "server counts the turn as an Omnigent failure ('session turn "
            "failed for <id>: API Error: ...', the turn-failure KPI "
            "signature). Handle the upstream limit the way the "
            "context-overflow constant is handled instead of relaying the raw "
            "dead end."
        )
    if settled_at is None:
        pytest.fail(
            f"the output-token-limit turn never reached a terminal state within "
            f"{_FAULT_SETTLE_S:.0f}s (no failed status, no second assistant "
            "reply) — the claude-native pipeline did not finish the turn, so "
            "its output-token handling could not be judged."
        )
    # Evidence of how the settled turn was attributed (status + last_task_error).
    snapshot = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=15.0).json()
    print(
        "fault turn settled:",
        json.dumps(
            {"status": snapshot.get("status"), "last_task_error": snapshot.get("last_task_error")},
            ensure_ascii=False,
        ),
    )
