"""E2E: a runner_asleep chat session offers a direct Attach button.

When a session's runner is down but its host is still up (``runner_asleep``),
the runner relaunches on the next message — and the chat now also offers a
direct **Attach** button that recovers via ``retry_session`` with NO message.

This drives the ``runner_asleep`` liveness row end to end (see
``web/src/hooks/useSessionLiveness.ts`` row 5: ``runner_online=false`` +
``host_online=true`` + past the startup grace + no turn in flight) and asserts
the banner renders, the click fires ``retry_session`` (no user message), and the
button holds pending afterwards.

The server fixture seeds a real runner-bound ``hello_world`` session; the
browser's view is patched into the ``runner_asleep`` shape via route
interception:

- ``GET /v1/sessions/{id}`` (snapshot) → an old ``created_at`` so the session is
  past the startup grace (a fresh session reads as ``starting`` and masks the
  runner-down signal).
- ``GET /health`` → the session reports ``runner_online=false`` +
  ``host_online=true`` (runner down, host up).
- ``WS /v1/sessions/updates`` → blocked so a stream push can't revert liveness
  to the real (online) values.
- ``POST /v1/sessions/{id}/events`` → the ``retry_session`` control event the
  Attach button fires is captured and answered with a recovered response.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry

# Unix seconds well before now so the session is outside STARTING_GRACE_S — see
# useSessionLiveness row 2; a fresh runner-never-seen session reads as
# `starting` and would mask the runner-down signal.
_OLD_CREATED_AT = 1_700_000_000
_RECONNECT_PLACEHOLDER = "Send a message to reconnect this session"


def _force_runner_asleep(page: Page, session_id: str) -> None:
    """Patch the browser's view of ``session_id`` into the runner_asleep state.

    :param page: Playwright page before navigation.
    :param session_id: Session id to patch, e.g. ``"conv_abc123"``.
    """

    def _patch_snapshot(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != f"/v1/sessions/{session_id}":
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
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
        # Runner down, host up: the row this Attach button targets.
        asleep = {"runner_online": False, "host_online": True}
        if isinstance(payload.get("sessions"), dict):
            payload["sessions"][session_id] = asleep
        if isinstance(payload.get("session"), dict):
            payload["session"] = {**payload["session"], **asleep}
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    # Drop the session from the sidebar list so its liveness resolves off the
    # patched /health poll (the open-session fallback) rather than the
    # `/v1/sessions/updates` push, which carries the real runner_online for
    # sidebar sessions. The snapshot route is registered last so it wins for
    # `/v1/sessions/{id}`; list/health fall through via continue_().
    page.route(re.compile(r"/v1/sessions(\?|$)"), _patch_list)
    page.route(re.compile(r"/health(\?|$)"), _patch_health)
    page.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}(\?|$)"), _patch_snapshot)
    page.route_web_socket(re.compile(r"/v1/sessions/updates"), lambda ws: None)


def test_runner_asleep_chat_offers_attach_button(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The runner_asleep banner's Attach fires retry_session with no message.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser view is patched to the runner_asleep shape.
    :returns: None.
    """
    base_url, session_id = seeded_session

    # Capture the retry_session control events the Attach button fires; answer
    # them as a successful relaunch so the button can settle into pending.
    retry_calls: list[dict] = []

    def _intercept_events(route: Route) -> None:
        request = route.request
        if request.method == "POST" and (
            urlparse(request.url).path == f"/v1/sessions/{session_id}/events"
        ):
            body = request.post_data_json or {}
            if body.get("type") == "retry_session":
                retry_calls.append(body)
                route.fulfill(
                    status=200,
                    headers={"content-type": "application/json"},
                    body=json.dumps({"recovered": True, "recovery": "runner_relaunched"}),
                )
                return
        route.continue_()

    page.route(
        re.compile(rf"/v1/sessions/{re.escape(session_id)}/events(\?|$)"),
        _intercept_events,
    )
    _force_runner_asleep(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=15_000)
    # runner_asleep tell: composer stays open with the reconnect placeholder,
    # NOT the host_offline "Session offline — reconnect" dead-end.
    expect(composer).to_have_attribute("placeholder", _RECONNECT_PLACEHOLDER, timeout=15_000)

    # The new affordance: a direct Attach banner (owner supplied the action).
    banner = page.get_by_test_id("runner-asleep-indicator")
    expect(banner).to_be_visible(timeout=15_000)
    expect(banner).to_contain_text("Agent disconnected")

    attach = banner.get_by_role("button", name="Attach")
    expect(attach).to_be_enabled()
    attach.click()

    # The click recovers in place — retry_session with no user message — and the
    # button holds pending while liveness is still runner_asleep.
    expect(banner.get_by_role("button", name=re.compile("Attaching"))).to_be_disabled(
        timeout=5_000
    )
    assert retry_calls, "Attach did not fire a retry_session event"
    assert retry_calls[0]["type"] == "retry_session"
