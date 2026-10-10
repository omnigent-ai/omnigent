"""E2E coverage for recovering session events a healthy stream never delivered."""

from __future__ import annotations

import json

import httpx
from playwright.sync_api import Page, expect

_WORKING = '[data-testid="working-indicator"]'
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_HEARTBEAT_INTERVAL_MS = 10_000


def _publish_status(base_url: str, session_id: str, status: str) -> None:
    """Publish a durable status edge through the native-harness event route."""
    response = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_session_status", "data": {"status": status}},
        timeout=10.0,
    )
    response.raise_for_status()


def _publish_message(
    base_url: str, session_id: str, *, role: str, text: str, response_id: str
) -> None:
    """Persist a user or assistant message through the native-harness event route."""
    content_type = "input_text" if role == "user" else "output_text"
    item_data: dict[str, object] = {
        "role": role,
        "content": [{"type": content_type, "text": text}],
    }
    if role == "assistant":
        item_data["agent"] = "e2e-native"
    response = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_conversation_item",
            "data": {"item_type": "message", "item_data": item_data, "response_id": response_id},
        },
        timeout=10.0,
    )
    response.raise_for_status()


def _snapshot_status(base_url: str, session_id: str) -> str:
    """Read the status exposed by the slim session snapshot."""
    response = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    response.raise_for_status()
    return str(response.json()["status"])


def _install_heartbeat_only_stream(page: Page, session_id: str) -> None:
    """Replace this session's stream with a live heartbeat-only transport."""
    script = """
        (() => {
          const sessionId = __SESSION_ID__;
          const heartbeatInterval = __HEARTBEAT_INTERVAL__;
          const originalFetch = window.fetch.bind(window);
          window.__statusGapStreamOpens = 0;
          window.__statusGapHeartbeats = 0;
          window.fetch = (input, init) => {
            const url = typeof input === "string" ? input : input.url;
            const streamPath = `/v1/sessions/${sessionId}/stream`;
            if (new URL(url, window.location.origin).pathname !== streamPath) {
              return originalFetch(input, init);
            }

            window.__statusGapStreamOpens += 1;
            let heartbeatTimer;
            const body = new ReadableStream({
              start(controller) {
                const sendHeartbeat = () => {
                  const frame = "event: session.heartbeat\\ndata: {}\\n\\n";
                  controller.enqueue(new TextEncoder().encode(frame));
                  window.__statusGapHeartbeats += 1;
                };
                sendHeartbeat();
                heartbeatTimer = window.setInterval(sendHeartbeat, heartbeatInterval);
              },
              cancel() {
                window.clearInterval(heartbeatTimer);
              },
            });
            return Promise.resolve(new Response(body, {
              status: 200,
              headers: { "content-type": "text/event-stream" },
            }));
          };
        })()
        """
    page.add_init_script(
        script.replace("__SESSION_ID__", json.dumps(session_id)).replace(
            "__HEARTBEAT_INTERVAL__", str(_HEARTBEAT_INTERVAL_MS)
        )
    )


def test_snapshot_idle_clears_working_on_heartbeat_only_stream(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A visible chat recovers when its stream misses the terminal status edge."""
    base_url, session_id = seeded_session
    _publish_status(base_url, session_id, "running")
    assert _snapshot_status(base_url, session_id) == "running"

    page.clock.install()
    _install_heartbeat_only_stream(page, session_id)
    page.goto(f"{base_url}/c/{session_id}")

    working = page.locator(_WORKING)
    expect(working).to_be_visible(timeout=15_000)
    page.wait_for_function("window.__statusGapHeartbeats > 0")

    # The server reaches idle, but this tab's healthy stream never receives
    # that lifecycle event. Only snapshot reconciliation can clear the UI.
    _publish_status(base_url, session_id, "idle")
    assert _snapshot_status(base_url, session_id) == "idle"
    expect(working).to_be_visible()

    # Heartbeats keep the transport healthy, and the missing idle edge leaves
    # the stale indicator untouched before the reconciliation interval.
    page.clock.run_for("00:50")
    expect(working).to_be_visible()
    assert page.evaluate("window.__statusGapStreamOpens") == 1

    page.clock.run_for("00:10")

    expect(working).to_have_count(0, timeout=15_000)
    assert page.evaluate("window.__statusGapStreamOpens") == 1
    assert page.evaluate("window.__statusGapHeartbeats") >= 6


def test_snapshot_reconcile_backfills_messages_missed_on_heartbeat_only_stream(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A visible chat picks up messages persisted while its healthy stream carried no events."""
    base_url, session_id = seeded_session
    reply_text = "reply printed in the TUI"

    page.clock.install()
    _install_heartbeat_only_stream(page, session_id)
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder("Send a message…")).to_be_visible(timeout=15_000)
    page.wait_for_function("window.__statusGapHeartbeats > 0")

    # Output produced outside this tab reaches the server, but the tab's
    # stream delivers heartbeats only.
    _publish_message(
        base_url, session_id, role="user", text="typed in the TUI", response_id="resp_tui"
    )
    _publish_message(
        base_url, session_id, role="assistant", text=reply_text, response_id="resp_tui"
    )
    items_response = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"order": "asc", "limit": 100},
        timeout=10.0,
    )
    items_response.raise_for_status()
    items = items_response.json()["data"]
    assert any(
        part.get("text") == reply_text
        for item in items
        if item.get("role") == "assistant"
        for part in item.get("content", [])
    )

    page.clock.run_for("01:10")

    expect(page.locator(_ASSISTANT, has_text=reply_text)).to_be_visible(timeout=15_000)
    assert page.evaluate("window.__statusGapStreamOpens") == 1
