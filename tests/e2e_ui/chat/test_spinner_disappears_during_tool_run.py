"""Browser regression for a false native idle during a pending tool call."""

from __future__ import annotations

import uuid

import httpx
from playwright.sync_api import Page, expect

_WORKING = '[data-testid="working-indicator"]'
_COMPOSER = "Message the agent"
_NARRATION = "let me look for universe repos around the file system"


def _post_event(client: httpx.Client, session_id: str, event_type: str, data: dict) -> None:
    """Publish an event through the spawned server's native-forwarder route."""
    resp = client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": event_type, "data": data},
    )
    assert resp.status_code == 202, resp.text


def test_working_indicator_survives_false_idle_during_tool_call(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Keep Working visible when a bare idle arrives before a tool result."""
    base_url, session_id = seeded_session
    response_id = f"resp_universe_{uuid.uuid4().hex[:8]}"
    call_id = f"call_{uuid.uuid4().hex[:8]}"

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_role("textbox", name=_COMPOSER)).to_be_visible(timeout=20_000)

    working = page.locator(_WORKING)

    with httpx.Client(base_url=base_url, timeout=10.0) as client:
        _post_event(
            client,
            session_id,
            "external_conversation_item",
            {
                "item_type": "message",
                "response_id": response_id,
                "item_data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Find the universe repos."}],
                },
            },
        )
        _post_event(
            client,
            session_id,
            "external_session_status",
            {"status": "running", "response_id": response_id},
        )
        expect(working).to_be_visible(timeout=15_000)

        _post_event(
            client,
            session_id,
            "external_output_text_delta",
            {"message_id": "live_text_1", "index": 0, "delta": _NARRATION},
        )
        # No tool result has arrived when the bare idle lands below.
        _post_event(
            client,
            session_id,
            "external_conversation_item",
            {
                "item_type": "function_call",
                "response_id": response_id,
                "item_data": {
                    "agent": "e2e-universe-agent",
                    "name": "shell",
                    "arguments": '{"command": "find / -type d -name \'*universe*\'"}',
                    "call_id": call_id,
                },
            },
        )
        expect(page.get_by_text(_NARRATION)).to_be_visible(timeout=15_000)
        expect(working).to_be_visible(timeout=15_000)

        # The idle watcher emits this bare status while the tool is still running.
        _post_event(client, session_id, "external_session_status", {"status": "idle"})

    # Let the bare-idle edge settle in the client store so the positive
    # assertion below cannot pass on a pre-event frame.
    page.wait_for_timeout(2_500)

    expect(working).to_be_visible()
