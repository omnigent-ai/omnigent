r"""E2E: native Claude's built-in AskUserQuestion renders as a web form.

When Claude Code calls its built-in ``AskUserQuestion`` tool, the native
``PermissionRequest`` hook forwards the call to the Omnigent server, which
detects the tool, stamps the structured ``ask_user_question`` payload onto the
elicitation (``_structured_ask_user_question`` in
``server/routes/sessions.py``), and publishes it. The SPA renders that payload
as an ``AskUserQuestionForm`` inside the ``ApprovalCard`` — radio inputs for a
single-select question, a Submit that posts the gathered answers back as the
elicitation verdict. This suite drives that loop end-to-end on a real
``claude-native`` session: ask Claude to pose a two-option question, answer it
in the web form, and assert the parked prompt drains.

This is the structured-form counterpart to the binary approval card
(``test_approval_card.py``): the binary card covers a policy ASK, this covers
Claude's own question tool. It rides the same ``native_claude_session`` fixture
as the native render-parity suite — a real Claude Code boots in the session
terminal — so it carries a 900s ceiling, and the prompt is explicit because it
depends on the model actually reaching for ``AskUserQuestion``.

The load-bearing assertion is that the server's parked elicitation drains after
the form submits — proof the web answer flowed back through the same
PermissionRequest round-trip Claude is blocked on, not a detached UI action.
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
_FORM = '[data-testid="ask-user-question-form"]'
_SUBMIT = '[data-testid="ask-user-question-submit"]'

# A native Claude turn is a full agent loop (real LLM, a tool call), far slower
# than a single custom-agent call — matches the render-parity suite's budget.
_NATIVE_TURN_TIMEOUT_MS = 180_000

# The exact AskUserQuestion shape the prompt pins, so the form assertions are
# deterministic regardless of how Claude phrases the surrounding prose.
_OPTION_ONE = "Alpha"
_OPTION_TWO = "Bravo"
_ASK_PROMPT = (
    "Use the AskUserQuestion tool right now to ask me exactly one question. "
    'Set the question text to "Which option do you prefer?" and offer exactly '
    f'two options with the labels "{_OPTION_ONE}" and "{_OPTION_TWO}". Do not '
    "ask anything else, do not call any other tool, and do not answer the "
    "question yourself."
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
def test_ask_user_question_form_renders_and_submits(
    page: Page,
    native_claude_session: tuple[str, str],
) -> None:
    """Claude calls AskUserQuestion → web form renders → answer → prompt drains."""
    base_url, session_id = native_claude_session
    _log.info("native-claude session ready: base_url=%s session_id=%s", base_url, session_id)
    page.goto(f"{base_url}/c/{session_id}")

    # Confirm Claude Code actually booted in the session terminal before asking
    # it to do anything (the runner's claude-native auto-launch).
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)

    # Ask Claude to pose the two-option question; its AskUserQuestion call rides
    # the PermissionRequest hook to the server and surfaces as a form here.
    _send(page, _ASK_PROMPT)

    # Scope the wait to the pending approval card that *contains* the
    # AskUserQuestion form, rather than grabbing ``.first`` pending card and
    # then asserting the form inside it. The prompt forbids other tool calls,
    # but if Claude ever calls an approval-requiring tool first (the same
    # degree of freedom that flaked test_exit_plan_mode.py the other way),
    # ``.first`` would latch onto that unrelated card and fail even though the
    # question card appears moments later. Filtering by ``has=`` waits for the
    # right card specifically. This does not weaken the assertion: if Claude
    # never calls AskUserQuestion, no such card ever appears and the test still
    # times out and fails.
    card = (
        page.locator(f'{_APPROVAL_CARD}[data-state="pending"]')
        .filter(has=page.locator(_FORM))
        .first
    )
    expect(card).to_be_visible(timeout=_NATIVE_TURN_TIMEOUT_MS)
    form = card.locator(_FORM)
    expect(form).to_be_visible(timeout=30_000)
    expect(form.get_by_text(_OPTION_ONE, exact=True)).to_be_visible()
    expect(form.get_by_text(_OPTION_TWO, exact=True)).to_be_visible()
    # The server is genuinely parked on this question, not an optimistic UI.
    assert _pending_elicitations(base_url, session_id), "server has no parked elicitation"

    # Answer the (single-select) question and submit.
    form.get_by_role("radio", name=_OPTION_ONE).check()
    form.locator(_SUBMIT).click()

    # The card flips to its responded state and the parked prompt drains —
    # the answer reached the blocked Claude tool call.
    responded = page.locator(f'{_APPROVAL_CARD}[data-state="responded"]').first
    expect(responded).to_be_visible(timeout=_NATIVE_TURN_TIMEOUT_MS)
    _wait_for(lambda: not _pending_elicitations(base_url, session_id))
