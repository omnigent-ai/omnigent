"""E2E: the Stop/Interrupt control stays available while an elicitation is pending.

A turn is paused mid-flight on the mock-LLM gate (session working, the
composer shows the Interrupt square), then an ``AskUserQuestion`` permission
request parks a pending elicitation into that window — the same shape as an
agent raising an interactive question or tool approval mid-turn. The pending
card locks the send path, but the turn is still active server-side, so the
composer's Interrupt control must remain visible and enabled. Clicking it must
also end the runner turn from that parked state.
"""

from __future__ import annotations

import threading
import time

import httpx
import pytest
from playwright.sync_api import Page, expect

_APPROVAL_CARD = '[data-testid="approval-card"]'
_COMPOSER = "Send a message…"

_MOCK_ELICITATION_TIMEOUT_MS = 15_000
# The mock-LLM turn is fast, but the first turn also pays runner boot.
_TURN_START_TIMEOUT_MS = 60_000


def _session_snapshot(base_url: str, session_id: str) -> dict:
    """Return the session snapshot (owner view)."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json()


def _wait_for(predicate, *, timeout_s: float = 30.0, interval_s: float = 0.5) -> None:
    """Poll *predicate* until truthy or the deadline passes."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    raise AssertionError("condition not met within timeout")


def _post_ask_user_question(base_url: str, session_id: str, holder: dict) -> threading.Thread:
    """Park an ``AskUserQuestion`` permission request on *session_id*.

    The call blocks server-side until a verdict lands, so it runs on its own
    daemon thread; the fixture's teardown drains it.
    """

    def _post() -> None:
        try:
            resp = httpx.post(
                f"{base_url}/v1/sessions/{session_id}/hooks/permission-request",
                json={
                    "tool_name": "AskUserQuestion",
                    "tool_input": {
                        "questions": [
                            {
                                "question": "Which option do you prefer?",
                                "options": ["Alpha", "Bravo"],
                            }
                        ]
                    },
                },
                timeout=120.0,
            )
            resp.raise_for_status()
            holder["response"] = resp.json()
        except Exception as exc:
            holder["error"] = exc

    thread = threading.Thread(target=_post, daemon=True)
    thread.start()
    return thread


@pytest.mark.timeout(180)
def test_stop_control_survives_pending_elicitation(
    page: Page,
    paused_mid_turn_session: tuple[str, str, str],
) -> None:
    """A pending elicitation keeps Interrupt available, and Interrupt ends the turn."""
    base_url, session_id, mock_url = paused_mid_turn_session

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("Inspect the workspace.")
    page.get_by_role("button", name="Send", exact=True).click()

    # The turn is genuinely in flight: blocked on the mock gate between its
    # two tool calls, so it stays active for as long as the test needs.
    _wait_for(
        lambda: httpx.get(f"{mock_url}/gate/pending", timeout=5.0).json()["pending"],
        timeout_s=_TURN_START_TIMEOUT_MS / 1000,
    )

    interrupt_button = page.get_by_role("button", name="Interrupt", exact=True)
    expect(interrupt_button).to_be_visible(timeout=_TURN_START_TIMEOUT_MS)

    holder: dict = {}
    _post_ask_user_question(base_url, session_id, holder)

    card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
    expect(card).to_be_visible(timeout=_MOCK_ELICITATION_TIMEOUT_MS)
    assert _session_snapshot(base_url, session_id).get("pending_elicitations"), (
        "server has no parked elicitation"
    )
    # The turn is still active: the gate was never released, so the composer
    # is looking at a working session with a pending prompt.
    assert httpx.get(f"{mock_url}/gate/pending", timeout=5.0).json()["pending"]
    assert _session_snapshot(base_url, session_id).get("status") in {"running", "waiting"}

    # Give the pending-card composer state a beat to settle (and to be
    # visible in a recording) before the core assertion.
    page.wait_for_timeout(1_000)

    expect(
        interrupt_button,
        "pending elicitation hid the Stop/Interrupt control of an active turn",
    ).to_be_visible()
    expect(interrupt_button).to_be_enabled()

    with page.expect_request(
        lambda request: (
            request.method == "POST" and request.url.endswith(f"/v1/sessions/{session_id}/events")
        )
    ) as request_info:
        interrupt_button.click()

    assert request_info.value.post_data_json == {"type": "interrupt", "data": {}}
    expect(interrupt_button).not_to_be_visible(timeout=_MOCK_ELICITATION_TIMEOUT_MS)
    _wait_for(lambda: _session_snapshot(base_url, session_id).get("status") == "idle")
