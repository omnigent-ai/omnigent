"""E2E coverage for stale reasoning shimmer during later turns."""

from __future__ import annotations

import re
import time
import uuid

import httpx
from playwright.sync_api import Page, expect

_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'


def _seed_completed_reasoning(client: httpx.Client, session_id: str, text: str) -> None:
    """Persist one completed reasoning item as prior transcript history."""
    response = client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_conversation_item",
            "data": {
                "item_type": "reasoning",
                "response_id": f"resp_reasoning_{uuid.uuid4().hex[:8]}",
                "item_data": {
                    "agent": "e2e-reasoning",
                    "summary": [],
                    "content": [{"type": "reasoning_text", "text": text}],
                },
            },
        },
    )
    assert response.status_code == 202, response.text


def _start_live_turn(client: httpx.Client, session_id: str) -> str:
    """Publish a new user turn and mark it running."""
    response_id = f"resp_running_{uuid.uuid4().hex[:8]}"
    user = client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_conversation_item",
            "data": {
                "item_type": "message",
                "response_id": response_id,
                "item_data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Start a new native turn."}],
                },
            },
        },
    )
    assert user.status_code == 202, user.text

    status = client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_session_status",
            "data": {"status": "running", "response_id": response_id},
        },
    )
    assert status.status_code == 202, status.text
    return response_id


def _post_reasoning_delta(client: httpx.Client, session_id: str, *, started: bool) -> None:
    data: dict[str, object] = {"delta": "Current live native thinking."}
    if started:
        data["started"] = True
    reasoning = client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_output_reasoning_delta",
            "data": data,
        },
    )
    assert reasoning.status_code == 202, reasoning.text


def _post_text_delta(client: httpx.Client, session_id: str) -> None:
    """Publish text after reasoning while leaving the turn running."""
    time.sleep(0.05)
    text = client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_output_text_delta",
            "data": {"message_id": "live_text_1", "index": 0, "delta": "Working output."},
        },
    )
    assert text.status_code == 202, text.text


def test_completed_reasoning_does_not_shimmer_during_next_turn(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Prior reasoning stays settled while current native reasoning says Thinking."""
    base_url, session_id = seeded_session
    reasoning_text = f"previous reasoning {uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=base_url, timeout=10.0) as client:
        _seed_completed_reasoning(client, session_id, reasoning_text)
        page.goto(f"{base_url}/c/{session_id}")
        prior = page.locator(_ASSISTANT).first
        expect(prior).to_be_visible(timeout=15_000)
        expect(prior.get_by_role("button", name="Thought for a few seconds")).to_be_visible()
        expect(prior.get_by_text("Thinking...", exact=True)).to_have_count(0)

        _start_live_turn(client, session_id)
        _post_reasoning_delta(client, session_id, started=True)
        expect(page.locator(_WORKING)).to_be_visible(timeout=15_000)

        current = page.locator(_ASSISTANT).last
        expect(current.get_by_text("Thinking...", exact=True)).to_be_visible(timeout=15_000)

        _post_reasoning_delta(client, session_id, started=False)
        _post_text_delta(client, session_id)
        expect(current.get_by_text("Working output.")).to_be_visible(timeout=15_000)
        expect(page.get_by_text(re.compile(r"Thought for \d"))).to_have_count(0)

        expect(prior).to_be_visible()
        expect(prior.get_by_role("button", name="Thought for a few seconds")).to_be_visible()
        expect(prior.get_by_text("Thinking...", exact=True)).to_have_count(0)
