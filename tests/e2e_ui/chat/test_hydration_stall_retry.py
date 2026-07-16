"""UI journey: a stalled hydration surfaces a Retry that recovers.

A hydration fetch that errors renders ``ConversationLoadError``, but one
that merely stalls (saturated server, dead intermediary) used to spin on
"Loading conversation..." forever with no affordance. Past the slow
threshold the spinner now admits the stall and offers Retry, which
re-runs hydration via ``switchTo``.

The stall is produced at the network level: while stalling, a Playwright
route rewrites session-scoped API requests to a local black-hole TCP
server that accepts connections and never responds - exactly what a
connection-pool-exhausted backend looks like to the SPA. Holding routes
open inside the route handler instead would wedge Playwright's routing
machinery on teardown.
"""

from __future__ import annotations

import socketserver
import threading
import time
from collections.abc import Iterator
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Page, Route, expect

_COMPOSER = "Ask the agent anything…"
_SLOW_HINT = "This is taking longer than expected."


class _BlackHoleHandler(socketserver.BaseRequestHandler):
    """Accept the connection, read the request, never answer."""

    def handle(self) -> None:
        with self.request:
            self.request.settimeout(120)
            try:
                self.request.recv(65536)
                time.sleep(120)
            except OSError:
                pass


@pytest.fixture
def black_hole_port() -> Iterator[int]:
    """A TCP server that accepts and never responds, for stall tests."""
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _BlackHoleHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.nightly
def test_stalled_hydration_offers_retry_and_recovers(
    page: Page,
    seeded_session: tuple[str, str],
    black_hole_port: int,
) -> None:
    base_url, session_id = seeded_session

    # While stalling, divert every session-scoped call to the black
    # hole so hydration hangs like it does against a saturated server.
    # Requests issued after the flag flips fall through to the real
    # backend.
    stalling = True

    def _maybe_stall(route: Route) -> None:
        if stalling:
            parts = urlsplit(route.request.url)
            target = f"http://127.0.0.1:{black_hole_port}{parts.path}"
            if parts.query:
                target = f"{target}?{parts.query}"
            route.continue_(url=target)
        else:
            route.fallback()

    page.route(f"**/v1/sessions/{session_id}**", _maybe_stall)

    page.goto(f"{base_url}/c/{session_id}")

    # Past the threshold the spinner is honest about the stall and
    # offers a way out. Threshold is 12s, so give the assertion room.
    expect(page.get_by_text(_SLOW_HINT)).to_be_visible(timeout=20_000)
    retry = page.get_by_role("button", name="Retry", exact=True)
    expect(retry).to_be_visible()

    # Unblock the backend and retry: switchTo re-runs hydration, the
    # new requests pass through, and the chat surface lands.
    stalling = False
    retry.click()
    expect(page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=15_000)
    expect(page.get_by_text(_SLOW_HINT)).to_have_count(0)
