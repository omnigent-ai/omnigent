"""E2E: an in-progress composer draft must survive newly arriving messages.

The reported journey: the user is typing a response when additional
agent responses land in the transcript, and the in-progress answer
gets reset.

Two user-observable claims, each with its own test:

1. **Mid-typing keystroke loss when a pending prompt arrives.** A tool
   call trips a permission gate and a pending ``ApprovalCard`` lands
   while the user is mid-sentence. On a buggy build the pending prompt
   disables the composer textarea (``hasPendingElicitation``), which
   ejects browser focus to ``<body>`` mid-word — every keystroke the
   user keeps typing silently vanishes, so their in-progress answer
   stops registering ("got reset"). The test types half an answer,
   parks a permission prompt, keeps typing, and asserts the whole
   answer landed.

2. **Draft survival across streaming replies + queue-flush re-renders.**
   A plausible failure mode here is that the composer re-syncs
   its value from per-session draft storage when a queued-message event
   re-renders, overwriting live typing. The test types a draft while a
   reply streams in and a queued follow-up flushes, and asserts the
   draft is untouched once everything settles.

Both tests drive the real SPA against the spawned server. The
permission prompt is raised through the server's real gate ingress
(``POST /v1/sessions/{id}/hooks/permission-request`` — the same route
native-harness hooks call), parked until the UI resolves it, mirroring
``tests/e2e_ui/approvals/test_persistent_approval.py``.
"""

from __future__ import annotations

import contextlib
import re
import threading

import httpx
import pytest
from playwright.sync_api import Page, expect

_APPROVAL_CARD = '[data-testid="approval-card"]'
_COMPOSER = 'textarea[aria-label="Message the agent"]'
_PROMPT_APPEAR_TIMEOUT_MS = 15_000

# The kickoff message that starts the agent turn ("new messages" then
# arrive as its reply). Worded so the mock reply has no reason to echo it.
_KICKOFF_MSG = "sentinel-kickoff-deploy please start the deploy"

# The user's in-progress answer, mid-sentence when the prompt interrupts.
_ANSWER_HEAD = "Yes, roll it out to staging first"
_ANSWER_TAIL = " and hold production until the smoke tests pass"

# Facet-2 sentinels: first turn, queued follow-up, and the live draft.
_TURN_A_MSG = "sentinel-turn-a first request"
_TURN_B_MSG = "sentinel-turn-b queued follow-up"
_LIVE_DRAFT = "drafting my next thought while replies arrive"


def _send(page: Page, text: str) -> None:
    """Type ``text`` into the composer and click Send."""
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible()
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _user_bubble(page: Page, text: str):
    """Locator for the user-message bubble carrying ``text``."""
    return page.locator('[data-testid="message-bubble"][data-role="user"]').filter(has_text=text)


