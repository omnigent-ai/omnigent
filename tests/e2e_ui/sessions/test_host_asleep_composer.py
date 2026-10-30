"""E2E: a dormant resumable managed host keeps the composer open.

When a session is bound to a managed host whose sandbox idle-stopped, the
open-session view must NOT dead-end on the ``host_offline`` reconnect banner:
the host is resumable, so the composer stays ENABLED with the normal send
placeholder (the next message resumes the sandbox host). This drives the
``host_asleep`` liveness variant (see ``web/src/hooks/useSessionLiveness.ts``
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

- ``GET /v1/hosts`` → adds an Arclet sandbox host record so the chip reads
  "Arclet Sandbox".

The same view also offers **Reconnect host**, which asks the real server to
wake the host with a ``retry_session`` event instead of sending a message. The
wake's progress shows only in the composer's host status dot, which spins until
the host is online; there is no toast unless the wake fails.
"""

from __future__ import annotations

import json
import re
import time
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry

_COMPOSER_PLACEHOLDER = "Send a message…"
_FAKE_HOST_ID = "host_test_managed"
_FAKE_HOST = {
    "host_id": _FAKE_HOST_ID,
    "name": "omnigent-arclet-1234",
    "owner": "local",
    "status": "offline",
    "sandbox_provider": "arclet",
}
# Unix seconds well before now so the session is outside the startup grace
# (STARTING_GRACE_S) — see useSessionLiveness row 2.
_OLD_CREATED_AT = 1_700_000_000
# Keeps each status visible long enough to read in a recording.
_HOLD_MS = 1_500


