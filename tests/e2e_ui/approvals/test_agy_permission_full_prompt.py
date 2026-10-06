"""E2E (UI): the Antigravity (agy) permission card must surface the full prompt.

When agy asks for command permission, its own TUI prompt offers the full menu —
"Yes", the "always allow" persist variants, and "No" — and describes the action.
This test guards that the Omnigent web card mirrors that prompt: it must surface
agy's action description and the persist ("always allow") choices rather than
collapsing to a bare binary Approve/Reject pair.

The journey is driven exactly the way the production bridge drives it: a REAL
recorded agy WAITING permission step
(``tests/fixtures/antigravity/steps/run_command_waiting.json`` — its spec
advertises ``persistSuggestionType: PERSIST_SUGGESTION_TYPE_SUGGESTED`` with
``suggestedPersistPattern: "pwd"``, i.e. agy explicitly offers an always-allow
choice for this prompt, and ``actionDescription: "Running pwd command"``) is
mapped through the production ``pending_interaction`` →
``to_elicitation_params`` → ``agy_elicitation_id`` pipeline and parked on the
live server via the same
``POST /v1/sessions/{id}/hooks/antigravity-elicitation-request`` long-poll the
runner-side reader uses. No real agy login is required (agy is OAuth-only, so
CI cannot run a live turn); everything from the bridge's shape-mapping to the
browser render is the production path.

Contrast: the Codex MCP approval card (``test_codex_mcp_persistence.py``)
renders "Approve / Approve for this session / Always allow / Reject" for an
equivalent persist-capable prompt.
"""

from __future__ import annotations

import contextlib
import json
import re
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent.harnesses.antigravity_native.interactions import agy_elicitation_id
from omnigent.harnesses.antigravity_native.steps import PendingInteraction, pending_interaction
from omnigent.server.routes._antigravity_elicitation import to_elicitation_params

_APPROVAL_CARD = '[data-testid="approval-card"]'
_CARD_TIMEOUT_MS = 15_000
# The card renders synchronously, so its affordances appear promptly.
_AFFORDANCE_TIMEOUT_MS = 4_000

# A real agy WAITING permission step recorded from a live `agy` session.
_FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "antigravity"
    / "steps"
    / "run_command_waiting.json"
)

# Any accessible action name that would carry the "allow always / don't ask
# again / allow for this session" semantics agy's own prompt offers. Kept
# deliberately loose so the test pins the *capability*, not one label.
_ALWAYS_ALLOW_PATTERN = re.compile(r"always|don'?t ask|this session", re.IGNORECASE)


def _load_pending_permission() -> PendingInteraction:
    """Parse the recorded agy WAITING step through the production detector."""
    step = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    pending = pending_interaction(step)
    assert pending is not None, "fixture step must be a WAITING interaction"
    assert pending["kind"] == "permission", "fixture step must be a permission gate"
    # The recorded spec advertises an always-allow persist option — the extra
    # menu entries agy's own TUI renders for this prompt.
    spec = pending["spec"]
    assert spec.get("persistSuggestionType") == "PERSIST_SUGGESTION_TYPE_SUGGESTED"
    assert spec.get("suggestedPersistPattern") == "pwd"
    assert spec.get("actionDescription") == "Running pwd command"
    return pending


def _park_agy_permission_elicitation(
    base_url: str,
    session_id: str,
    result_holder: dict,
) -> threading.Thread:
    """POST the production-shaped agy permission elicitation hook (long-poll).

    Mirrors ``omnigent.harnesses.antigravity_native.reader._post_agy_elicitation_request``
    byte-for-byte: ``{"elicitation_id": <agy_elicitation_id(...)>, "params":
    <to_elicitation_params(pending).model_dump()>}``.
    """
    pending = _load_pending_permission()
    params = to_elicitation_params(dict(pending))
    elicitation_id = agy_elicitation_id(
        f"cascade-e2e-{uuid.uuid4().hex[:8]}",
        pending["trajectory_id"],
        pending["step_index"],
    )
    body = {"elicitation_id": elicitation_id, "params": params.model_dump()}

    def _post_hook() -> None:
        try:
            resp = httpx.post(
                f"{base_url}/v1/sessions/{session_id}/hooks/antigravity-elicitation-request",
                json=body,
                timeout=120.0,
            )
            resp.raise_for_status()
            result_holder["status_code"] = resp.status_code
        except Exception as exc:
            result_holder["error"] = exc

    thread = threading.Thread(target=_post_hook, daemon=True)
    thread.start()
    return thread


