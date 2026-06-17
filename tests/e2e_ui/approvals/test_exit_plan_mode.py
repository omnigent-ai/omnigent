r"""E2E: native Claude's ExitPlanMode plan review renders and approves.

Claude Code in **plan mode** researches a task and then calls its built-in
``ExitPlanMode`` tool to present the plan for the user's approval. On a native
``claude-native`` session that call rides the ``PermissionRequest`` hook to the
Omnigent server, which detects the tool and stamps the full ``exit_plan_mode``
tool input (the plan markdown) onto the elicitation
(``server/routes/sessions.py``). The SPA renders it as ``ExitPlanModeReview``
inside the ``ApprovalCard`` — the plan plus plan-review actions ("use auto
mode", "manually approve edits", "reject with feedback"). This suite drives that
loop on a real plan-mode session: ask Claude to plan, then approve the plan in
the web review card.

Plan mode is set by launching the session with
``terminal_launch_args=["--permission-mode", "plan", "--disallowedTools",
"AskUserQuestion"]`` — see the ``native_claude_plan_session`` fixture. It is the
sibling of ``test_ask_user_question.py``: both cover a Claude built-in tool that
surfaces a structured card rather than the binary policy ASK. Rides a real
Claude Code boot, so it carries a 900s ceiling.

Two layers keep the model on the ExitPlanMode path (and off the clarifying
``AskUserQuestion`` path, which would surface the wrong card and time the test
out): the fixture disables ``AskUserQuestion`` outright, and the prompt pins the
exact comment text/location and forbids questions. The AskUserQuestion render
path keeps its own dedicated coverage in ``test_ask_user_question.py``, so
removing the tool here loses no coverage.

The load-bearing assertion is that the parked elicitation drains after the
review's approve — proof the verdict flowed back through the PermissionRequest
round-trip the planning Claude is blocked on.
"""

from __future__ import annotations

import logging
import time

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.messages.test_message_render_parity import _ensure_chat_view, _send
from tests.e2e_ui.messages.test_native_claude_render_parity import (
    _open_terminal_view,
    _wait_terminal_connected,
)

_log = logging.getLogger(__name__)

_APPROVAL_CARD = '[data-testid="approval-card"]'
_PLAN_REVIEW = '[data-testid="exit-plan-mode-review"]'

# A native Claude planning turn is a full agent loop (real LLM, research), far
# slower than a single custom-agent call — matches the render-parity budget.
_NATIVE_TURN_TIMEOUT_MS = 180_000

# The prompt pins the comment text and location so there is nothing for Claude
# to clarify, and explicitly forbids questions / mandates going straight to
# ExitPlanMode. Combined with the fixture's ``--disallowedTools
# AskUserQuestion`` (native_claude_plan_session), this keeps the model on the
# ExitPlanMode path the review card under test depends on.
#
# The pinned file/comment only have to be *plausible* targets for a plan: Claude
# is planning, never executing (the test never approves an edit), so the run is
# unaffected even if ``README.md`` is later renamed or removed from the repo
# root. The constant just keeps the prompt's intent explicit.
_PLAN_FILE = "README.md"
_PLAN_COMMENT = "<!-- Maintained by the Platform team -->"
_PLAN_PROMPT = (
    "You are in plan mode. Put together a short plan describing how you would "
    f"add the one-line comment `{_PLAN_COMMENT}` as the final line of "
    f"`{_PLAN_FILE}` in this repository. Do not ask any clarifying questions; "
    "if any detail is unspecified, assume a reasonable default and proceed. "
    "Then immediately use the ExitPlanMode tool to present that plan for my "
    "approval. Keep the plan to a few bullet points and do not make any edits "
    "yet."
)


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


@pytest.mark.timeout(900)
def test_exit_plan_mode_review_renders_and_approves(
    page: Page,
    native_claude_plan_session: tuple[str, str],
) -> None:
    """Plan-mode Claude calls ExitPlanMode → review card → approve → prompt drains."""
    base_url, session_id = native_claude_plan_session
    _log.info("native-claude plan session ready: base_url=%s session_id=%s", base_url, session_id)
    page.goto(f"{base_url}/c/{session_id}")

    # Confirm Claude Code actually booted in the session terminal (plan mode) before
    # asking it to plan.
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)

    # Ask Claude to produce a plan; in plan mode it presents it via ExitPlanMode,
    # which surfaces here as the plan-review card.
    _send(page, _PLAN_PROMPT)

    card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
    expect(card).to_be_visible(timeout=_NATIVE_TURN_TIMEOUT_MS)
    review = card.locator(_PLAN_REVIEW)
    expect(review).to_be_visible(timeout=30_000)
    # The server is genuinely parked on the plan approval, not an optimistic UI.
    assert _pending_elicitations(base_url, session_id), "server has no parked elicitation"

    # Approve the plan ("manually approve edits" maps to a plain accept verdict).
    review.get_by_role("button", name="Yes, manually approve edits").click()

    # The card flips to its responded state and the parked prompt drains — the
    # verdict reached the blocked ExitPlanMode call.
    responded = page.locator(f'{_APPROVAL_CARD}[data-state="responded"]').first
    expect(responded).to_be_visible(timeout=_NATIVE_TURN_TIMEOUT_MS)
    _wait_for(lambda: not _pending_elicitations(base_url, session_id))
