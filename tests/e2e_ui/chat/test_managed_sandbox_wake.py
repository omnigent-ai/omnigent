"""Browser regressions for managed wake status and missed consumed events.

Drive the real SPA and server-backed history, controlling only the session's
HTTP/SSE boundary. No live sandbox or native agent credentials are required.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.chat.test_initial_history_loading import _seed_message
from tests.e2e_ui.chat.test_queued_message_lifecycle import _send, _user_bubble
from tests.e2e_ui.conftest import fetch_with_retry

_STREAM_CONTROLLER = """
(() => {
  const sessionId = __SESSION_ID__;
  const originalFetch = window.fetch.bind(window);
  window.__wakeStreams = [];
  window.fetch = (input, init) => {
    const url = typeof input === "string" ? input : input.url;
    if (new URL(url, window.location.origin).pathname ===
        `/v1/sessions/${sessionId}/stream`) {
      const body = new ReadableStream({
        start(controller) {
          window.__wakeStreams.push(controller);
          init?.signal?.addEventListener("abort", () => {
            try { controller.error(new DOMException("Aborted", "AbortError")); }
            catch { /* Already closed by the test. */ }
          }, { once: true });
        },
      });
      return Promise.resolve(new Response(body, {
        status: 200, headers: { "content-type": "text/event-stream" },
      }));
    }
    return originalFetch(input, init);
  };
})()
"""


def _install_stream(page: Page, session_id: str) -> None:
    page.add_init_script(_STREAM_CONTROLLER.replace("__SESSION_ID__", json.dumps(session_id)))


def _push_sse(page: Page, event: str, payload: dict) -> None:
    page.wait_for_function("window.__wakeStreams.length > 0")
    page.evaluate(
        """({ event, payload }) => {
          const frame = `event: ${event}\\ndata: ${JSON.stringify(payload)}\\n\\n`;
          window.__wakeStreams.at(-1).enqueue(new TextEncoder().encode(frame));
        }""",
        {"event": event, "payload": payload},
    )


@pytest.mark.parametrize("from_snapshot", [False, True], ids=["live-event", "snapshot"])
def test_managed_wake_indicator_advances_and_clears(
    page: Page, seeded_session: tuple[str, str], from_snapshot: bool
) -> None:
    """A wake is not a new provision; ready removes the progress indicator."""
    base_url, session_id = seeded_session
    _install_stream(page, session_id)

    def patch_snapshot(route: Route) -> None:
        if route.request.method != "GET":
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
        payload["sandbox_status"] = {"stage": "waking"} if from_snapshot else None
        route.fulfill(response=response, json=payload)

    page.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}(\?|$)"), patch_snapshot)
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_label("Message the agent")).to_be_visible(timeout=15_000)

    def stage(value: str) -> None:
        _push_sse(
            page,
            "session.sandbox_status",
            {"type": "session.sandbox_status", "conversation_id": session_id, "stage": value},
        )

    if not from_snapshot:
        expect(page.get_by_role("heading", name="What should we work on?")).to_be_visible()
        stage("waking")
    indicator = page.get_by_test_id("runner-starting-indicator")
    expect(indicator).to_contain_text("Waking sandbox")
    expect(page.get_by_text("Provisioning sandbox", exact=False)).to_have_count(0)
    expect(page.get_by_test_id("disconnected-indicator")).to_have_count(0)

    stage("connecting")
    expect(indicator).to_contain_text("Starting agent")
    stage("ready")
    expect(indicator).to_have_count(0)
    expect(page.get_by_test_id("sandbox-failed-indicator")).to_have_count(0)
    expect(page.get_by_label("Message the agent")).to_be_enabled()


@pytest.mark.parametrize("queued_duplicate", [False, True], ids=["consumed", "still-queued"])
@pytest.mark.parametrize("wrapper", ["claude-code-native-ui", "codex-native-ui"])
def test_rebind_reconciles_a_missed_consumed_event(
    page: Page, seeded_session: tuple[str, str], queued_duplicate: bool, wrapper: str
) -> None:
    """Drop a stale optimistic copy, but keep an identical server-queued send."""
    base_url, session_id = seeded_session
    prompt = "wake-rebind original prompt"
    follow_up = "wake-rebind follow up"
    pending_inputs: list[dict] = []
    posts: list[dict] = []
    _install_stream(page, session_id)

    def patch_snapshot(route: Route) -> None:
        if route.request.method != "GET":
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
        payload["labels"] = {**(payload.get("labels") or {}), "omnigent.wrapper": wrapper}
        payload["pending_inputs"] = pending_inputs
        route.fulfill(response=response, json=payload)

    def accept_message(route: Route) -> None:
        if route.request.method != "POST":
            route.continue_()
            return
        body = route.request.post_data_json
        if body.get("type") != "message":
            route.continue_()
            return
        posts.append(body)
        # Native message acceptance precedes transcript persistence/consumption.
        route.fulfill(status=202, json={"queued": True, "pending_id": f"pending_{len(posts)}"})

    page.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}(\?|$)"), patch_snapshot)
    page.route(f"**/v1/sessions/{session_id}/events", accept_message)
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_label("Message the agent")).to_be_visible(timeout=15_000)

    with page.expect_response(f"**/v1/sessions/{session_id}/events"):
        _send(page, prompt)
    expect(_user_bubble(page, prompt)).to_have_count(1)
    if queued_duplicate:
        _push_sse(
            page,
            "session.status",
            {"type": "session.status", "conversation_id": session_id, "status": "idle"},
        )
        expect(page.get_by_label("Message the agent")).to_have_attribute(
            "placeholder", "Send a message…"
        )
        with page.expect_response(f"**/v1/sessions/{session_id}/events"):
            _send(page, prompt)
        expect(_user_bubble(page, prompt)).to_have_count(2)
        pending_inputs.append(
            {"pending_id": "pending_2", "content": [{"type": "input_text", "text": prompt}]}
        )

    # Persist the accepted prompt and answer while withholding consumed SSE.
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        _seed_message(client, session_id, response_id="resp_wake", role="user", text=prompt)
        _seed_message(
            client,
            session_id,
            response_id="resp_wake",
            role="assistant",
            text="The original task completed.",
        )
    _push_sse(
        page,
        "session.status",
        {"type": "session.status", "conversation_id": session_id, "status": "idle"},
    )
    # Native idle edges must not clear the optimistic copies before rebind.
    expect(page.get_by_label("Message the agent")).to_have_attribute(
        "placeholder", "Send a message…"
    )
    expect(_user_bubble(page, prompt)).to_have_count(2 if queued_duplicate else 1)
    # End the subscription; the next send must rebind and read durable history.
    page.evaluate(
        """() => {
          const stream = window.__wakeStreams.at(-1);
          stream.enqueue(new TextEncoder().encode("data: [DONE]\\n\\n"));
          stream.close();
        }"""
    )
    with page.expect_response(f"**/v1/sessions/{session_id}/events"):
        _send(page, follow_up)
    page.wait_for_function("window.__wakeStreams.length === 2")
    expect(page.get_by_text("The original task completed.", exact=True)).to_be_visible()
    expect(_user_bubble(page, prompt)).to_have_count(2 if queued_duplicate else 1)
    expect(_user_bubble(page, follow_up)).to_have_count(1)
    assert [post["data"]["content"][0]["text"] for post in posts] == (
        [prompt, prompt, follow_up] if queued_duplicate else [prompt, follow_up]
    )
