r"""E2E: the persistent "don't ask again" approval button approves a scope once.

When Claude Code calls a non-edit tool under a prompting permission mode (e.g.
``WebFetch`` in the default mode), the native ``PermissionRequest`` hook
forwards the call to the Omnigent server, which stamps a ``remember_scope``
extra onto the elicitation (``server/routes/sessions.py`` —
``_allow_remember_eligible`` / ``_claude_native_remember_host``) and publishes
it. The SPA renders a third button on the binary ``ApprovalCard``
(``ap-web/src/components/blocks/ApprovalCard.tsx``): **"Approve & don't ask
again for {host|tool}"**. Accepting through it sends ``content.remember`` back;
the server re-derives the scope and echoes an ``addRules`` permission update so
same-scope calls stop prompting — the web equivalent of Claude Code's native
"don't ask again" option.

This is the persistent-allow-rule counterpart to ``test_ask_user_question.py``
(Claude's question tool) and ``test_exit_plan_mode.py`` (the plan card): all
three cover a claude-native ``PermissionRequest`` that surfaces a richer card
than the binary policy ASK. It rides the same ``native_claude_session`` fixture
— a real Claude Code boots in the session terminal — so it carries a 900s
ceiling, and the prompt is explicit because the test depends on the model
actually reaching for ``WebFetch``.

The scope exercised is the **WebFetch domain** case: the rule (and the button /
responded label) is scoped to the request host, ``github.com``. The tool-wide
fallback (no host → a rule for the whole tool) is covered by the server
integration tests
(``tests/server/integration/test_sessions_permission_request_hook.py``) and the
ApprovalCard unit tests (``ap-web/.../ApprovalCard.test.tsx``); like the sibling
suites, this test drives exactly one forced tool call per real Claude boot to
stay deterministic.

The load-bearing assertion is that the parked elicitation drains after the
remember-click — proof the ``remember`` verdict (and the ``addRules`` update it
carries) flowed back through the PermissionRequest round-trip Claude is blocked
on, not a detached UI action.
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
_REMEMBER_BUTTON = '[data-testid="approval-card-remember"]'

# A native Claude turn is a full agent loop (real LLM, a tool call), far slower
# than a single custom-agent call — matches the sibling approval suites' budget.
_NATIVE_TURN_TIMEOUT_MS = 180_000

# WebFetch on a stable, well-known repo URL: the host the server scopes the
# domain rule to is ``github.com``. The fetch never has to succeed — the
# PermissionRequest fires on the tool *call*, before any network egress — so the
# exact path is irrelevant to the assertion.
_WEBFETCH_URL = "https://github.com/cli/cli"
_WEBFETCH_HOST = "github.com"
_WEBFETCH_PROMPT = (
    f"Use the WebFetch tool right now to fetch {_WEBFETCH_URL} with the prompt "
    '"summarize this page". Do not call any other tool first and do not ask any '
    "questions — call WebFetch immediately."
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
def test_persistent_approval_remembers_webfetch_domain(
    page: Page,
    native_claude_session: tuple[str, str],
) -> None:
    """WebFetch → domain "don't ask again" button → click → responded + drain."""
    base_url, session_id = native_claude_session
    _log.info("native-claude session ready: base_url=%s session_id=%s", base_url, session_id)
    page.goto(f"{base_url}/c/{session_id}")

    # Confirm Claude Code actually booted in the session terminal before asking
    # it to do anything (the runner's claude-native auto-launch).
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)

    # Ask Claude to call WebFetch; its call rides the PermissionRequest hook to
    # the server, which stamps the WebFetch ``remember_scope`` (host github.com)
    # and surfaces the persistent-approval card here.
    _send(page, _WEBFETCH_PROMPT)

    # Scope the wait to the pending card that actually carries the remember
    # button (filter by ``has=``), not ``.first`` pending card — if Claude ever
    # surfaces an unrelated approval first, ``.first`` would latch onto it and
    # fail even though the right card appears moments later. This does not weaken
    # the assertion: if the remember button never renders, no such card appears
    # and the test still times out and fails.
    card = (
        page.locator(f'{_APPROVAL_CARD}[data-state="pending"]')
        .filter(has=page.locator(_REMEMBER_BUTTON))
        .first
    )
    expect(card).to_be_visible(timeout=_NATIVE_TURN_TIMEOUT_MS)
    # The server is genuinely parked on this prompt, not an optimistic UI.
    assert _pending_elicitations(base_url, session_id), "server has no parked elicitation"

    remember = card.locator(_REMEMBER_BUTTON)
    # Visible label: "Approve & don't ask again for github.com" (domain-scoped).
    expect(remember).to_contain_text(f"don't ask again for {_WEBFETCH_HOST}")
    # The tooltip spells out the (session-scoped) domain grant explicitly.
    assert (
        remember.get_attribute("title")
        == f"Won't ask again for {_WEBFETCH_HOST} for the rest of this session"
    )

    remember.click()

    # The card flips to its responded state with the persistent-approval label —
    # "Approved · won't ask again for github.com".
    responded = page.locator(f'{_APPROVAL_CARD}[data-state="responded"]').filter(
        has_text=f"won't ask again for {_WEBFETCH_HOST}"
    )
    expect(responded.first).to_be_visible(timeout=_NATIVE_TURN_TIMEOUT_MS)
    # The parked prompt drains — the remember verdict reached the blocked
    # WebFetch call, carrying the addRules update back to Claude Code.
    _wait_for(lambda: not _pending_elicitations(base_url, session_id))
