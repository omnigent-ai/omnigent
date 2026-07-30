"""E2E: the chat header's host badge shows which host a session is bound to.

The badge (``ap-web/src/components/HostBadge.tsx``) renders at the top of the
chat window for a host-bound session: the friendly host name (or a
sandbox-provider label) plus a green/red status dot driven by the live host
``host_online`` signal. It renders nothing when the session isn't host-bound.

The harness seeds a normal runner-bound ``hello_world`` session, so the browser
view is patched into a host-bound shape via route interception (same approach as
``test_host_asleep_composer.py``):

- ``GET /v1/sessions/{id}`` (snapshot) → ``host_id`` set so the badge has a host
  to resolve. ``host_resumable`` + an old ``created_at`` keep an offline host out
  of the ``host_offline`` reconnect dead-end so the chat view stays normal.
- ``GET /v1/hosts`` → returns the bound host so the badge resolves its name (or
  the sandbox-provider label). ``HostBadge`` calls this via
  ``useHosts({ includeSandbox: true })``.
- ``GET /health`` → reports the session's live ``host_online`` so the dot reads
  online/offline; the open-session poll overrides the WS stream.
- ``GET /v1/sessions`` (sidebar list) → the session is dropped so the open row
  resolves off-sidebar from the patched snapshot (host-bound).
- ``WS /v1/sessions/updates`` → blocked so a stream push can't revert liveness to
  the real (runner-online, host-unbound) values.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect

_FAKE_HOST_ID = "host_test_badge"
# Unix seconds well before now so an offline host is outside the startup grace
# (STARTING_GRACE_S) and reads host_asleep, not `starting` — see
# useSessionLiveness. Harmless for the online case.
_OLD_CREATED_AT = 1_700_000_000


def _patch_host_view(
    page: Page,
    session_id: str,
    *,
    host: dict,
    host_online: bool,
) -> None:
    """Patch the browser's view of ``session_id`` into a host-bound shape.

    Registered before navigation: four HTTP route patches plus one WS block.

    :param page: Playwright page before navigation.
    :param session_id: Session id to patch, e.g. ``"conv_abc123"``.
    :param host: The host record ``GET /v1/hosts`` should return for the bound
        host (``host_id`` is overwritten to match the patched snapshot).
    :param host_online: Live ``host_online`` the session reports via ``/health``;
        drives the badge's online (green) vs offline (red) dot.
    """
    host = {**host, "host_id": _FAKE_HOST_ID}

    def _patch_snapshot(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != f"/v1/sessions/{session_id}":
            route.continue_()
            return
        response = route.fetch()
        payload = response.json()
        payload["host_id"] = _FAKE_HOST_ID
        # Keep an offline host out of the host_offline reconnect dead-end so the
        # chat view (and its header) renders normally; harmless when online.
        payload["host_resumable"] = True
        payload["created_at"] = _OLD_CREATED_AT
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
        route.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps({"hosts": [host]}),
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
        live = {"runner_online": False, "host_online": host_online}
        if isinstance(payload.get("sessions"), dict):
            payload["sessions"][session_id] = live
        if isinstance(payload.get("session"), dict):
            payload["session"] = {**payload["session"], **live}
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    # Snapshot route registered last so it wins for /v1/sessions/{id} (Playwright
    # matches most-recently-registered first); the others fall through via
    # continue_() for anything they don't own.
    page.route(re.compile(r"/v1/hosts(\?|$)"), _patch_hosts)
    page.route(re.compile(r"/v1/sessions(\?|$)"), _patch_list)
    page.route(re.compile(r"/health(\?|$)"), _patch_health)
    page.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}(\?|$)"), _patch_snapshot)
    page.route_web_socket(re.compile(r"/v1/sessions/updates"), lambda ws: None)


def test_host_badge_shows_host_name_when_online(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A host-bound, online session shows the host name with an online status.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser view is patched to a host-bound, online shape.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _patch_host_view(
        page,
        session_id,
        host={"name": "e2e-host", "owner": "e2e", "status": "online", "sandbox_provider": None},
        host_online=True,
    )

    page.goto(f"{base_url}/c/{session_id}")

    badge = page.get_by_test_id("host-badge")
    expect(badge).to_be_visible(timeout=15_000)
    expect(badge).to_contain_text("e2e-host")
    # The dot is decorative; status is conveyed by the title (mouse hover) and an
    # sr-only word. Online == reachable host.
    expect(badge).to_have_attribute("title", "Host e2e-host, online", timeout=15_000)


def test_host_badge_shows_offline_when_host_unreachable(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A host-bound session whose host is offline shows an offline status.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser view is patched to a host-bound, offline shape.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _patch_host_view(
        page,
        session_id,
        host={"name": "e2e-host", "owner": "e2e", "status": "offline", "sandbox_provider": None},
        host_online=False,
    )

    page.goto(f"{base_url}/c/{session_id}")

    badge = page.get_by_test_id("host-badge")
    expect(badge).to_be_visible(timeout=15_000)
    expect(badge).to_contain_text("e2e-host")
    expect(badge).to_have_attribute("title", "Host e2e-host, offline", timeout=15_000)


def test_host_badge_labels_sandbox_session_by_provider(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A sandbox-backed session is labeled by its provider, not its managed name.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser view is patched to a sandbox-host-bound shape.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _patch_host_view(
        page,
        session_id,
        host={
            "name": "managed-cd8f66d0",
            "owner": "e2e",
            "status": "online",
            "sandbox_provider": "databricks-lakebox",
        },
        host_online=True,
    )

    page.goto(f"{base_url}/c/{session_id}")

    badge = page.get_by_test_id("host-badge")
    expect(badge).to_be_visible(timeout=15_000)
    # The managed-<hex> host name is collapsed to the provider label.
    expect(badge).to_contain_text("Databricks-lakebox Sandbox")
    expect(badge).not_to_contain_text("managed-cd8f66d0")
