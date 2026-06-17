"""E2E: ExitPlanMode approval updates the permission-mode badge in real time.

Verifies that when the server processes an ExitPlanMode PermissionRequest and
stamps a ``setMode=auto`` update, the ``session.mode`` SSE event reaches the
web UI and renders the "Auto mode" badge in ``ComposerStatusLine`` without a
page reload.

Uses the synthetic hook-injection path
(``POST /v1/sessions/{id}/hooks/permission-request``) to avoid requiring a
real Claude Code boot: the approval card renders from the server's elicitation
publish, which is the same code path native claude uses.

The load-bearing assertion is ``[data-testid="composer-permission-mode"]``
appearing with text "Auto mode" after clicking "Accept & allow all edits" —
evidence that:
  1. The server built the ``setMode=auto`` verdict.
  2. The ``session.mode`` SSE event was published and reached the browser.
  3. ``chatStore`` stored the new mode value.
  4. ``ComposerStatusLine`` rendered the badge from that value.
"""

from __future__ import annotations

import threading

import httpx
import pytest
from playwright.sync_api import Page, expect

_APPROVAL_CARD = '[data-testid="approval-card"]'
_COMPOSER_MODE = '[data-testid="composer-permission-mode"]'

_HOOK_TIMEOUT_S = 20.0


def _inject_exit_plan_mode(base_url: str, session_id: str) -> None:
    """POST an ExitPlanMode permission-request in a background thread.

    The call blocks until the elicitation is answered (or times out), so it
    must not run on the main thread while the Playwright driver is waiting for
    the approval card to appear.
    """
    httpx.post(
        f"{base_url}/v1/sessions/{session_id}/hooks/permission-request",
        json={"tool_name": "ExitPlanMode", "permission_mode": "plan", "tool_input": {}},
        timeout=_HOOK_TIMEOUT_S,
    )


@pytest.mark.timeout(120)
def test_permission_mode_badge_updates_on_exit_plan_mode_approval(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Accept ExitPlanMode with auto mode → 'Auto mode' badge renders live.

    Flow:
    1. Navigate to the session page (SSE subscription established).
    2. Inject a synthetic ExitPlanMode permission request (background thread).
    3. Approval card appears in the chat view.
    4. Click "Accept & allow all edits" (server maps this to setMode=auto for
       ExitPlanMode, then publishes a session.mode SSE event).
    5. The "Auto mode" badge appears in ComposerStatusLine — live, no reload.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.locator("main")).to_be_visible(timeout=15_000)

    # Inject the ExitPlanMode hook request concurrently — the POST blocks
    # until the elicitation is answered, so it runs on a daemon thread.
    thread = threading.Thread(
        target=_inject_exit_plan_mode,
        args=(base_url, session_id),
        daemon=True,
    )
    thread.start()

    # The approval card must appear (server published the elicitation over SSE).
    card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
    expect(card).to_be_visible(timeout=15_000)

    # "Accept & allow all edits" on ExitPlanMode → server emits setMode=auto
    # → publishes session.mode SSE event → chatStore updates permissionMode.
    card.get_by_role("button", name="Accept & allow all edits").click()

    # Badge must appear in ComposerStatusLine without a page reload.
    badge = page.locator(_COMPOSER_MODE)
    expect(badge).to_be_visible(timeout=10_000)
    expect(badge).to_have_text("Auto mode")

    thread.join(timeout=10.0)
