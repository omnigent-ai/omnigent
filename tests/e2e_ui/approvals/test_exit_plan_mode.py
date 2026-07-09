r"""E2E: ExitPlanMode plan review renders and approves (mock, no real Claude Code).

A background thread POSTs directly to the server's
``POST /v1/sessions/{session_id}/hooks/permission-request`` endpoint with an
``ExitPlanMode`` payload. The server parks the elicitation (long-poll), the SPA
renders it as an ``ExitPlanModeReview`` inside an ``ApprovalCard``, and the test
approves it via the "Yes, manually approve edits" button. The parked HTTP call
then returns, confirming the verdict flowed back through the PermissionRequest
round-trip.

This approach replaces the original native Claude Code session (plan-mode boot,
real LLM planning turn) with a seeded session and a synthetic hook POST — no
real Claude Code needed, test completes in seconds rather than minutes.

This is the sibling of ``test_ask_user_question.py``: both cover a Claude
built-in tool that surfaces a structured card rather than the binary policy ASK.
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
_PLAN_REVIEW = '[data-testid="exit-plan-mode-review"]'

_MOCK_ELICITATION_TIMEOUT_MS = 15_000


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
def test_exit_plan_mode_review_renders_and_approves(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Mock ExitPlanMode permission-request → review card → approve → prompt drains."""
    base_url, session_id = seeded_session
    _log.info("seeded session ready: base_url=%s session_id=%s", base_url, session_id)

    result_holder: dict = {}

    def _post_hook() -> None:
        try:
            resp = httpx.post(
                f"{base_url}/v1/sessions/{session_id}/hooks/permission-request",
                json={
                    "tool_name": "ExitPlanMode",
                    "tool_input": {
                        "plan": (
                            "- Add comment `<!-- Maintained by the Platform team -->`"
                            " as the final line of `README.md`."
                        )
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

    card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
    expect(card).to_be_visible(timeout=_MOCK_ELICITATION_TIMEOUT_MS)
    review = card.locator(_PLAN_REVIEW)
    expect(review).to_be_visible(timeout=_MOCK_ELICITATION_TIMEOUT_MS)
    # The server is genuinely parked on the plan approval, not an optimistic UI.
    assert _pending_elicitations(base_url, session_id), "server has no parked elicitation"

    # Approve the plan ("manually approve edits" maps to a plain accept verdict).
    review.get_by_role("button", name="Yes, manually approve edits").click()

    # The card flips to its responded state and the parked prompt drains.
    responded = page.locator(f'{_APPROVAL_CARD}[data-state="responded"]').first
    expect(responded).to_be_visible(timeout=_MOCK_ELICITATION_TIMEOUT_MS)

    hook_thread.join(timeout=30)
    if "error" in result_holder:
        raise AssertionError(f"hook thread failed: {result_holder['error']}") from result_holder[
            "error"
        ]

    _wait_for(lambda: not _pending_elicitations(base_url, session_id))
