"""E2E coverage for recovering session events a live stream missed."""

from __future__ import annotations

import json

import httpx
from playwright.sync_api import Page, expect

from tests._helpers.polling import poll_until
from tests.e2e_ui.conftest import configure_mock_llm

from .test_queued_message_lifecycle import _send, _user_bubble

_WORKING = '[data-testid="working-indicator"]'
_HEARTBEAT_INTERVAL_MS = 10_000
_STATUS_RECONCILE_INTERVAL_MS = 60_000

_MISSED_REPLY_TOKEN = "sentinel-missed-reply-6c3a"
_MISSED_REPLY_PROMPT = f"{_MISSED_REPLY_TOKEN} hold this turn open"
_MISSED_REPLY = "sentinel-missed-reply-text-2f9d"


def _publish_status(base_url: str, session_id: str, status: str) -> None:
    """Publish a durable status edge through the native-harness event route."""
    response = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_session_status", "data": {"status": status}},
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


def _install_stream_silenced_after_turn_opens(page: Page, session_id: str) -> None:
    """Deliver this session's stream until a turn opens, then send only heartbeats."""
    script = """
        (() => {
          const sessionId = __SESSION_ID__;
          const heartbeatInterval = __HEARTBEAT_INTERVAL__;
          const originalFetch = window.fetch.bind(window);
          const encoder = new TextEncoder();
          const heartbeat = encoder.encode("event: session.heartbeat\\ndata: {}\\n\\n");
          const opensTurn = (frame) =>
            /^event: session\\.status$/m.test(frame) && frame.includes('"status": "running"');
          window.__streamSilenced = false;
          window.__silencedHeartbeats = 0;
          window.fetch = async (input, init) => {
            const url = typeof input === "string" ? input : input.url;
            const streamPath = `/v1/sessions/${sessionId}/stream`;
            if (new URL(url, window.location.origin).pathname !== streamPath) {
              return originalFetch(input, init);
            }

            window.__firstStreamOpenAt ??= Date.now();
            const upstream = window.__streamSilenced ? null : await originalFetch(input, init);
            const reader = upstream?.body.getReader();
            let heartbeatTimer;
            const body = new ReadableStream({
              start(controller) {
                const silence = () => {
                  window.__streamSilenced = true;
                  heartbeatTimer = window.setInterval(() => {
                    controller.enqueue(heartbeat);
                    window.__silencedHeartbeats += 1;
                  }, heartbeatInterval);
                };
                init?.signal?.addEventListener("abort", () => {
                  window.clearInterval(heartbeatTimer);
                  controller.error(new DOMException("aborted", "AbortError"));
                });
                if (reader === undefined) {
                  silence();
                  return;
                }
                // Upstream frames keep arriving after silence() and are dropped.
                (async () => {
                  const decoder = new TextDecoder();
                  let buffered = "";
                  for (;;) {
                    const { value, done } = await reader.read();
                    if (done) return;
                    if (window.__streamSilenced) continue;
                    buffered += decoder.decode(value, { stream: true });
                    let end;
                    while (!window.__streamSilenced && (end = buffered.indexOf("\\n\\n")) >= 0) {
                      const frame = buffered.slice(0, end + 2);
                      buffered = buffered.slice(end + 2);
                      controller.enqueue(encoder.encode(frame));
                      if (opensTurn(frame)) silence();
                    }
                  }
                })().catch(() => {});
              },
              cancel() {
                window.clearInterval(heartbeatTimer);
                reader?.cancel().catch(() => {});
              },
            });
            return new Response(body, {
              status: 200,
              headers: { "content-type": "text/event-stream" },
            });
          };
        })()
        """
    page.add_init_script(
        script.replace("__SESSION_ID__", json.dumps(session_id)).replace(
            "__HEARTBEAT_INTERVAL__", str(_HEARTBEAT_INTERVAL_MS)
        )
    )


def _run_until_status_check_due_in(page: Page, remaining_ms: int) -> None:
    """Fast-forward to ``remaining_ms`` before the status check armed at the first stream open."""
    elapsed_ms = page.evaluate("Date.now() - window.__firstStreamOpenAt")
    assert elapsed_ms < _STATUS_RECONCILE_INTERVAL_MS - remaining_ms
    page.clock.run_for(_STATUS_RECONCILE_INTERVAL_MS - remaining_ms - elapsed_ms)


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


def test_snapshot_idle_backfills_reply_a_silent_stream_missed(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A reply the live stream never delivered appears once the snapshot shows the turn ended."""
    base_url, session_id = seeded_session
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": _MISSED_REPLY, "block": True}],
        key="missed-reply",
        match=_MISSED_REPLY_TOKEN,
    )
    page.add_init_script("localStorage.setItem('omnigent:background-session-titles', 'off')")
    page.clock.install()
    _install_stream_silenced_after_turn_opens(page, session_id)
    page.goto(f"{base_url}/c/{session_id}")
    try:
        _send(page, _MISSED_REPLY_PROMPT)
        poll_until(
            lambda: httpx.get(f"{mock_llm_server_url}/gate/pending", timeout=5.0).json()["pending"]
        )
        page.wait_for_function("window.__streamSilenced")
    finally:
        httpx.post(f"{mock_llm_server_url}/gate/release", timeout=5.0)

    # The server finishes the turn, but this tab's stream now only carries heartbeats.
    poll_until(lambda: _snapshot_status(base_url, session_id) == "idle")
    working = page.locator(_WORKING)
    reply = page.locator('[data-testid="message-bubble"][data-role="assistant"]').filter(
        has_text=_MISSED_REPLY
    )
    # The installed clock also advances in real time, so measure how far the status check is.
    _run_until_status_check_due_in(page, 5_000)
    expect(working).to_be_visible()
    expect(reply).to_have_count(0)

    page.clock.run_for("00:10")

    expect(reply).to_be_visible(timeout=15_000)
    expect(working).to_have_count(0)
    expect(_user_bubble(page, _MISSED_REPLY_PROMPT)).to_have_count(1)
    assert page.evaluate("window.__silencedHeartbeats") >= 5
