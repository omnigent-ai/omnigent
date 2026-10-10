r"""E2E: a partially answered AskUserQuestion card keeps its draft across navigation.

A four-question ``AskUserQuestion`` is parked on a seeded session the same way
``test_ask_user_question.py`` parks one (a background POST to the server's
``POST /v1/sessions/{session_id}/hooks/permission-request`` endpoint, which is
what Claude Code's PermissionRequest hook calls). The test answers the card only
part-way — picks an option on question 1, moves to question 2 and types into
its "Type something" row — then leaves the session through the sidebar's Inbox
row and comes back through the session's own sidebar row.

The question is still pending on the server, so the card renders again. The
draft entered before leaving must still be in it: the picked option checked,
the typed text present, and the carousel on the question the user left.
"""

from __future__ import annotations

import logging
import re
import threading
import time

import httpx
import pytest
from playwright.sync_api import Page, expect

_log = logging.getLogger(__name__)

_APPROVAL_CARD = '[data-testid="approval-card"]'
_FORM = '[data-testid="ask-user-question-form"]'
_PROGRESS = '[data-testid="ask-user-question-progress"]'
_PREV = '[data-testid="ask-user-question-prev"]'
_NEXT = '[data-testid="ask-user-question-next"]'
_CUSTOM_INPUT = '[data-testid="ask-user-question-custom-input"]'
_INBOX_BUTTON = '[data-testid="inbox-button"]'

_RENDER_TIMEOUT_MS = 15_000
# Loading the conversation page is slower than rendering a card in it.
_LOAD_TIMEOUT_MS = 60_000

_FIRST_CHOICE = "Apply proposal"
_DRAFT_TEXT = "Draft answer that should survive navigation"

_QUESTIONS = [
    {
        "question": (
            'conftest.py:2843: "what is browser backed ui runner". How should I handle it?'
        ),
        "header": "runner msg",
        "options": [
            {"label": _FIRST_CHOICE, "description": "Reword the UsageError and reply."},
            {"label": "Reply only", "description": "No code change; reply with an explanation."},
            {"label": "Skip", "description": "Leave the thread alone."},
        ],
    },
    {
        "question": "Which fixture should own the browser_fleet_debug flag?",
        "header": "fixture",
        "options": [
            {"label": "page fixture", "description": "Set it unconditionally there."},
            {"label": "ui_local_storage", "description": "Keep both consumers on it."},
        ],
    },
    {
        "question": "Which checks should run before replying?",
        "header": "checks",
        "multiSelect": True,
        "options": [
            {"label": "Lint", "description": "pre-commit on the touched files."},
            {"label": "Unit tests", "description": "The fixture's own suite."},
            {"label": "UI smoke", "description": "One browser-backed run."},
        ],
    },
    {
        "question": "Where should the explanation go?",
        "header": "reply",
        "options": [
            {"label": "Thread reply", "description": "Answer in the review thread."},
            {"label": "Commit message", "description": "Explain it in the commit."},
        ],
    },
]


def _pending_elicitations(base_url: str, session_id: str) -> list[dict]:
    """Return the session snapshot's pending elicitation events (owner view)."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json().get("pending_elicitations") or []


def _wait_for(predicate, *, timeout_s: float = 30.0, interval_s: float = 0.5) -> None:
    """Poll *predicate* until truthy or the deadline passes."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    raise AssertionError("condition not met within timeout")


def _post_ask_user_question(base_url: str, session_id: str, holder: dict) -> threading.Thread:
    """Park a four-question ``AskUserQuestion`` permission request on *session_id*.

    The call blocks server-side until a verdict lands, so it runs on its own
    daemon thread; the fixture's teardown drains it when the session is deleted.
    """

    def _post() -> None:
        try:
            resp = httpx.post(
                f"{base_url}/v1/sessions/{session_id}/hooks/permission-request",
                json={"tool_name": "AskUserQuestion", "tool_input": {"questions": _QUESTIONS}},
                timeout=600.0,
            )
            resp.raise_for_status()
            holder["response"] = resp.json()
        except Exception as exc:
            holder["error"] = exc

    thread = threading.Thread(target=_post, daemon=True)
    thread.start()
    return thread


