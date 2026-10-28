"""E2E: an approval answered in the session must promptly clear the sidebar/inbox.

Elicitations are slow to clear from the sidebar / inbox after being
answered in the session: answering an ApprovalCard in the chat
(``chatStore.submitApproval``) resolves the server-side prompt immediately —
the card flips to "Approved" and the parked elicitation drains from the
session snapshot — but nothing refreshes the conversations cache that drives
the sidebar row's "Needs response" badge and the Inbox counter. Those wait for
the session-updates socket's next interval re-scan (4s; longer on
multi-replica deploys, where the row's persisted count mirror lags the resolve
under ``max()``), so the session keeps advertising a prompt that no longer
exists.

The InboxPage answer path documents the intended UX for the same verdict:
"Success invalidates the session list so the row's count (and the sidebar
badge) drop without waiting for the socket." This test pins that expectation
onto the in-chat answer path: once the server has drained the prompt, both
markers must clear well inside one re-scan interval.

The prompt is parked via the synthetic claude-native PermissionRequest hook —
no LLM, same PR-lane trigger as ``test_approval_card.py``. The answer is
clicked immediately after the badge's own re-scan frame arrives, so under the
bug the clearing frame is a full interval away and the budget assertion cannot
false-pass off a lucky tick phase.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

_APPROVAL_CARD = '[data-testid="approval-card"]'

# The prompt must surface in the UI well within this: the card arrives over
# live SSE almost immediately; the sidebar markers on the next socket re-scan.
_SURFACE_TIMEOUT_MS = 15_000

# How long the answered prompt may keep its sidebar / inbox markers once the
# server has drained it. Generous for a prompt clear (the intended UX drops
# the markers without waiting for the socket) while far below the 4s re-scan
# interval the bug makes the UI wait for.
_CLEAR_BUDGET_S = 2.0

# Past this the markers are simply stuck, not merely slow.
_CLEAR_POLL_CEILING_S = 15.0


def _pending_elicitations(base_url: str, session_id: str) -> list[dict]:
    """Return the session snapshot's parked elicitation events (owner view)."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json().get("pending_elicitations") or []


def _row(page: Page, session_id: str) -> Locator:
    """Locate the sidebar row (``<li>``) for *session_id* by its href."""
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def _inbox_counter_value(counter: Locator) -> int:
    """Read the sidebar Inbox counter, ``0`` when the badge is unmounted."""
    if counter.count() == 0:
        return 0
    text = (counter.first.text_content() or "").strip()
    return int(text) if text.isdigit() else 0


def _wait_until(
    predicate: Callable[[], bool], *, timeout_s: float, interval_s: float = 0.1
) -> float:
    """Poll *predicate* until truthy; return the moment it first held."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return time.monotonic()
        time.sleep(interval_s)
    raise AssertionError(f"condition not met within {timeout_s:.0f}s")


@pytest.mark.timeout(180)
def test_answered_approval_clears_sidebar_and_inbox_promptly(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Answering an approval in the chat must promptly drop both sidebar markers.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound (idle) session.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    row = _row(page, session_id)
    expect(row).to_be_visible(timeout=30_000)

    inbox_counter = page.get_by_test_id("inbox-button").locator('span[aria-label*="inbox item"]')
    # Tolerate marker pollution from earlier tests in the shard: "cleared"
    # below means back to this baseline, not necessarily unmounted.
    baseline_inbox = _inbox_counter_value(inbox_counter)

    # Park a real server-side elicitation (claude-native PermissionRequest
    # hook — no LLM). The POST long-polls until the verdict, so it rides a
    # thread; errors surface after the assertions.
    result_holder: dict = {}

    def _post_hook() -> None:
        try:
            resp = httpx.post(
                f"{base_url}/v1/sessions/{session_id}/hooks/permission-request",
                json={"tool_name": "Bash", "tool_input": {"command": "git push origin main"}},
                timeout=120.0,
            )
            resp.raise_for_status()
            result_holder["response"] = resp.json()
        except Exception as exc:
            result_holder["error"] = exc

    hook_thread = threading.Thread(target=_post_hook, daemon=True)
    hook_thread.start()

    # The prompt reaches the open chat over live SSE almost immediately...
    card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
    expect(card).to_be_visible(timeout=_SURFACE_TIMEOUT_MS)

    # ...and the sidebar catches up on the next socket re-scan: the row's
    # "Needs response" badge and the Inbox counter both advertise the prompt.
    badge = row.locator('[data-testid="session-state-badge"][data-state="awaiting"]')
    expect(badge).to_be_visible(timeout=_SURFACE_TIMEOUT_MS)
    _wait_until(
        lambda: _inbox_counter_value(inbox_counter) > baseline_inbox,
        timeout_s=_SURFACE_TIMEOUT_MS / 1000,
    )

    # Answer the prompt in the session — right after the badge frame, so the
    # next re-scan is a full interval away and can't mask a missing prompt
    # clear. The card flips to its responded state on the spot.
    card.get_by_role("button", name="Approve", exact=True).click()
    responded = page.locator(f'{_APPROVAL_CARD}[data-state="responded"]').first
    expect(responded).to_be_visible(timeout=_SURFACE_TIMEOUT_MS)

    # The server settles the verdict quickly: the parked prompt drains from
    # the session snapshot. From this moment the sidebar markers advertise a
    # prompt that no longer exists anywhere.
    server_drained_at = _wait_until(
        lambda: not _pending_elicitations(base_url, session_id), timeout_s=10.0
    )

    # Measure how long each marker keeps advertising the answered prompt.
    badge_cleared_at: float | None = None
    counter_cleared_at: float | None = None
    deadline = server_drained_at + _CLEAR_POLL_CEILING_S
    while time.monotonic() < deadline and (badge_cleared_at is None or counter_cleared_at is None):
        if badge_cleared_at is None and badge.count() == 0:
            badge_cleared_at = time.monotonic()
        if counter_cleared_at is None and _inbox_counter_value(inbox_counter) <= baseline_inbox:
            counter_cleared_at = time.monotonic()
        page.wait_for_timeout(50)

    hook_thread.join(timeout=30)
    if "error" in result_holder:
        raise AssertionError(f"hook thread failed: {result_holder['error']}") from result_holder[
            "error"
        ]

    def _describe(cleared_at: float | None) -> str:
        if cleared_at is None:
            return f"past the {_CLEAR_POLL_CEILING_S:.0f}s poll ceiling"
        return f"for {cleared_at - server_drained_at:.2f}s"

    failures = []
    if badge_cleared_at is None or badge_cleared_at - server_drained_at > _CLEAR_BUDGET_S:
        failures.append(
            "sidebar 'Needs response' badge kept advertising the answered "
            f"prompt {_describe(badge_cleared_at)}"
        )
    if counter_cleared_at is None or counter_cleared_at - server_drained_at > _CLEAR_BUDGET_S:
        failures.append(
            f"sidebar Inbox counter kept counting the answered prompt "
            f"{_describe(counter_cleared_at)}"
        )
    assert not failures, (
        f"answered elicitation stayed stale past the {_CLEAR_BUDGET_S:.1f}s budget: "
        + "; ".join(failures)
    )
