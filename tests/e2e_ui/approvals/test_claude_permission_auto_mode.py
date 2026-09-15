"""E2E: regular Claude permission cards must offer "Approve & switch to auto mode".

A regular Claude-native permission prompt (the ``PermissionRequest`` hook for
an ordinary gated tool such as ``Write``) renders the binary ``ApprovalCard``
in chat and in the Inbox. Plan-review cards (``ExitPlanMode``) already offer
an auto-mode approval; the regular card must offer a session-scoped
**Approve & switch to auto mode** action too, and honoring it must echo a
``setMode(auto, session)`` permission update in the hook decision so Claude
Code switches the session into its ``auto`` permission mode.

Journey (mirrors the reported repro): a Claude Code session in Manual
(``default``) permission mode asks to create a file -> the PermissionRequest
hook parks the prompt (driven here as a synthetic hook POST with the exact
body the real hook subprocess sends, the same fast pattern as
``test_exit_plan_mode.py`` / ``test_inbox_approval.py`` -- no native CLI
required) -> the SPA renders the pending permission card -> the card must
offer "Approve & switch to auto mode" -> choosing it resolves the parked hook
with ``behavior=allow`` plus ``updatedPermissions=[{setMode, auto, session}]``.

On the buggy build the pending card renders only one-off approval, the
narrower "Accept & allow all edits" mode switch, and Reject -- no auto-mode
action in chat or Inbox -- so the auto-mode button expectation is the failing
assertion in both tests.
"""

from __future__ import annotations

import contextlib
import secrets
import threading
import time
from functools import partial

import httpx
import pytest
from playwright.sync_api import Page, expect

_APPROVAL_CARD = '[data-testid="approval-card"]'
_INBOX_ITEM = '[data-testid="inbox-item"]'
_AUTO_MODE_LABEL = "Approve & switch to auto mode"
_CARD_TIMEOUT_MS = 30_000
# The action row renders with the card itself, so once the pending card is
# visible a short window is enough for the button to appear; this keeps the
# failing run (and its recording) tight when the affordance is missing.
_AUTO_BUTTON_TIMEOUT_MS = 8_000
_EXPECTED_AUTO_MODE_PERMISSIONS = [{"type": "setMode", "mode": "auto", "destination": "session"}]


def _permission_hook_payload(elicitation_id: str) -> dict:
    """Claude PermissionRequest hook body for a file-creating ``Write`` call.

    Mirrors the ``omnigent claude`` wrapper's hook subprocess: the
    ``_omnigent_elicitation_id`` is minted once per prompt and re-sent on every
    retry POST so the server re-parks the same elicitation. ``permission_mode``
    is ``default`` -- the Manual mode where a regular tool call still prompts,
    exactly the mode the auto-mode affordance is for.

    :param elicitation_id: ``elicit_claude_`` + 32 hex chars.
    :returns: JSON-serializable PermissionRequest payload.
    """
    return {
        "session_id": "claude_sess_e2e",
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/tmp/cwd",
        "permission_mode": "default",
        "hook_event_name": "PermissionRequest",
        "tool_name": "Write",
        "tool_input": {"file_path": "permission-auto-check.txt", "content": "hello"},
        "tool_use_id": "tool_use_e2e",
        "_omnigent_elicitation_id": elicitation_id,
    }


def _pending_elicitations(base_url: str, session_id: str) -> list[dict]:
    """Return the session snapshot's pending elicitation events (owner view)."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json().get("pending_elicitations") or []


def _is_parked(base_url: str, session_id: str, elicitation_id: str) -> bool:
    """True once the server snapshot lists *elicitation_id* as pending."""
    return any(
        e.get("elicitation_id") == elicitation_id
        for e in _pending_elicitations(base_url, session_id)
    )


def _wait_for(predicate, *, timeout_s: float = 30.0, interval_s: float = 0.5) -> None:
    """Poll *predicate* until truthy or the deadline passes."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    raise AssertionError("condition not met within timeout")


def _park_permission_hook(
    base_url: str,
    session_id: str,
    elicitation_id: str,
    sink: dict,
) -> None:
    """Long-poll the permission hook in a worker thread; stash the verdict.

    The endpoint parks the elicitation (publishing the SSE the card renders
    from) and blocks until the web UI delivers a verdict -- exactly what the
    real hook subprocess does. Run from a thread so the test thread can drive
    Playwright; the verdict response (or any error) lands in *sink*.

    :param base_url: Live server base URL.
    :param session_id: Owning session id.
    :param elicitation_id: Stable id to park.
    :param sink: Mutable dict; gets ``"resp"`` (httpx.Response) or
        ``"error"`` (Exception).
    """
    try:
        sink["resp"] = httpx.post(
            f"{base_url}/v1/sessions/{session_id}/hooks/permission-request",
            json=_permission_hook_payload(elicitation_id),
            timeout=120.0,
        )
    except Exception as exc:  # surfaced by the test thread via the sink
        sink["error"] = exc


def _park_in_thread(base_url: str, session_id: str, elicitation_id: str) -> dict:
    """Start a hook long-poll for *elicitation_id* and wait until it parks.

    :returns: The sink dict the worker writes into; it also carries the worker
        thread under ``"thread"`` for the later join.
    """
    sink: dict = {}
    worker = threading.Thread(
        target=_park_permission_hook,
        args=(base_url, session_id, elicitation_id, sink),
        daemon=True,
    )
    sink["thread"] = worker
    worker.start()
    _wait_for(partial(_is_parked, base_url, session_id, elicitation_id))
    return sink


