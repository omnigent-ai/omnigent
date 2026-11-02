"""Patch the browser's view of a seeded session into the ``host_asleep`` state.

The e2e_ui harness has no real stop/resume managed provider, so tests that
need a dormant resumable sandbox host patch what the SPA sees instead:

- ``GET /v1/sessions/{id}`` (snapshot) → ``host_id`` set, ``host_resumable``
  true, and an old ``created_at`` (so the session is past the startup grace, or
  a fresh session reads as ``starting`` and masks ``host_asleep``).
- ``GET /v1/sessions?...`` (sidebar list) → the session is dropped from the
  list so the open-session row resolves off-sidebar straight from the patched
  snapshot (host-bound), instead of the real runner-bound sidebar row.
- ``GET /health`` → while ``asleep()`` holds, the session reports
  ``runner_online=false`` + ``host_online=false``; afterwards it reports both
  online. The open-session poll overrides the WS stream.
- ``WS /v1/sessions/updates`` → blocked so a stream push can't re-add the
  session to the sidebar or revert its liveness to the real values.

The seeded session's runner stays really connected, so filesystem reads and
``retry_session`` reach the real server.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from urllib.parse import urlparse

from playwright.sync_api import Page, Route

from tests.e2e_ui.conftest import fetch_with_retry

FAKE_HOST_ID = "host_test_managed"
# Unix seconds well before now so the session is outside the startup grace
# (STARTING_GRACE_S) — see useSessionLiveness row 2.
OLD_CREATED_AT = 1_700_000_000


def force_host_asleep(
    page: Page,
    session_id: str,
    *,
    asleep: Callable[[], bool] = lambda: True,
) -> None:
    """Register the host_asleep route patches on ``page`` before navigation.

    :param page: Playwright page before navigation.
    :param session_id: Session id to patch, e.g. ``"conv_abc123"``.
    :param asleep: Read on every ``/health`` poll; return ``False`` once the
        test wants the sandbox to read as back online.
    """

    def _patch_snapshot(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != f"/v1/sessions/{session_id}":
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
        payload["host_id"] = payload.get("host_id") or FAKE_HOST_ID
        payload["host_resumable"] = True
        payload["created_at"] = OLD_CREATED_AT
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

    def _patch_health(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != "/health":
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
        online = not asleep()
        liveness = {"runner_online": online, "host_online": online}
        # Plural shape used by the open-session fallback poll:
        # {"sessions": {"<id>": {...}}}.
        if isinstance(payload.get("sessions"), dict):
            payload["sessions"][session_id] = liveness
        # Singular shape ({"session": {...}}) for the session_id= variant.
        if isinstance(payload.get("session"), dict):
            payload["session"] = {**payload["session"], **liveness}
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    # Snapshot route registered last so it wins for /v1/sessions/{id} (Playwright
    # matches most-recently-registered first); the list/health handlers fall
    # through via continue_() for anything they don't own.
    page.route(re.compile(r"/v1/sessions(\?|$)"), _patch_list)
    page.route(re.compile(r"/health(\?|$)"), _patch_health)
    page.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}(\?|$)"), _patch_snapshot)
    page.route_web_socket(re.compile(r"/v1/sessions/updates"), lambda ws: None)
