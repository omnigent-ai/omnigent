"""Shared SSE helpers for server-backed chat tests.

The model-flow assertions live in the browser lane; a few unrelated E2E tests
still use these helpers to replace the session stream deterministically.
"""

from __future__ import annotations

import json

from playwright.sync_api import Page

_STREAM_CONTROLLER = """
(() => {
  const sessionId = __SESSION_ID__;
  const originalFetch = window.fetch.bind(window);
  window.fetch = (input, init) => {
    const url = typeof input === "string" ? input : input.url;
    const streamPath = `/v1/sessions/${sessionId}/stream`;
    if (new URL(url, window.location.origin).pathname === streamPath) {
      const body = new ReadableStream({
        start(controller) {
          window.__mfStreamController = controller;
        },
      });
      return Promise.resolve(new Response(body, {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      }));
    }
    return originalFetch(input, init);
  };
})()
"""


def _install_stream_controller(page: Page, session_id: str) -> None:
    """Capture the session's SSE stream so tests can push frames."""
    page.add_init_script(_STREAM_CONTROLLER.replace("__SESSION_ID__", json.dumps(session_id)))


def _push_sse(page: Page, event: str, payload: dict) -> None:
    """Push one SSE frame through the captured stream controller."""
    page.wait_for_function("window.__mfStreamController !== undefined")
    page.evaluate(
        """
        ({ event, payload }) => {
          const frame = `event: ${event}\\ndata: ${JSON.stringify(payload)}\\n\\n`;
          window.__mfStreamController.enqueue(new TextEncoder().encode(frame));
        }
        """,
        {"event": event, "payload": payload},
    )