def _drain_pending(base_url: str, session_id: str) -> None:
    """Best-effort decline of any still-parked elicitation (failure-path cleanup).

    When the auto-mode assertion fails the prompt is still parked; declining it
    releases the hook long-poll so teardown (session delete, thread join) never
    waits on a parked request. No-op on the passing path.
    """
    with contextlib.suppress(Exception):
        for pending in _pending_elicitations(base_url, session_id):
            eid = pending.get("elicitation_id")
            if not eid:
                continue
            with contextlib.suppress(Exception):
                httpx.post(
                    f"{base_url}/v1/sessions/{session_id}/elicitations/{eid}/resolve",
                    json={"action": "decline"},
                    timeout=10.0,
                )


def _assert_auto_mode_decision(sink: dict) -> None:
    """Join the parked long-poll and assert the auto-mode allow decision.

    The accepted verdict must come back as ``behavior=allow`` carrying the
    session-scoped ``setMode(auto)`` permission update -- the signal Claude
    Code needs to actually switch the session into auto mode.
    """
    sink["thread"].join(timeout=60)
    assert not sink["thread"].is_alive(), "permission hook long-poll never resolved"
    assert "error" not in sink, f"permission hook POST failed: {sink.get('error')!r}"
    assert sink["resp"].status_code == 200, sink["resp"].text
    decision = sink["resp"].json()["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "allow", decision
    assert decision.get("updatedPermissions") == _EXPECTED_AUTO_MODE_PERMISSIONS, decision


@pytest.mark.timeout(240)
def test_permission_card_offers_approve_and_switch_to_auto(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Chat: the pending Write permission card offers the auto-mode approval.

    Control legs first: the card renders with its ordinary actions (Approve,
    the narrower "Accept & allow all edits", Reject), proving the hook -> SSE
    -> card journey works in this session. Then the card must ALSO offer
    "Approve & switch to auto mode"; on the buggy build no such action renders
    and that expectation is the failing assertion.
    """
    base_url, session_id = seeded_session
    elicitation_id = f"elicit_claude_{secrets.token_hex(16)}"
    sink = _park_in_thread(base_url, session_id, elicitation_id)
    try:
        page.goto(f"{base_url}/c/{session_id}")
        card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
        expect(card).to_be_visible(timeout=_CARD_TIMEOUT_MS)
        expect(card).to_contain_text("Write")

        # Control: the existing affordances render.
        expect(card.get_by_role("button", name="Approve", exact=True)).to_be_visible()
        expect(card.get_by_role("button", name="Accept & allow all edits")).to_be_visible()
        expect(card.get_by_role("button", name="Reject", exact=True)).to_be_visible()

        # The bug: a regular permission prompt must offer a session-scoped
        # auto-mode approval alongside the narrower grants.
        auto_button = card.get_by_role("button", name=_AUTO_MODE_LABEL)
        expect(auto_button).to_be_visible(timeout=_AUTO_BUTTON_TIMEOUT_MS)

        # Honoring it: the card flips to its accepted-with-auto state and the
        # parked hook returns the session-scoped setMode(auto) decision.
        auto_button.click()
        responded = page.locator(f'{_APPROVAL_CARD}[data-state="responded"]').first
        expect(responded).to_be_visible(timeout=_CARD_TIMEOUT_MS)
        expect(responded).to_contain_text("Approved · auto mode")
        _assert_auto_mode_decision(sink)
    finally:
        _drain_pending(base_url, session_id)
        sink["thread"].join(timeout=10)


@pytest.mark.timeout(240)
def test_inbox_permission_card_offers_approve_and_switch_to_auto(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Inbox: the same pending permission prompt offers the auto-mode approval.

    The Inbox page gathers pending elicitations across sessions and renders
    the same ``ApprovalCard`` with a local submit handler that routes the
    verdict to the owning session -- so the auto-mode affordance (and the
    verdict it produces) must work there too. On the buggy build the inbox
    card lacks the action and the button expectation is the failing assertion.
    """
    base_url, session_id = seeded_session
    elicitation_id = f"elicit_claude_{secrets.token_hex(16)}"
    sink = _park_in_thread(base_url, session_id, elicitation_id)
    try:
        page.goto(f"{base_url}/inbox")
        item = page.locator(_INBOX_ITEM).filter(has_text="Write").first
        expect(item).to_be_visible(timeout=_CARD_TIMEOUT_MS)
        card = item.locator(_APPROVAL_CARD)
        expect(card).to_be_visible()

        # Control: the ordinary approval renders in the inbox card.
        expect(card.get_by_role("button", name="Approve", exact=True)).to_be_visible()

        # The bug: the inbox card must offer the same auto-mode approval.
        auto_button = card.get_by_role("button", name=_AUTO_MODE_LABEL)
        expect(auto_button).to_be_visible(timeout=_AUTO_BUTTON_TIMEOUT_MS)

        # Honoring it from the inbox routes the verdict to the owning session:
        # the prompt drains and the hook gets the setMode(auto) decision.
        auto_button.click()
        _wait_for(lambda: not _pending_elicitations(base_url, session_id))
        _assert_auto_mode_decision(sink)
    finally:
        _drain_pending(base_url, session_id)
        sink["thread"].join(timeout=10)
