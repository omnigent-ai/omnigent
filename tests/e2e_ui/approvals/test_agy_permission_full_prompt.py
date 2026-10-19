"""Exercise Antigravity permission choices in Chat and Inbox.

A recorded native prompt goes through production mapping and the real server
hook. The browser response proves scope delivery, not live native rule saving.
"""

from __future__ import annotations

import json
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

# A real agy WAITING permission step recorded from a live `agy` session.
_FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "antigravity"
    / "steps"
    / "run_command_waiting.json"
)


def _load_pending_permission() -> PendingInteraction:
    """Parse the recorded agy WAITING step through the production detector."""
    step = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    pending = pending_interaction(step)
    assert pending is not None, "fixture step must be a WAITING interaction"
    assert pending["kind"] == "permission", "fixture step must be a permission gate"
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
    """Park a recorded permission prompt on the production long-poll hook."""
    pending = _load_pending_permission()
    params = to_elicitation_params(dict(pending))
    elicitation_id = agy_elicitation_id(
        f"cascade-e2e-{uuid.uuid4().hex[:8]}",
        pending["trajectory_id"],
        pending["step_index"],
    )
    body = {"elicitation_id": elicitation_id, "params": params.model_dump()}
    result_holder["elicitation_id"] = elicitation_id

    def _post_hook() -> None:
        try:
            resp = httpx.post(
                f"{base_url}/v1/sessions/{session_id}/hooks/antigravity-elicitation-request",
                json=body,
                timeout=120.0,
            )
            resp.raise_for_status()
            result_holder["verdict"] = resp.json()
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
    *,
    surface: str = "chat",
):
    """Navigate to the session and return the pending agy permission card."""

    def _elicitation_ready() -> bool:
        _require_no_hook_error(result_holder)
        return any(
            event.get("elicitation_id") == result_holder["elicitation_id"]
            for event in _pending_elicitations(base_url, session_id)
        )

    _wait_for(_elicitation_ready, message="agy permission elicitation did not appear")
    page.goto(f"{base_url}/inbox" if surface == "inbox" else f"{base_url}/c/{session_id}")
    card = (
        page.locator(f'{_APPROVAL_CARD}[data-state="pending"]')
        .filter(has_text="Antigravity")
        .first
    )
    expect(card).to_be_visible(timeout=_CARD_TIMEOUT_MS)
    return card


def _reject_and_join(
    base_url: str,
    session_id: str,
    hook_thread: threading.Thread,
    result_holder: dict,
) -> None:
    """Resolve this test's hook even if rendering failed, then join its thread."""
    eid = result_holder["elicitation_id"]
    try:
        response = httpx.post(
            f"{base_url}/v1/sessions/{session_id}/elicitations/{eid}/resolve",
            json={"action": "decline"},
            timeout=10.0,
        )
        response.raise_for_status()
        _wait_for(
            lambda: all(
                event.get("elicitation_id") != eid
                for event in _pending_elicitations(base_url, session_id)
            ),
            message="agy permission elicitation did not clear",
        )
    finally:
        hook_thread.join(timeout=30)
    assert not hook_thread.is_alive(), "agy elicitation hook POST did not settle"
    _require_no_hook_error(result_holder)


@pytest.mark.timeout(120)
@pytest.mark.parametrize("surface", ["chat", "inbox"])
@pytest.mark.parametrize("persist", ["session", "always"])
def test_agy_permission_persist_request_round_trip(
    page: Page,
    seeded_session: tuple[str, str],
    surface: str,
    persist: str,
) -> None:
    """Both approval surfaces send the chosen scope without claiming it was saved."""
    base_url, session_id = seeded_session
    result_holder: dict = {}
    hook_thread = _park_agy_permission_elicitation(base_url, session_id, result_holder)
    try:
        card = _open_pending_agy_card(page, base_url, session_id, result_holder, surface=surface)
        expect(card).to_contain_text("Antigravity wants to run: pwd")
        expect(card).to_contain_text("Running pwd command")
        expect(card).to_contain_text("New sessions have separate rules")
        for label in (
            "Approve",
            "Reject",
            "Allow commands starting with pwd for this session",
            "Always allow commands starting with pwd",
        ):
            expect(card.get_by_role("button", name=label, exact=True)).to_be_visible()
        card.get_by_test_id(f"approval-card-agy-{persist}-allow").click()
        hook_thread.join(timeout=30)
        assert not hook_thread.is_alive(), "agy elicitation hook POST did not settle"
        _require_no_hook_error(result_holder)
        assert result_holder["verdict"] == {
            "action": "accept",
            "content": None,
            "_meta": {"persist": persist},
        }
        if surface == "chat":
            responded = page.locator(f'{_APPROVAL_CARD}[data-state="responded"]').filter(
                has_text="Antigravity"
            )
            label = "Session allow requested" if persist == "session" else "Always allow requested"
            expect(responded).to_contain_text(label)
            expect(responded).not_to_contain_text("Always allowed")
        else:
            expect(card).not_to_be_visible()
    finally:
        if hook_thread.is_alive():
            _reject_and_join(base_url, session_id, hook_thread, result_holder)
