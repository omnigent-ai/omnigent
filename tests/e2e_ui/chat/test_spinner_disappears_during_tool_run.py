"""The "Working…" spinner disappears while the agent is still running.

A native harness without a status file derives running/idle solely from the
tmux pane-diff idle watcher, so a long, output-less tool call (a
filesystem-wide search) leaves the pane unchanged past the idle threshold and
the watcher publishes a bare ``idle`` (no ``response_id``) mid-turn. The web
client adopts every server idle as a turn end: the "Working…" shimmer vanishes
and the in-flight tool card collapses to "no output" although the agent is
still running the search, so the session reads as stopped.

A plain-text turn's bare idle is a genuine turn end and must still clear the
indicator (see ``test_working_indicator_idle_clears``); the discriminator here
is the unresolved trailing tool call. This test FAILS while a bare idle
mid-tool is adopted as a turn end and passes once the indicator survives it.
"""

from __future__ import annotations

import uuid

import httpx
from playwright.sync_api import Page, expect

_WORKING = '[data-testid="working-indicator"]'
_COMPOSER = "Message the agent"
_NARRATION = "let me look for universe repos around the file system"


def _post_event(client: httpx.Client, session_id: str, event_type: str, data: dict) -> None:
    """Publish one native-forwarder ``/events`` payload.

    :param client: HTTP client bound to the spawned server's base URL.
    :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
    :param event_type: Wire event type, e.g. ``"external_session_status"``.
    :param data: The event's ``data`` payload.
    :returns: None.
    :raises AssertionError: If the server does not accept the event (202).
    """
    resp = client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": event_type, "data": data},
    )
    assert resp.status_code == 202, resp.text


def test_working_indicator_survives_false_idle_during_tool_call(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A bare ``idle`` mid-tool must not make a running agent read as stopped.

    Journey: the user prompts and the turn starts running ("Working…" lights);
    the agent narrates and dispatches a long filesystem-search tool call that
    is still in flight when the quiet pane makes the idle watcher publish a
    bare ``idle``; the indicator must stay lit because the tool is unresolved.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server.
    :returns: None.
    """
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
        # The function_call gets no function_call_output: the search tool is
        # genuinely in flight when the bare idle lands below.
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

        # The quiet pane makes the PTY-diff idle watcher misfire: a bare idle
        # (no response_id) mid-turn.
        _post_event(client, session_id, "external_session_status", {"status": "idle"})

    # Let the bare-idle edge settle in the client store so the positive
    # assertion below cannot pass on a pre-event frame.
    page.wait_for_timeout(2_500)

    expect(working).to_be_visible()
