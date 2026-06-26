r"""E2E: a pending elicitation card floats to the bottom of the chat.

Regression coverage for the float-to-bottom behavior. A pending elicitation
card is lifted out of its inline transcript position and re-rendered as the
last item in the chat scroll flow — wrapped in a ``bottom-elicitation``
Message — so an outstanding question stays in view no matter how much text the
agent streams after it. Once answered, the card leaves the floated slot and
returns inline showing its ``responded`` state.

Like ``test_ask_user_question.py``, this drives a synthetic permission-request
hook against a seeded session (no real LLM turn), so it completes in seconds.
The distinction from that sibling: this test asserts WHERE the card renders
(inside the floated ``bottom-elicitation`` wrapper while pending, and no longer
wrapped once answered), which is exactly the behavior the float-to-bottom
change introduced.
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
_BOTTOM = '[data-testid="bottom-elicitation"]'
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
def test_pending_elicitation_floats_to_bottom(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Pending card renders in the floated bottom slot, then returns inline once answered."""
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

    # While pending, the card lives INSIDE the floated bottom-elicitation
    # wrapper — not in its inline transcript slot. Scoping the locator to the
    # wrapper is the assertion that distinguishes float-to-bottom from the old
    # inline rendering.
    floated_card = page.locator(f'{_BOTTOM} {_APPROVAL_CARD}[data-state="pending"]').first
    expect(floated_card).to_be_visible(timeout=_MOCK_ELICITATION_TIMEOUT_MS)
    form = floated_card.locator(_FORM)
    expect(form).to_be_visible(timeout=_MOCK_ELICITATION_TIMEOUT_MS)
    assert _pending_elicitations(base_url, session_id), "server has no parked elicitation"

    # Answer it.
    form.get_by_role("radio", name=_OPTION_ONE).check()
    form.locator(_SUBMIT).click()

    # Answered: the card flips to responded, leaves the floated slot, and the
    # bottom-elicitation wrapper disappears (it returns inline).
    responded = page.locator(f'{_APPROVAL_CARD}[data-state="responded"]').first
    expect(responded).to_be_visible(timeout=_MOCK_ELICITATION_TIMEOUT_MS)
    expect(page.locator(_BOTTOM)).to_have_count(0)

    hook_thread.join(timeout=30)
    if "error" in result_holder:
        raise AssertionError(f"hook thread failed: {result_holder['error']}") from result_holder[
            "error"
        ]

    _wait_for(lambda: not _pending_elicitations(base_url, session_id))