def _pending_elicitations(base_url: str, session_id: str) -> list[dict]:
    """Return the session snapshot's pending elicitation events."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json().get("pending_elicitations") or []


def _start_parked_prompt(base_url: str, session_id: str) -> tuple[threading.Thread, dict]:
    """POST a gated-Bash permission request on a background thread.

    The route parks until the UI resolves the prompt, so the POST runs on
    its own thread and reports back through ``holder``.
    """
    holder: dict = {}

    def _post() -> None:
        try:
            resp = httpx.post(
                f"{base_url}/v1/sessions/{session_id}/hooks/permission-request",
                json={"tool_name": "Bash", "tool_input": {"command": "systemctl restart workers"}},
                timeout=120.0,
            )
            resp.raise_for_status()
            holder["response"] = resp.json()
        except Exception as exc:  # surfaced by the caller after UI assertions
            holder["error"] = exc

    thread = threading.Thread(target=_post, daemon=True)
    thread.start()
    return thread, holder


def _resolve_prompt_if_pending(page: Page) -> None:
    """Best-effort: approve a still-pending card so the parked hook returns."""
    with contextlib.suppress(Exception):
        card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
        if card.is_visible():
            card.get_by_role("button", name="Approve", exact=True).click()
            page.wait_for_timeout(500)


@pytest.mark.timeout(180)
def test_mid_typing_answer_survives_arriving_prompt(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Keystrokes typed as a pending prompt lands must not be dropped.

    Journey: open a session and exchange one turn → start typing an
    in-progress answer → the agent's next action raises a permission
    prompt, whose card lands near the composer → keep typing. Every
    keystroke must land in the draft. On a buggy build the card's
    arrival disables the textarea, focus is ejected to ``<body>``, and
    the continuation silently vanishes — the reported "in-progress
    answer got reset".
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    # One real exchange first: the user is mid-conversation, not on a
    # blank session, when the interruption happens.
    _send(page, _KICKOFF_MSG)
    expect(_user_bubble(page, _KICKOFF_MSG)).to_be_visible(timeout=10_000)
    assistant = page.locator('[data-testid="message-bubble"][data-role="assistant"]').first
    expect(assistant).to_have_text(re.compile(r"\S"), timeout=60_000)

    # The user starts typing their next answer.
    composer = page.locator(_COMPOSER)
    expect(composer).to_be_visible()
    composer.click()
    page.keyboard.type(_ANSWER_HEAD)
    expect(composer).to_have_value(_ANSWER_HEAD)

    # A permission prompt arrives mid-sentence (real gate ingress; parks
    # server-side until answered).
    hook_thread, holder = _start_parked_prompt(base_url, session_id)
    try:
        card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
        expect(card).to_be_visible(timeout=_PROMPT_APPEAR_TIMEOUT_MS)
        assert _pending_elicitations(base_url, session_id), "server has no parked elicitation"

        # The user, eyes on the keyboard, keeps typing.
        page.keyboard.type(_ANSWER_TAIL)

        # Every keystroke must have landed in the draft. On the buggy
        # build the value stops at _ANSWER_HEAD: the card's arrival
        # disabled the textarea and ejected focus, so the continuation
        # was silently dropped.
        assert composer.input_value() == _ANSWER_HEAD + _ANSWER_TAIL, (
            "keystrokes typed after the prompt appeared were silently dropped — "
            "the in-progress answer got reset by the arriving prompt "
            f"(composer now holds: {composer.input_value()!r})"
        )
    finally:
        # Answer the prompt so the parked hook returns and teardown is clean.
        _resolve_prompt_if_pending(page)
        hook_thread.join(timeout=30)

    if "error" in holder:
        raise AssertionError(f"hook thread failed: {holder['error']}") from holder["error"]


@pytest.mark.timeout(180)
def test_draft_survives_streaming_reply_and_queue_flush(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A live draft must survive replies streaming in and a queue flush.

    Journey: send A (turn starts) → immediately send B (queues while A
    streams — a queued-message event re-render) → type a draft while
    A's reply arrives and B flushes and replies → once both turns
    settle, the draft is byte-for-byte intact. Guards the hypothesized
    failure of the composer re-syncing its value from per-session draft
    storage on a queued-message re-render.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    _send(page, _TURN_A_MSG)
    expect(_user_bubble(page, _TURN_A_MSG)).to_be_visible(timeout=10_000)
    # Queue a follow-up while A's turn is in flight.
    _send(page, _TURN_B_MSG)
    expect(_user_bubble(page, _TURN_B_MSG)).to_be_visible(timeout=10_000)

    # Start a live draft while replies arrive and the queue drains.
    composer = page.locator(_COMPOSER)
    expect(composer).to_be_visible()
    composer.click()
    page.keyboard.type(_LIVE_DRAFT)
    expect(composer).to_have_value(_LIVE_DRAFT)

    # A's turn produces an assistant reply (the "new messages" streaming
    # in while the user types).
    assistant = page.locator('[data-testid="message-bubble"][data-role="assistant"]').first
    expect(assistant).to_have_text(re.compile(r"\S"), timeout=90_000)
    # The queued follow-up B flushes and its user bubble commits — the
    # queued-message re-render the triage hypothesis blamed for clobbering
    # the draft. Both user messages persist as exactly one bubble each.
    expect(_user_bubble(page, _TURN_A_MSG)).to_have_count(1, timeout=90_000)
    expect(_user_bubble(page, _TURN_B_MSG)).to_have_count(1, timeout=90_000)
    # Let any settle-time re-render (queue flush, draft-store sync) paint.
    page.wait_for_timeout(1_500)

    assert composer.input_value() == _LIVE_DRAFT, (
        "the in-progress draft was clobbered while new messages arrived "
        f"(composer now holds: {composer.input_value()!r})"
    )
