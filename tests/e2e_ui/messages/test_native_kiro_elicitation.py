"""E2E: Kiro-native tool approvals surface as Chat approval cards."""

from __future__ import annotations

import os
import time

import httpx
import pytest
from playwright.sync_api import Page, expect

from .test_native_kiro_render_parity import (
    _ASSISTANT,
    _WORKING,
    _ensure_chat_view,
    _kiro_unavailable_reason,
    _send,
)

_APPROVAL_CARD = '[data-testid="approval-card"]'
_NATIVE_TURN_TIMEOUT_MS = 180_000


_KIRO_SKIP_REASON = _kiro_unavailable_reason()

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_KIRO_NATIVE") != "1" or _KIRO_SKIP_REASON is not None,
    reason=(
        _KIRO_SKIP_REASON
        or "native Kiro approval e2e needs an interactive Kiro login; "
        "set OMNIGENT_E2E_KIRO_NATIVE=1"
    ),
)


def _pending_elicitations(base_url: str, session_id: str) -> list[dict]:
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json().get("pending_elicitations") or []


def _wait_for(predicate, *, timeout_s: float = 30.0, interval_s: float = 0.5) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    raise AssertionError("condition not met within timeout")


@pytest.mark.timeout(900)
def test_native_kiro_tool_approval_card_approves(
    page: Page,
    native_kiro_session: tuple[str, str],
) -> None:
    """Kiro TUI permission prompt -> Chat card -> web approve -> Kiro continues."""
    base_url, session_id = native_kiro_session

    page.goto(f"{base_url}/c/{session_id}")
    _ensure_chat_view(page)
    # Do not ask the model to echo a confirmation token. A safety-conscious model
    # reads "after the approved command, output this exact token" as an attempt to
    # forge an approval signal and refuses, which fails the turn even though the
    # approval loop worked. Prove continuation structurally instead: once the gate
    # is released the turn runs the tool, replies, and finishes.
    _send(page, "Use the shell tool to run `pwd`, then briefly report the result.")

    card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
    expect(card).to_be_visible(timeout=_NATIVE_TURN_TIMEOUT_MS)
    expect(card.get_by_text("Approval required")).to_be_visible()
    expect(card.get_by_text("Kiro", exact=False).first).to_be_visible()
    assert _pending_elicitations(base_url, session_id), "server has no parked Kiro elicitation"

    card.get_by_role("button", name="Approve").click()

    responded = page.locator(f'{_APPROVAL_CARD}[data-state="responded"]').first
    expect(responded).to_be_visible(timeout=30_000)
    expect(responded.get_by_text("Approved", exact=False).first).to_be_visible()
    _wait_for(lambda: not _pending_elicitations(base_url, session_id))
    # Approval released the gate: Kiro produces an assistant reply and the turn
    # ends (no lingering working indicator).
    expect(page.locator(_ASSISTANT).last).to_be_visible(timeout=_NATIVE_TURN_TIMEOUT_MS)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_NATIVE_TURN_TIMEOUT_MS)
