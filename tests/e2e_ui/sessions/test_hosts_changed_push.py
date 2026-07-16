"""E2E: ``hosts_changed`` WS frame drives host badge updates without polling.

Two tests that together cover the headline claim of the hosts-push change:

- ``test_hosts_changed_frame_updates_host_badge`` — injecting a
  ``hosts_changed`` frame over the session-updates WS causes the host
  badge to reflect a new host status within the WS round-trip, far
  below the 60 s fallback-poll cadence. A broken push path (frame not
  forwarded by ``_discovery()``, cache not invalidated in
  ``SessionUpdatesProvider``, ``useHosts`` not refetching on invalidate)
  would leave the badge stale until the 60 s poll fires — timing out
  this assertion.

- ``test_host_badge_not_polled_frequently`` — after the page settles,
  ``GET /v1/hosts`` is not called every 10 s. The old code had
  ``refetchInterval: 10_000``; the new code uses 60 s. Zero requests
  during a 12 s observation window distinguishes these (a restored 10 s
  cadence produces at least one).

Route-interception approach mirrors ``test_host_badge.py``:

- ``GET /v1/sessions/{id}`` (snapshot) patched to inject ``host_id`` so
  ``HostBadge`` has a host to resolve.
- ``GET /v1/hosts`` stubbed to a controlled list so the badge name and
  status come from a known payload that the test can swap mid-run.
- ``GET /v1/sessions`` (list) drops the test session so the off-sidebar
  badge renders from the patched snapshot path.
- ``WS /v1/sessions/updates`` intercepted (not connected to the real
  server) so the stream never pushes a live ``host_online`` signal —
  keeping ``liveOnline === undefined`` and letting the badge fall back
  to the ``useHosts`` status field we control. The interceptor stores
  the ``WebSocketRoute`` object so the test can inject a
  ``hosts_changed`` frame at will.
- ``GET /health`` is NOT patched: the live health poll's
  ``runner_online`` / ``host_online`` comes from the real server, but
  since the session is not host-bound there, both fields are absent or
  false — ``useSessionHostOnline`` will be ``undefined`` and won't
  override the ``useHosts``-derived status.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from playwright.sync_api import Page, Request, WebSocketRoute, expect

_FAKE_HOST_ID = "host_push_e2e"
_OLD_CREATED_AT = 1_700_000_000  # far in the past → outside the host-asleep grace window


# ---------------------------------------------------------------------------
# Shared route helpers
# ---------------------------------------------------------------------------


def _make_hosts_body(status: str) -> str:
    return json.dumps(
        {
            "hosts": [
                {
                    "host_id": _FAKE_HOST_ID,
                    "name": "push-e2e-host",
                    "owner": "e2e",
                    "status": status,
                    "sandbox_provider": None,
                }
            ]
        }
    )


def _patch_session_snapshot(page: Page, session_id: str) -> None:
    """Inject ``host_id`` into ``GET /v1/sessions/{session_id}``."""

    def _handler(route):  # type: ignore[no-untyped-def]
        req = route.request
        if req.method != "GET" or urlparse(req.url).path != f"/v1/sessions/{session_id}":
            route.continue_()
            return
        resp = route.fetch()
        payload = resp.json()
        payload["host_id"] = _FAKE_HOST_ID
        payload["host_resumable"] = True
        payload["created_at"] = _OLD_CREATED_AT
        route.fulfill(
            status=200,
            headers={**resp.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    page.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}(\?|$)"), _handler)


def _patch_session_list(page: Page, session_id: str) -> None:
    """Drop ``session_id`` from ``GET /v1/sessions`` list responses."""

    def _handler(route):  # type: ignore[no-untyped-def]
        req = route.request
        if req.method != "GET" or urlparse(req.url).path != "/v1/sessions":
            route.continue_()
            return
        resp = route.fetch()
        payload = resp.json()
        rows = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(rows, list):
            payload["data"] = [r for r in rows if r.get("id") != session_id]
        route.fulfill(
            status=200,
            headers={**resp.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    page.route(re.compile(r"/v1/sessions(\?|$)"), _handler)


def _patch_health_drops_session(page: Page, session_id: str) -> None:
    """Drop ``session_id`` from ``GET /health`` batch responses.

    The badge draws ``host_online`` from two independent sources: the
    ``WS /v1/sessions/updates`` stream (intercepted here) and the
    open-session ``/health`` poll. The real ``/health`` always emits
    ``host_online`` for a session it finds — ``null`` when the session
    isn't host-bound — and that ``null`` reaches ``useSessionHostOnline``
    as a live signal, which ``HostBadge`` treats as authoritative
    "unknown" and renders over the ``useHosts`` status this test drives.

    Dropping the id from the ``sessions`` map leaves it *absent* (not
    ``null``), so ``useSessionHostOnline`` stays ``undefined`` — "not
    observed yet" — and the badge falls back to the ``useHosts`` status
    field, exactly as the test intends. This mirrors the snapshot/list
    patches: the browser is placed in a host-bound view whose liveness
    comes solely from the controlled ``useHosts`` payload.
    """

    def _handler(route):  # type: ignore[no-untyped-def]
        req = route.request
        if req.method != "GET" or urlparse(req.url).path != "/health":
            route.continue_()
            return
        resp = route.fetch()
        payload = resp.json()
        sessions = payload.get("sessions") if isinstance(payload, dict) else None
        if isinstance(sessions, dict):
            sessions.pop(session_id, None)
        route.fulfill(
            status=200,
            headers={**resp.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    page.route(re.compile(r"/health(\?|$)"), _handler)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_hosts_changed_frame_updates_host_badge(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A ``hosts_changed`` WS frame causes the host badge to update immediately.

    The test patches the browser into a host-bound view, waits for the badge
    to reflect the initial "online" state from ``GET /v1/hosts``, then swaps
    the stub to "offline" and injects a ``hosts_changed`` frame. The badge must
    update to "offline" well within the 60 s fallback-poll window — proving
    that cache invalidation (not the poll) drove the change.

    A failure means the ``hosts_changed`` frame is not forwarded or not handled:
    the badge would stay "online" until the 60 s fallback poll fires, timing
    out this assertion.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a server-backed session.
    """
    base_url, session_id = seeded_session

    # Capture the WS route object so we can inject frames mid-test.
    ws_routes: list[WebSocketRoute] = []

    def _handle_ws(ws: WebSocketRoute) -> None:
        ws_routes.append(ws)
        # Swallow client messages (watch-set frames) without connecting to the
        # real server. The stream never pushes host_online, so liveOnline stays
        # undefined and the badge falls back to the useHosts status field.
        ws.on_message(lambda _msg: None)

    # Register routes before navigation so they're active on first request.
    _patch_session_snapshot(page, session_id)
    _patch_session_list(page, session_id)
    _patch_health_drops_session(page, session_id)
    page.route_web_socket(re.compile(r"/v1/sessions/updates"), _handle_ws)

    # Start with "online" so we can observe a transition to "offline".
    hosts_status = {"current": "online"}

    def _hosts_route(route):  # type: ignore[no-untyped-def]
        req = route.request
        if req.method != "GET" or urlparse(req.url).path != "/v1/hosts":
            route.continue_()
            return
        route.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=_make_hosts_body(hosts_status["current"]),
        )

    page.route(re.compile(r"/v1/hosts(\?|$)"), _hosts_route)

    page.goto(f"{base_url}/c/{session_id}")

    badge = page.get_by_test_id("host-badge")
    expect(badge).to_be_visible(timeout=15_000)
    expect(badge).to_have_attribute("title", "Host push-e2e-host, online", timeout=15_000)

    # Flip the stub to "offline" before pushing the invalidation frame.
    # The client will re-fetch /v1/hosts on invalidation and see the new value.
    hosts_status["current"] = "offline"

    # Inject the hosts_changed frame via the intercepted WS.
    assert ws_routes, "WS /v1/sessions/updates never connected"
    ws_routes[0].send(json.dumps({"type": "hosts_changed"}))

    # KEY ASSERTION: badge reflects the new status well below the 60 s fallback.
    # Only the WS-push → invalidate → refetch path delivers this fast; a dead
    # push path would leave the badge stale and time out here.
    expect(badge).to_have_attribute("title", "Host push-e2e-host, offline", timeout=15_000)