def _force_host_asleep(
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
        payload["host_id"] = payload.get("host_id") or _FAKE_HOST_ID
        payload["host_resumable"] = True
        # Age the session out of the startup grace (STARTING_GRACE_S): a
        # freshly-created session whose runner has never been seen online reads
        # as `starting` (cold-boot) and would mask the host_asleep row.
        payload["created_at"] = _OLD_CREATED_AT
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
        payload["hosts"] = [*payload.get("hosts", []), {**_FAKE_HOST, "status": status}]
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


def _hold_wake_requests(page: Page, session_id: str) -> list[Route]:
    """Hold each ``retry_session`` POST until the test releases it.

    :param page: Playwright page before navigation.
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


def _next_held(page: Page, held: list[Route], timeout_ms: int = 10_000) -> Route:
    """Wait for the next held wake request and return it.

    :param page: Page whose event loop delivers the route.
    :param held: List filled by :func:`_hold_wake_requests`.
    :param timeout_ms: How long to wait for the request.
    :returns: The oldest held route, removed from ``held``.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    while not held:
        assert time.monotonic() < deadline, "the wake request never reached the server"
        page.wait_for_timeout(50)
    return held.pop(0)


def _expect_host_status(page: Page, status: str, *, tooltip: str) -> None:
    """Hover the composer's host chip and check its dot and tooltip.

    :param page: Page showing the session composer.
    :param status: Expected ``data-status``, e.g. ``"starting"``.
    :param tooltip: Text the hover tooltip's Status row should show.
    """
    expect(page.get_by_test_id("composer-host-status")).to_have_attribute(
        "data-status", status, timeout=15_000
    )
    page.mouse.move(0, 0)
    page.get_by_test_id("composer-host-select").hover()
    expect(page.get_by_test_id("composer-host-tooltip")).to_contain_text(tooltip, timeout=5_000)
    page.wait_for_timeout(_HOLD_MS)
    page.mouse.move(0, 0)


def _open_host_menu(page: Page, status: str) -> None:
    """Open the host menu and check its current-host dot matches the chip.

    :param page: Page showing the session composer.
    :param status: Expected ``data-status`` for both dots.
    """
    page.get_by_test_id("composer-host-select").click()
    menu_dot = page.get_by_test_id("composer-host-menu-status")
    expect(menu_dot).to_have_attribute("data-status", status, timeout=5_000)
    chip_class = page.get_by_test_id("composer-host-status").get_attribute("class")
    assert menu_dot.get_attribute("class") == chip_class
    page.wait_for_timeout(_HOLD_MS)


def test_host_asleep_keeps_composer_open(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
) -> None:
    """In ``host_asleep`` the composer stays enabled with the send placeholder.

    :param request: Supplies the recorded ``page`` after server setup.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser view is patched to the host_asleep shape.
    :returns: None.
    """
    base_url, session_id = seeded_session
    page: Page = request.getfixturevalue("page")
    _force_host_asleep(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=15_000)
    # Key behavior: a resumable dormant host keeps the composer open and
    # enabled with the normal send placeholder — NOT the host_offline
    # "Session offline — reconnect" disabled dead-end.
    expect(composer).to_have_attribute("placeholder", _COMPOSER_PLACEHOLDER, timeout=15_000)
    expect(composer).not_to_be_disabled()


def test_reconnect_host_wakes_asleep_sandbox_in_status_dot(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
) -> None:
    """Reconnect host spins the host dot, then turns it green, with no toast.

    The wake request reaches the real server, which answers once the runner is
    connected; the test holds it so the starting state can be observed, then
    reports the host online before releasing it.

    :param request: Supplies the recorded ``page`` after server setup.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser view is patched to the host_asleep shape.
    :returns: None.
    """
    base_url, session_id = seeded_session
    page: Page = request.getfixturevalue("page")
    host_online = {"online": False}
    _force_host_asleep(page, session_id, host_online=host_online)
    held = _hold_wake_requests(page, session_id)
    toasts = page.locator("[data-sonner-toast]")

    page.goto(f"{base_url}/c/{session_id}")

    _expect_host_status(page, "offline", tooltip="Asleep — wakes on next message")
    _open_host_menu(page, "offline")
    page.get_by_role("menuitem", name="Reconnect host").click()
    wake = _next_held(page, held)
    assert wake.request.post_data_json == {"type": "retry_session", "data": {}}

    _expect_host_status(page, "starting", tooltip="Starting")
    _open_host_menu(page, "starting")
    expect(page.get_by_role("menuitem", name="Reconnect host")).to_have_count(0)
    page.keyboard.press("Escape")
    expect(toasts).to_have_count(0)

    host_online["online"] = True
    wake.continue_()
    _expect_host_status(page, "online", tooltip="Connected")
    _open_host_menu(page, "online")
    page.keyboard.press("Escape")
    expect(toasts).to_have_count(0)


def test_failed_wake_restores_dot_and_reports_error(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
) -> None:
    """A failed wake stops the spinner, explains why, and offers Reconnect again.

    :param request: Supplies the recorded ``page`` after server setup.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser view is patched to the host_asleep shape.
    :returns: None.
    """
    base_url, session_id = seeded_session
    page: Page = request.getfixturevalue("page")
    _force_host_asleep(page, session_id)
    held = _hold_wake_requests(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    _expect_host_status(page, "offline", tooltip="Asleep — wakes on next message")
    _open_host_menu(page, "offline")
    page.get_by_role("menuitem", name="Reconnect host").click()
    wake = _next_held(page, held)
    _expect_host_status(page, "starting", tooltip="Starting")

    wake.fulfill(
        status=502,
        content_type="application/json",
        body=json.dumps({"detail": "Arclet sandbox didn't start."}),
    )
    expect(
        page.get_by_text("Couldn't reconnect the sandbox: Arclet sandbox didn't start.")
    ).to_be_visible(timeout=15_000)
    _expect_host_status(page, "offline", tooltip="Asleep — wakes on next message")
    _open_host_menu(page, "offline")
    expect(page.get_by_role("menuitem", name="Reconnect host")).to_be_visible()
    page.wait_for_timeout(_HOLD_MS)
    page.keyboard.press("Escape")


def test_session_opened_mid_wake_spins_dot_through_server_stage(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
) -> None:
    """Opening a session while its sandbox wakes shows the stage in the dot.

    The snapshot reports the server's ``provisioning`` stage, as it does when a
    wake started from another tab or a sent message is still running.

    :param request: Supplies the recorded ``page`` after server setup.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser view is patched to the host_asleep shape.
    :returns: None.
    """
    base_url, session_id = seeded_session
    page: Page = request.getfixturevalue("page")
    host_online = {"online": False}
    _force_host_asleep(
        page,
        session_id,
        host_online=host_online,
        sandbox_status={"stage": "provisioning", "provider": "arclet", "error": None},
    )

    page.goto(f"{base_url}/c/{session_id}")

    _expect_host_status(page, "starting", tooltip="Provisioning sandbox")
    _open_host_menu(page, "starting")
    expect(page.get_by_role("menuitem", name="Reconnect host")).to_have_count(0)
    page.keyboard.press("Escape")

    # The next liveness poll (every 10s) sees the host online.
    host_online["online"] = True
    _expect_host_status(page, "online", tooltip="Connected")
