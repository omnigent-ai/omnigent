r"""E2E: the "Resolved elsewhere" approval pill explains itself on hover.

When a harness-native permission prompt mirrored into the web chat is
answered *outside* the card (the vendor TUI, another tab, the standalone
approve page), the server publishes ``response.elicitation_resolved`` and
the chat store flips the pending ``ApprovalCard`` to its ``auto_resolved``
state — the "Resolved elsewhere" pill with an info (ⓘ) icon
(``web/src/components/blocks/ApprovalCard.tsx``).

That pill is the only place the state is surfaced, and the ⓘ icon implies
hover-for-detail — so the pill must actually explain what "Resolved
elsewhere" means on hover (a tooltip, or an accessible ``title`` /
``aria-label``). Regression guard for the bug where the icon showed
nothing at all and the status was left unexplained.

The journey is driven exactly like the shipped native-permission suites
(``test_persistent_approval.py``): a background thread POSTs the generic
``hooks/native-permission-request`` webhook (stamped as a Cursor prompt,
matching the reporter's harness), the web tab renders the pending card,
and then a *different* client — plain ``httpx``, standing in for the TUI
mirror / another tab — resolves the same elicitation via the URL resolve
endpoint. The open tab receives ``response.elicitation_resolved`` over SSE
and flips the card to "Resolved elsewhere" without ever submitting a
verdict itself. No LLM, no native CLI — seconds, runs on every PR.
"""

from __future__ import annotations

import threading
import time

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

_APPROVAL_CARD = '[data-testid="approval-card"]'
_MOCK_ELICITATION_TIMEOUT_MS = 15_000
_ELICITATION_ID = "elic_answered_elsewhere"
_PROMPT_MESSAGE = "Cursor wants approval to run a shell command"
# Radix tooltips open after the provider's 600ms hover delay; 5s is
# generous without stalling the failure case for long.
_TOOLTIP_WAIT_MS = 5_000


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


def _hover_status_detail(page: Page, card: Locator, icon: Locator) -> str:
    """Hover the pill's info icon and return whatever detail it offers.

    Accepts any of the accessible channels a fix could reasonably use:
    a Radix tooltip (``role="tooltip"``, rendered into a portal outside
    the card), or a native ``title`` / ``aria-label`` on the pill's
    title row. Returns ``""`` when the icon offers nothing — the bug.

    :param page: The Playwright page (tooltip portals mount at the root).
    :param card: The responded approval card locator.
    :param icon: The ⓘ icon inside the card's title row.
    :returns: The explanation text, or ``""`` when none exists.
    """
    icon.hover()
    tooltip = page.get_by_role("tooltip").first
    try:
        tooltip.wait_for(state="visible", timeout=_TOOLTIP_WAIT_MS)
        return (tooltip.inner_text() or "").strip()
    except Exception:
        pass
    title_row = card.locator('[data-slot="alert-title"]').first
    for attr in ("title", "aria-label"):
        holders = title_row.locator(f"[{attr}]")
        for i in range(holders.count()):
            value = (holders.nth(i).get_attribute(attr) or "").strip()
            if value:
                return value
        row_value = (title_row.get_attribute(attr) or "").strip()
        if row_value:
            return row_value
    return ""


@pytest.mark.timeout(120)
def test_resolved_elsewhere_pill_explains_itself(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Prompt answered elsewhere → "Resolved elsewhere" pill → ⓘ explains it."""
    base_url, session_id = seeded_session
    errors: list[Exception] = []

    def _post_hook() -> None:
        # Parks server-side until the elicitation resolves; the accept
        # verdict below unblocks it, so the thread exits with the test.
        try:
            httpx.post(
                f"{base_url}/v1/sessions/{session_id}/hooks/native-permission-request",
                json={
                    "elicitation_id": _ELICITATION_ID,
                    "agent": "Cursor",
                    "policy_name": "cursor_native_permission",
                    "message": _PROMPT_MESSAGE,
                },
                timeout=120.0,
            ).raise_for_status()
        except Exception as exc:  # surfaced after the join below
            errors.append(exc)

    hook_thread = threading.Thread(target=_post_hook, daemon=True)
    hook_thread.start()
    # Let the server park the elicitation before the SPA renders.
    page.wait_for_timeout(500)

    page.goto(f"{base_url}/c/{session_id}")

    # The mirrored prompt renders as a pending approval card, and the
    # server is genuinely parked on it.
    pending = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
    expect(pending).to_be_visible(timeout=_MOCK_ELICITATION_TIMEOUT_MS)
    expect(pending).to_contain_text(_PROMPT_MESSAGE)
    _wait_for(lambda: _pending_elicitations(base_url, session_id))

    # Answer it ELSEWHERE: a different client (the TUI mirror / another
    # tab) hits the URL resolve endpoint. This tab never submits, so the
    # SSE `response.elicitation_resolved` flips its card to the neutral
    # auto-resolved state.
    httpx.post(
        f"{base_url}/v1/sessions/{session_id}/elicitations/{_ELICITATION_ID}/resolve",
        json={"action": "accept"},
        timeout=10.0,
    ).raise_for_status()

    responded = page.locator(f'{_APPROVAL_CARD}[data-state="responded"]').first
    expect(responded).to_be_visible(timeout=_MOCK_ELICITATION_TIMEOUT_MS)
    expect(responded).to_contain_text("Resolved elsewhere")

    hook_thread.join(timeout=30)
    assert not errors, f"native permission hook POST failed: {errors[0]}"
    _wait_for(lambda: not _pending_elicitations(base_url, session_id))

    # The pill renders an info (ⓘ) icon — hovering it must explain what
    # "Resolved elsewhere" means, not silently do nothing.
    info_icon = responded.locator("svg.lucide-info").first
    expect(info_icon).to_be_visible()

    detail = _hover_status_detail(page, responded, info_icon)
    assert detail, (
        'the "Resolved elsewhere" pill\'s ⓘ icon offers no hover detail: '
        "no tooltip appeared and no title/aria-label explains the status"
    )
    assert detail.lower() != "resolved elsewhere", (
        "the ⓘ icon's hover detail just repeats the label instead of "
        f"explaining the status: {detail!r}"
    )
