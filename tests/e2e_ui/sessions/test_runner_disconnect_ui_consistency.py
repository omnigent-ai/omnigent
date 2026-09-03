"""UI consistency after a runner disconnects mid-turn on a host-bound session.

Without the fix, the UI could enter a self-contradictory state:

  1. The error banner said the connection dropped.
  2. The "Working…" / "Pondering…" shimmer was STILL visible.
  3. The host badge below the composer showed the host as CONNECTED (green).

Items 1 and 2 are inconsistent: the error says the turn was interrupted, but
the shimmer implies work is still in progress.  Items 1 and 3 clashed because
the error copy blamed the HOST ("the connection to the host dropped") while
the badge — correctly — showed the host online: only the runner dropped.

The race that produces the state:

  1. Runner disconnects.  The server starts the disconnect-grace timer.
  2. The client's SSE connection drops (transport-level).  The reconnect loop
     fires and calls ``reconcileOnReconnect``, which force-fetches the session
     snapshot.
  3. Within the grace window the snapshot still reports
     ``session.status = "running"`` and carries the active ``response_id``.
  4. ``reconnectStatusPatch`` sees a running turn and re-opens the streaming
     lifecycle: ``status = "streaming"``, ``sessionStatus = "running"``.
     The "Pondering…" shimmer reappears.
  5. ``itemsToBlocks`` also sees the persisted ``runner_disconnected`` error
     item (written by the relay before the stream dropped) and appends it to
     ``blocks``.  The error banner is now visible alongside the shimmer.
  6. Meanwhile the health poll reports ``runner_online=false, host_online=true``
     so the host badge correctly shows the host as connected.

This test simulates the grace-window server state via route interception (same
approach as ``test_host_badge.py``) and asserts the UI invariants: the
"Working…" indicator must not be present at the same time as a
``runner_disconnected`` error banner, and the error copy must not blame the
host (the badge tells the true story — the host IS up).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import _server_state, seed_committed_turn

# A fake host id the patched session is bound to.
_FAKE_HOST_ID = "host_disconnect_consistency"
# Unix seconds well before now so the session is past STARTING_GRACE_S (45 s).
_OLD_CREATED_AT = 1_700_000_000
# A synthetic in-flight response id that makes the snapshot look "running".
_FAKE_RESPONSE_ID = "resp_disconnect_inflight"


def _seed_runner_disconnect_error(session_id: str) -> None:
    """Append a committed ``runner_disconnected`` error item to the session.

    Mirrors ``_seed_error_item`` in ``test_failure_error_card.py``.  Writes
    directly to the server's database so the client sees the persisted item
    when it fetches ``/v1/sessions/{id}/items``.

    :param session_id: Session to append to.
    :raises RuntimeError: If running against ``--ui-base-url`` (no local DB).
    """
    from omnigent.entities import ErrorData, NewConversationItem
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    database_uri = _server_state.get("database_uri")
    if not database_uri:
        raise RuntimeError(
            "seeding an error item needs the spawned server's database; "
            "unavailable when running against --ui-base-url."
        )
    SqlAlchemyConversationStore(str(database_uri)).append(
        session_id,
        [
            NewConversationItem(
                type="error",
                response_id=_FAKE_RESPONSE_ID,
                data=ErrorData(
                    source="execution",
                    code="runner_disconnected",
                    message="Runner disconnected unexpectedly.",
                ),
            ),
        ],
    )


@pytest.fixture(autouse=True)
def _drop_routes(page: Page) -> Iterator[None]:
    """Unregister route handlers before the page closes.

    In-flight ``/health`` polls and snapshot refetches can call route
    handlers as the page tears down, raising ``TargetClosedError`` that
    surfaces in the *next* test's setup.  Unroute everything up-front to
    prevent that.

    :param page: Playwright page fixture.
    :returns: Iterator yielding once, then unrouting.
    """
    yield
    page.unroute_all(behavior="ignoreErrors")


def _patch_grace_window_state(
    page: Page,
    session_id: str,
    *,
    host_name: str = "e2e-grace-host",
) -> None:
    """Patch the browser into the disconnect-grace-window server state.

    Simulates the moment AFTER the runner dropped but BEFORE the server's
    grace timer fires and marks the session ``failed``:

    - ``GET /v1/sessions/{id}``  →  snapshot with ``status="running"``,
      ``active_response_id`` set, ``host_id`` set to ``_FAKE_HOST_ID``,
      and an old ``created_at`` (outside the startup grace).
    - ``GET /v1/hosts``          →  the bound host record.
    - ``GET /health``            →  ``runner_online=false``, ``host_online=true``.
    - ``GET /v1/sessions``       →  the session is dropped from the sidebar
      list so the open-session row falls back to the patched snapshot.
    - ``WS /v1/sessions/updates``→  blocked so a WS push can't revert state.

    :param page: Playwright page before navigation.
    :param session_id: Session id to patch.
    :param host_name: Friendly name for the bound host.
    """

    def _patch_snapshot(route: Route) -> None:
        req = route.request
        if req.method != "GET" or urlparse(req.url).path != f"/v1/sessions/{session_id}":
            route.continue_()
            return
        resp = route.fetch()
        payload = resp.json()
        # Force the grace-window state: runner down, host up, turn "running".
        payload["host_id"] = _FAKE_HOST_ID
        payload["host_resumable"] = False
        payload["created_at"] = _OLD_CREATED_AT
        payload["status"] = "running"
        payload["active_response_id"] = _FAKE_RESPONSE_ID
        route.fulfill(
            status=200,
            headers={**resp.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    def _patch_hosts(route: Route) -> None:
        req = route.request
        if req.method != "GET" or urlparse(req.url).path != "/v1/hosts":
            route.continue_()
            return
        route.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps(
                {
                    "hosts": [
                        {
                            "host_id": _FAKE_HOST_ID,
                            "name": host_name,
                            "owner": "e2e",
                            "status": "online",
                            "sandbox_provider": None,
                        }
                    ]
                }
            ),
        )

    def _patch_health(route: Route) -> None:
        req = route.request
        if req.method != "GET" or urlparse(req.url).path != "/health":
            route.continue_()
            return
        resp = route.fetch()
        payload = resp.json()
        # Runner dropped, host still up — the exact grace-window liveness.
        live = {"runner_online": False, "host_online": True}
        if isinstance(payload.get("sessions"), dict):
            payload["sessions"][session_id] = live
        if isinstance(payload.get("session"), dict):
            payload["session"] = {**payload["session"], **live}
        route.fulfill(
            status=200,
            headers={**resp.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    def _patch_list(route: Route) -> None:
        req = route.request
        if req.method != "GET" or urlparse(req.url).path != "/v1/sessions":
            route.continue_()
            return
        resp = route.fetch()
        payload = resp.json()
        rows = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(rows, list):
            payload["data"] = [
                r for r in rows if not (isinstance(r, dict) and r.get("id") == session_id)
            ]
        route.fulfill(
            status=200,
            headers={**resp.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    # Snapshot patch last: Playwright matches most-recently-registered first.
    page.route(re.compile(r"/v1/hosts(\?|$)"), _patch_hosts)
    page.route(re.compile(r"/v1/sessions(\?|$)"), _patch_list)
    page.route(re.compile(r"/health(\?|$)"), _patch_health)
    page.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}(\?|$)"), _patch_snapshot)
    page.route_web_socket(re.compile(r"/v1/sessions/updates"), lambda ws: None)


def test_working_indicator_absent_when_runner_disconnect_error_present(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Working shimmer must not appear together with a runner-disconnect error.

    In the disconnect-grace window the client reconnects and sees
    ``session.status="running"`` in the snapshot, re-opens the streaming
    lifecycle, and renders the "Pondering…" shimmer — while at the same time
    the persisted ``runner_disconnected`` error item is loaded and the error
    banner renders.  The two states are mutually exclusive: if the turn was
    interrupted, it is not still working.

    This test reproduces the grace-window state via route interception and
    asserts the invariant holds: the working indicator is absent whenever
    the runner-disconnected error banner is present.  It also pins the error
    copy: the banner must not blame the host (only the runner dropped — the
    host badge correctly shows the host online).

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server.
    :returns: None.
    """
    base_url, session_id = seeded_session

    # Seed a prior committed turn (so the session isn't empty) and then the
    # runner_disconnected error that follows it.
    seed_committed_turn(
        session_id,
        prompt="Run a long analysis",
        reply="Starting analysis…",
        response_id="resp_disconnect_prior",
    )
    _seed_runner_disconnect_error(session_id)

    # Patch the browser into the grace-window state before navigating.
    _patch_grace_window_state(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    # The error banner must be visible (the persisted item hydrates it) and
    # must not blame the host: the host is online, only the runner dropped.
    error_pill = page.get_by_test_id("error-pill")
    expect(error_pill).to_be_visible(timeout=20_000)
    expect(error_pill).to_contain_text("agent's process disconnected")
    expect(error_pill).not_to_contain_text("connection to the host dropped")

    # THE BUG: the working shimmer must NOT be visible when the error is shown.
    # Before the fix, reconnectStatusPatch re-opens the streaming lifecycle
    # because the snapshot still reports status="running" (grace window), and
    # computeShowsWorking let it through even with runner_online=false because
    # sessionStatus="running" (isWorking=true) bypasses the runnerOnline gate.
    working = page.locator('[data-testid="working-indicator"]')
    expect(working).to_have_count(0, timeout=5_000)


def test_host_badge_shows_connected_when_runner_drops_but_host_is_up(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Host badge accurately shows the host as online when only the runner dropped.

    The old error copy ("The connection to the host dropped unexpectedly")
    implied the HOST dropped, but on a host-bound session the host tunnel
    (``host_online=true``) can stay up while only the runner tunnel drops.
    The host badge must reflect reality: the host IS connected.

    This pins the badge behaviour so the error-copy fix (which now blames the
    agent's process, not the host) keeps agreeing with the badge.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server.
    :returns: None.
    """
    base_url, session_id = seeded_session

    seed_committed_turn(
        session_id,
        prompt="Run something",
        reply="On it…",
        response_id="resp_disconnect_badge",
    )

    _patch_grace_window_state(page, session_id, host_name="e2e-badge-host")

    page.goto(f"{base_url}/c/{session_id}")

    # Host badge must be present and show the host name (not generic copy).
    badge = page.get_by_test_id("host-badge")
    expect(badge).to_be_visible(timeout=20_000)
    expect(badge).to_contain_text("e2e-badge-host")

    # The host IS online — badge must NOT say "offline".
    # (The runner is offline, but the badge is about the host.)
    expect(badge).not_to_contain_text("offline")