def test_host_badge_not_polled_frequently(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """``GET /v1/hosts`` is not called every 10 s while the page is idle.

    The old ``useHosts`` had ``refetchInterval: 10_000``; the new code uses
    60 s. Over a 12 s observation window (comfortably inside 60 s, multiple
    times the old 10 s cadence) an idle page must make zero list requests
    after the initial load settles. A restored 10 s poll would produce at
    least one.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the fixture.
    """
    base_url, session_id = seeded_session

    hits: list[str] = []

    def _track(req: Request) -> None:
        parsed = urlparse(req.url)
        if req.method == "GET" and parsed.path == "/v1/hosts":
            hits.append(req.url)

    page.on("request", _track)

    _patch_session_snapshot(page, session_id)
    _patch_session_list(page, session_id)
    page.route_web_socket(
        re.compile(r"/v1/sessions/updates"), lambda ws: ws.on_message(lambda _: None)
    )
    page.route(
        re.compile(r"/v1/hosts(\?|$)"),
        lambda r: r.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=_make_hosts_body("online"),
        ),
    )

    page.goto(f"{base_url}/c/{session_id}")

    badge = page.get_by_test_id("host-badge")
    expect(badge).to_be_visible(timeout=15_000)

    # Let the initial-load burst settle (useHosts fetch + possible staleTime
    # mount refetch) before measuring.
    page.wait_for_timeout(5_000)
    baseline = len(hits)

    # Observe over a window longer than the old 10 s cadence but shorter than
    # the new 60 s fallback.
    page.wait_for_timeout(12_000)
    new_hits = hits[baseline:]

    assert new_hits == [], (
        f"expected no GET /v1/hosts polls during 12 s idle window "
        f"(refetchInterval should be 60 s, not 10 s), but saw {len(new_hits)}: {new_hits}"
    )