def _pending_elicitations(base_url: str, session_id: str) -> list[dict]:
    """Return the session snapshot's pending elicitation events."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json().get("pending_elicitations") or []


def _wait_for(
    predicate: Callable[[], bool],
    *,
    timeout_s: float = 30.0,
    interval_s: float = 0.25,
    message: str = "condition not met within timeout",
) -> None:
    """Poll *predicate* until truthy or the deadline passes."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    raise AssertionError(message)


def _require_no_hook_error(result_holder: dict) -> None:
    """Surface a failed elicitation hook POST instead of a generic timeout."""
    if "error" in result_holder:
        raise AssertionError(f"agy elicitation hook POST failed: {result_holder['error']}")


def _open_pending_agy_card(
    page: Page,
    base_url: str,
    session_id: str,
    result_holder: dict,
):
    """Navigate to the session and return the pending agy permission card."""

    def _elicitation_ready() -> bool:
        _require_no_hook_error(result_holder)
        return bool(_pending_elicitations(base_url, session_id))

    _wait_for(_elicitation_ready, message="agy permission elicitation did not appear")
    page.goto(f"{base_url}/c/{session_id}")
    card = (
        page.locator(f'{_APPROVAL_CARD}[data-state="pending"]')
        .filter(has_text="Antigravity")
        .first
    )
    expect(card).to_be_visible(timeout=_CARD_TIMEOUT_MS)
    return card


def _reject_and_join(
    card,
    base_url: str,
    session_id: str,
    hook_thread: threading.Thread,
    result_holder: dict,
) -> None:
    """Settle the parked long-poll, then join so a late POST failure surfaces.

    When setup failed before the card rendered, resolve the parked elicitation
    directly so the hook long-poll thread cannot leak onto the shared server.
    """
    if card is None:
        with contextlib.suppress(Exception):
            for pending in _pending_elicitations(base_url, session_id):
                eid = pending.get("elicitation_id")
                if eid:
                    httpx.post(
                        f"{base_url}/v1/sessions/{session_id}/elicitations/{eid}/resolve",
                        json={"action": "reject"},
                        timeout=10.0,
                    )
        hook_thread.join(timeout=30)
        return
    card.get_by_role("button", name="Reject", exact=True).click()
    _wait_for(
        lambda: not _pending_elicitations(base_url, session_id),
        message="agy permission elicitation did not clear after Reject",
    )
    hook_thread.join(timeout=30)
    assert not hook_thread.is_alive(), "agy elicitation hook POST did not settle"
    _require_no_hook_error(result_holder)


@pytest.mark.timeout(120)
def test_agy_permission_card_offers_always_allow_choice(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The card must expose agy's always-allow choice, not just Approve/Reject.

    agy's own TUI prompt for this exact recorded step is a numbered menu whose
    middle options are the "always allow" persist variants (the spec advertises
    ``persistSuggestionType: SUGGESTED`` / ``suggestedPersistPattern: "pwd"``).
    The web card must surface at least one affordance carrying that semantics;
    without it a web user cannot express "allow and don't ask again" at all.
    """
    base_url, session_id = seeded_session
    result_holder: dict = {}
    hook_thread = _park_agy_permission_elicitation(base_url, session_id, result_holder)

    card = None
    try:
        card = _open_pending_agy_card(page, base_url, session_id, result_holder)

        # Sanity: the card names the gated command and still offers the binary pair.
        expect(card).to_contain_text("pwd")
        expect(card.get_by_role("button", name="Approve", exact=True)).to_be_visible()
        expect(card.get_by_role("button", name="Reject", exact=True)).to_be_visible()

        # The card must surface agy's advertised always-allow choice.
        always_allow = card.get_by_role("button", name=_ALWAYS_ALLOW_PATTERN)
        expect(always_allow.first).to_be_visible(timeout=_AFFORDANCE_TIMEOUT_MS)
    finally:
        _reject_and_join(card, base_url, session_id, hook_thread, result_holder)


@pytest.mark.timeout(120)
def test_agy_permission_card_shows_action_description(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The card must show agy's action description, not just the raw command.

    agy's TUI prompt describes what it wants to do ("Running pwd command"); the
    web card must capture that context so the approver sees the same prompt the
    terminal shows, rather than only "Antigravity wants to run: pwd".
    """
    base_url, session_id = seeded_session
    result_holder: dict = {}
    hook_thread = _park_agy_permission_elicitation(base_url, session_id, result_holder)

    card = None
    try:
        card = _open_pending_agy_card(page, base_url, session_id, result_holder)

        # Sanity: the card names the gated command.
        expect(card).to_contain_text("pwd")

        # The card must surface agy's action description.
        expect(card).to_contain_text("Running pwd command", timeout=_AFFORDANCE_TIMEOUT_MS)
    finally:
        _reject_and_join(card, base_url, session_id, hook_thread, result_holder)
