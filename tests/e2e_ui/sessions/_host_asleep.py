"""Patch the browser's view of a seeded session into the ``host_asleep`` state.

The e2e_ui harness has no real stop/resume managed provider, so tests that
need a dormant resumable sandbox host patch what the SPA sees instead:

- ``GET /v1/sessions/{id}`` (snapshot) → ``host_id`` set, ``host_resumable``
  true, and an old ``created_at`` (so the session is past the startup grace, or
  a fresh session reads as ``starting`` and masks ``host_asleep``).
- ``GET /v1/sessions?...`` (sidebar list) → the session is dropped from the
  list so the open-session row resolves off-sidebar straight from the patched
  snapshot (host-bound), instead of the real runner-bound sidebar row.
- ``GET /v1/hosts`` → adds an Arclet sandbox host record so the chip reads
  "Arclet Sandbox".
- ``GET /health`` → the session reports ``runner_online`` and ``host_online``
  from the caller's ``host_online`` switch (offline by default). The
  open-session poll overrides the WS stream.
- ``WS /v1/sessions/updates`` → blocked so a stream push can't re-add the
  session to the sidebar or revert its liveness to the real values.

The seeded session's runner stays really connected, so filesystem reads and
``retry_session`` reach the real server.
"""

from __future__ import annotations

import json
import re
import time
from urllib.parse import urlparse

from playwright.sync_api import Page, Route

from tests.e2e_ui.conftest import fetch_with_retry

FAKE_HOST_ID = "host_test_managed"
FAKE_HOST = {
    "host_id": FAKE_HOST_ID,
    "name": "omnigent-arclet-1234",
    "owner": "local",
    "status": "offline",
    "sandbox_provider": "arclet",
}
# Unix seconds well before now so the session is outside the startup grace
# (STARTING_GRACE_S) — see useSessionLiveness row 2.
OLD_CREATED_AT = 1_700_000_000


def force_host_asleep(
    page: Page,
    session_id: str,
    *,
    host_online: dict[str, bool] | None = None,
    sandbox_status: dict[str, object] | None = None,
) -> None:
    """Patch the browser's view of ``session_id`` into the host_asleep state.

    Registered before navigation: four HTTP route patches plus one WS block.

    :param page: Playwright page before navigation.
    :param session_id: Session id to patch, e.g. ``"conv_abc123"``.
    :param host_online: Mutable ``{"online": bool}`` the ``/health`` patch reads
        on every poll, so a test can bring the host online mid-journey.
        Defaults to always offline.
    :param sandbox_status: Wake stage to report in the snapshot, e.g.
        ``{"stage": "provisioning", "provider": "arclet", "error": None}``.
    """
    liveness = host_online if host_online is not None else {"online": False}

    def _patch_snapshot(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != f"/v1/sessions/{session_id}":
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
        payload["host_id"] = payload.get("host_id") or FAKE_HOST_ID
        payload["host_resumable"] = True
        # Age the session out of the startup grace (STARTING_GRACE_S): a
        # freshly-created session whose runner has never been seen online reads
        # as `starting` (cold-boot) and would mask the host_asleep row.
        payload["created_at"] = OLD_CREATED_AT
        if sandbox_status is not None:
            payload["sandbox_status"] = sandbox_status
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    def _patch_list(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != "/v1/sessions":
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
        rows = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(rows, list):
            payload["data"] = [
                r for r in rows if not (isinstance(r, dict) and r.get("id") == session_id)
            ]
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    def _patch_hosts(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != "/v1/hosts":
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
        status = "online" if liveness["online"] else "offline"
        payload["hosts"] = [*payload.get("hosts", []), {**FAKE_HOST, "status": status}]
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    def _patch_health(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != "/health":
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
        online = liveness["online"]
        reported = {"runner_online": online, "host_online": online}
        # Plural shape used by the open-session fallback poll:
        # {"sessions": {"<id>": {...}}}.
        if isinstance(payload.get("sessions"), dict):
            payload["sessions"][session_id] = reported
        # Singular shape ({"session": {...}}) for the session_id= variant.
        if isinstance(payload.get("session"), dict):
            payload["session"] = {**payload["session"], **reported}
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    # Snapshot route registered last so it wins for /v1/sessions/{id} (Playwright
    # matches most-recently-registered first); the list/health handlers fall
    # through via continue_() for anything they don't own.
    page.route(re.compile(r"/v1/sessions(\?|$)"), _patch_list)
    page.route(re.compile(r"/v1/hosts(\?|$)"), _patch_hosts)
    page.route(re.compile(r"/health(\?|$)"), _patch_health)
    page.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}(\?|$)"), _patch_snapshot)
    page.route_web_socket(re.compile(r"/v1/sessions/updates"), lambda ws: None)


def hold_wake_requests(page: Page, session_id: str) -> list[Route]:
    """Hold each ``retry_session`` POST until the test releases it.

    :param page: Playwright page, before the wake is requested.
    :param session_id: Session whose events endpoint to intercept.
    :returns: Held routes, in arrival order; other events pass through.
    """
    held: list[Route] = []

    def _hold(route: Route) -> None:
        request = route.request
        if request.method == "POST" and "retry_session" in (request.post_data or ""):
            held.append(route)
            return
        route.continue_()

    page.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}/events(\?|$)"), _hold)
    return held


def next_held_wake(page: Page, held: list[Route], timeout_ms: int = 10_000) -> Route:
    """Wait for the next held wake request and return it.

    :param page: Page whose event loop delivers the route.
    :param held: List filled by :func:`hold_wake_requests`.
    :param timeout_ms: How long to wait for the request.
    :returns: The oldest held route, removed from ``held``.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    while not held:
        assert time.monotonic() < deadline, "the wake request never reached the server"
        page.wait_for_timeout(50)
    return held.pop(0)
