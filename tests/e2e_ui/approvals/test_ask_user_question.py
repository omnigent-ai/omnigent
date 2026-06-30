r"""E2E: AskUserQuestion renders as a web form (mock, no real Claude Code).

A background thread POSTs directly to the server's
``POST /v1/sessions/{session_id}/hooks/permission-request`` endpoint with an
``AskUserQuestion`` payload. The server parks the elicitation (long-poll), the
SPA renders it as an ``AskUserQuestionForm`` inside an ``ApprovalCard`` — radio
inputs for a single-select question and a Submit button. The test selects an
answer and submits it, confirming the parked HTTP call drains and the verdict
flowed back through the PermissionRequest round-trip.

This approach replaces the original native Claude Code session (real LLM turn)
with a seeded session and a synthetic hook POST — no real Claude Code needed,
test completes in seconds rather than minutes.

This is the structured-form counterpart to the binary approval card
(``test_approval_card.py``): the binary card covers a policy ASK, this covers
Claude's own question tool. It is the sibling of ``test_exit_plan_mode.py``:
both cover a Claude built-in tool that surfaces a structured card rather than
the binary policy ASK.
"""

from __future__ import annotations

import logging
import threading
import time

import httpx
import pytest
from playwright.sync_api import Page, expect

_log = logging.getLogger(__name__)

_APPROVAL_CARD = '[data-testid="approval-card"]'
_FORM = '[data-testid="ask-user-question-form"]'
_SUBMIT = '[data-testid="ask-user-question-submit"]'

_MOCK_ELICITATION_TIMEOUT_MS = 15_000

# The exact option labels used in the hook payload and form assertions.
_OPTION_ONE = "Alpha"
_OPTION_TWO = "Bravo"


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


@pytest.mark.timeout(90)
def test_ask_user_question_form_renders_and_submits(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Mock AskUserQuestion permission-request → web form renders → answer → prompt drains."""
    base_url, session_id = seeded_session
    _log.info("seeded session ready: base_url=%s session_id=%s", base_url, session_id)

    result_holder: dict = {}

    def _post_hook() -> None:
        try:
            resp = httpx.post(
                f"{base_url}/v1/sessions/{session_id}/hooks/permission-request",
                json={
                    "tool_name": "AskUserQuestion",
                    "tool_input": {
                        "questions": [
                            {
                                "question": "Which option do you prefer?",
                                "options": [_OPTION_ONE, _OPTION_TWO],
                            }
                        ]
                    },
                },
                timeout=60.0,
            )
            resp.raise_for_status()
            result_holder["response"] = resp.json()
        except Exception as exc:
            result_holder["error"] = exc

    hook_thread = threading.Thread(target=_post_hook, daemon=True)
    hook_thread.start()

    # Let the server park the elicitation before the SPA tries to render it.
    page.wait_for_timeout(500)

    page.goto(f"{base_url}/c/{session_id}")

    card = (
        page.locator(f'{_APPROVAL_CARD}[data-state="pending"]')
        .filter(has=page.locator(_FORM))
        .first
    )
    expect(card).to_be_visible(timeout=_MOCK_ELICITATION_TIMEOUT_MS)
    form = card.locator(_FORM)
    expect(form).to_be_visible(timeout=_MOCK_ELICITATION_TIMEOUT_MS)
    expect(form.get_by_text(_OPTION_ONE, exact=True)).to_be_visible()
    expect(form.get_by_text(_OPTION_TWO, exact=True)).to_be_visible()
    assert _pending_elicitations(base_url, session_id), "server has no parked elicitation"

    form.get_by_role("radio", name=_OPTION_ONE).check()
    form.locator(_SUBMIT).click()

    responded = page.locator(f'{_APPROVAL_CARD}[data-state="responded"]').first
    expect(responded).to_be_visible(timeout=_MOCK_ELICITATION_TIMEOUT_MS)

    hook_thread.join(timeout=30)
    if "error" in result_holder:
        raise AssertionError(f"hook thread failed: {result_holder['error']}") from result_holder[
            "error"
        ]

    _wait_for(lambda: not _pending_elicitations(base_url, session_id))
