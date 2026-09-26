"""A real permission webhook and SSE update lead to an accessible status explanation.

A native event clears the prompt without a verdict; no native CLI or model is used."""

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
# Allow the tooltip provider’s hover delay.
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
    """Read the info icon’s tooltip, title or accessible label; return empty if absent."""
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
        # The hook blocks until the native resolution event arrives.
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

    pending = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
    expect(pending).to_be_visible(timeout=_MOCK_ELICITATION_TIMEOUT_MS)
    expect(pending).to_contain_text(_PROMPT_MESSAGE)
    _wait_for(lambda: _pending_elicitations(base_url, session_id))

    # Native transcript watchers can report resolution without knowing the verdict.
    httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_elicitation_resolved",
            "data": {"elicitation_id": _ELICITATION_ID},
        },
        timeout=10.0,
    ).raise_for_status()

    responded = page.locator(f'{_APPROVAL_CARD}[data-state="responded"]').first
    expect(responded).to_be_visible(timeout=_MOCK_ELICITATION_TIMEOUT_MS)
    expect(responded).to_contain_text("Resolved elsewhere")

    hook_thread.join(timeout=30)
    assert not hook_thread.is_alive(), "native permission hook did not finish"
    assert not errors, f"native permission hook POST failed: {errors[0]}"
    _wait_for(lambda: not _pending_elicitations(base_url, session_id))

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
