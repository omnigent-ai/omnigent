"""E2E: a dormant resumable managed host keeps the composer open.

When a session is bound to a managed host whose sandbox idle-stopped, the
open-session view must NOT dead-end on the ``host_offline`` reconnect banner:
the host is resumable, so the composer stays ENABLED and its placeholder tells
the user the next message will resume the sandbox host. This drives the
``host_asleep`` liveness variant (see ``ap-web/src/hooks/useSessionLiveness.ts``
row 3) end to end — host-bound + ``host_online=false`` + ``host_resumable=true``
+ the runner offline, and outside the startup grace.

The server fixture seeds a normal runner-bound ``hello_world`` session; the
harness has no real stop/resume managed provider, so the browser's view of the
session is patched into the ``host_asleep`` shape via route interception:

- ``GET /v1/sessions/{id}`` (snapshot) → ``host_id`` set, ``host_resumable``
  true, and an old ``created_at`` (so the session is past the startup grace, or
  a fresh session reads as ``starting`` and masks ``host_asleep``).
- ``GET /v1/sessions?...`` (sidebar list) → the session is dropped from the
  list so the open-session row resolves off-sidebar straight from the patched
  snapshot (host-bound), instead of the real runner-bound sidebar row.
- ``GET /health`` → the session reports ``runner_online=false`` +
  ``host_online=false``; the open-session poll overrides the WS stream.
- ``WS /v1/sessions/updates`` → blocked so a stream push can't re-add the
  session to the sidebar or revert its liveness to the real (online) values.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect

_ASLEEP_PLACEHOLDER = (
    "Current session's host is offline. "
    "Next message will resume the sandbox host which can take minutes"
)
_FAKE_HOST_ID = "host_test_managed"
# Unix seconds well before now so the session is outside the startup grace
# (STARTING_GRACE_S) — see useSessionLiveness row 2.
_OLD_CREATED_AT = 1_700_000_000


def _force_host_asleep(page: Page, session_id: str) -> None:
    """Patch the browser's view of ``session_id`` into the host_asleep state.

    Registered before navigation: three HTTP route patches plus one WS block.

    :param page: Playwright page before navigation.
    :param session_id: Session id to patch, e.g. ``"conv_abc123"``.
    """

    def _patch_snapshot(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != f"/v1/sessions/{session_id}":
            route.continue_()
            return
        response = route.fetch()
        payload = response.json()
        payload["host_id"] = payload.get("host_id") or _FAKE_HOST_ID
        payload["host_resumable"] = True
        # Age the session out of the startup grace (STARTING_GRACE_S): a
        # freshly-created session whose runner has never been seen online reads
        # as `starting` (cold-boot) and would mask the host_asleep row.
        payload["created_at"] = _OLD_CREATED_AT
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
        response = route.fetch()
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
        response = route.fetch()
        payload = response.json()
        offline = {"runner_online": False, "host_online": False}
        # Plural shape used by the open-session fallback poll:
        # {"sessions": {"<id>": {...}}}.
        if isinstance(payload.get("sessions"), dict):
            payload["sessions"][session_id] = offline
        # Singular shape ({"session": {...}}) for the session_id= variant.
        if isinstance(payload.get("session"), dict):
            payload["session"] = {**payload["session"], **offline}
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


def test_host_asleep_keeps_composer_open_with_resume_placeholder(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """In ``host_asleep`` the composer stays enabled with the wake placeholder.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser view is patched to the host_asleep shape.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _force_host_asleep(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=15_000)
    # The placeholder is the host_asleep tell: composer open, message resumes
    # the sandbox host. NOT the host_offline "Session offline — reconnect"
    # dead-end.
    expect(composer).to_have_attribute("placeholder", _ASLEEP_PLACEHOLDER, timeout=15_000)
    # Key behavior: a resumable dormant host keeps the composer usable.
    expect(composer).not_to_be_disabled()