def _pending_form(page: Page):
    """The AskUserQuestion form inside the pending approval card."""
    card = (
        page.locator(f'{_APPROVAL_CARD}[data-state="pending"]')
        .filter(has=page.locator(_FORM))
        .first
    )
    return card.locator(_FORM)


def _first_choice(form):
    return form.get_by_role("radio", name=re.compile(rf"^{re.escape(_FIRST_CHOICE)}"))


@pytest.mark.timeout(240)
def test_partial_answers_survive_leaving_and_returning(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
) -> None:
    """Pick an option, type a draft, go to the Inbox and back: the draft is still there."""
    base_url, session_id = seeded_session
    _log.info("seeded session ready: base_url=%s session_id=%s", base_url, session_id)

    result_holder: dict = {}
    post_thread = _post_ask_user_question(base_url, session_id, result_holder)

    def _question_posted() -> bool:
        # Fail fast on a hook POST error instead of waiting out the full
        # timeout for a question that will never arrive.
        if "error" in result_holder:
            raise AssertionError(f"hook POST failed: {result_holder['error']}")
        return bool(_pending_elicitations(base_url, session_id))

    _wait_for(_question_posted)

    # The recorded page is created only now, after the non-browser setup.
    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")

    form = _pending_form(page)
    expect(form).to_be_visible(timeout=_LOAD_TIMEOUT_MS)
    expect(form.locator(_PROGRESS)).to_have_text("Question 1 of 4:")

    # Partially answer: an option on question 1, free text on question 2.
    _first_choice(form).check()
    expect(_first_choice(form)).to_be_checked()
    form.locator(_NEXT).click()
    expect(form.locator(_PROGRESS)).to_have_text("Question 2 of 4:")
    form.locator(_CUSTOM_INPUT).fill(_DRAFT_TEXT)
    expect(form.locator(_CUSTOM_INPUT)).to_have_value(_DRAFT_TEXT)

    # Leave through the sidebar's Inbox row, come back through the session's row.
    session_row = page.locator(f'a[href$="/c/{session_id}"]').first
    expect(session_row).to_be_visible()
    page.locator(_INBOX_BUTTON).click()
    page.wait_for_url(re.compile(r"/inbox/?$"), timeout=_LOAD_TIMEOUT_MS)
    expect(page.locator(_FORM).first).to_be_visible(timeout=_RENDER_TIMEOUT_MS)
    page.locator(f'a[href$="/c/{session_id}"]').first.click()
    page.wait_for_url(re.compile(rf"/c/{re.escape(session_id)}/?$"), timeout=_LOAD_TIMEOUT_MS)

    form = _pending_form(page)
    expect(form).to_be_visible(timeout=_LOAD_TIMEOUT_MS)
    assert _pending_elicitations(base_url, session_id), (
        "the question itself was lost, not the draft"
    )

    # Read the whole draft back, walking the carousel so the probe does not
    # depend on where it landed, then return to question 1.
    progress_on_return = form.locator(_PROGRESS).inner_text().strip()
    # Bounded by the question count so a stuck Prev button can't spin forever.
    for _ in range(len(_QUESTIONS)):
        if not form.locator(_PREV).is_enabled():
            break
        form.locator(_PREV).click()
    expect(form.locator(_PROGRESS)).to_have_text("Question 1 of 4:")
    first_choice_checked = _first_choice(form).is_checked()
    form.locator(_NEXT).click()
    expect(form.locator(_PROGRESS)).to_have_text("Question 2 of 4:")
    draft_text = form.locator(_CUSTOM_INPUT).input_value()
    form.locator(_PREV).click()
    expect(form.locator(_PROGRESS)).to_have_text("Question 1 of 4:")

    observed = {
        "progress_on_return": progress_on_return,
        "first_choice_checked": first_choice_checked,
        "draft_text": draft_text,
    }
    expected = {
        "progress_on_return": "Question 2 of 4:",
        "first_choice_checked": True,
        "draft_text": _DRAFT_TEXT,
    }
    assert observed == expected, f"draft lost after leaving and returning: {observed}"

    # The hook POST is still parked server-side (no verdict was ever sent), so
    # the thread stays alive until teardown deletes the session. Surface a late
    # request error first; otherwise a dead thread hides it behind a misleading
    # "returned before any verdict" message.
    assert "error" not in result_holder, f"hook POST errored: {result_holder.get('error')}"
    assert post_thread.is_alive(), "the hook POST returned before any verdict"
