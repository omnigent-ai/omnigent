"""E2E: the "+" → Shell affordance stays usable on a runner-asleep session.

When the web app believes a session's runner is offline but its host is up
(``runner_asleep`` liveness), the "+" ("Open new") menu must keep the "Shell"
item enabled — a wakeable session reconnects on create rather than dead-ending
on a disabled button or a "no runner available" 502. This pins the frontend
half of that behavior (``NewTabMenu`` in ``web/src/shell/WorkspacePanel.tsx``):
picking Shell while the UI reads runner-offline still opens a working shell.

Scope / honesty about the harness: the runner really is alive server-side here
(only the browser's *view* of liveness is patched runner-offline via
``/health``), so the create route's wake ladder (``ensure_runner_connected`` in
``_sessions/orchestration.py``) fast-paths on the live runner — the server-side
relaunch itself is NOT exercised (that needs host/managed-sandbox infrastructure
this harness doesn't stand up; it's covered by the backend unit test
``tests/server/routes/test_ensure_runner_connected.py``). What this proves is
the user-visible guard: perceived runner-offline liveness does not block shell
creation from the UI. The complementary "genuinely offline / can't reconnect
from the web" branch (Shell disabled + "Offline") is a pure frontend gate
covered by vitest (``web/src/shell/WorkspacePanel.test.tsx``).

Mirrors the liveness patching in ``files/test_offline_runner_host_served.py``
and ``sessions/test_host_badge.py``.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import open_right_rail

# Unix seconds well before now so a session whose runner reads offline is
# outside the startup grace and classifies runner_asleep (not "starting").
_OLD_CREATED_AT = 1_700_000_000


def _patch_runner_asleep(page: Page, session_id: str) -> None:
    """Patch the browser's view of ``session_id`` into runner-asleep.

    ``runner_online: false`` + ``host_online: true`` via ``/health`` is the
    ``runner_asleep`` liveness the feature targets — the host relaunches the
    runner on demand. The snapshot is patched host-bound with an old
    ``created_at`` so the session escapes the startup grace and reads asleep
    rather than "starting", and the sessions ``updates`` WS is blocked so a
    real stream push can't revert liveness to the (truly online) runner.

    :param page: Playwright page before navigation.
    :param session_id: Session id to patch.
    """

    def _patch_snapshot(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != f"/v1/sessions/{session_id}":
            route.continue_()
            return
        response = route.fetch()
        payload = response.json()
        payload["created_at"] = _OLD_CREATED_AT
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
        # Runner offline, host online → runner_asleep (host wakes it on demand).
        live = {"runner_online": False, "host_online": True}
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
    # matches most-recently-registered first); /health falls through via
    # continue_() for anything it doesn't own.
    page.route(re.compile(r"/health(\?|$)"), _patch_health)
    page.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}(\?|$)"), _patch_snapshot)
    page.route_web_socket(re.compile(r"/v1/sessions/updates"), lambda ws: None)


def test_new_shell_reconnects_asleep_runner(page: Page, terminal_session: tuple[str, str]) -> None:
    """ "+" → Shell on a runner-asleep session stays enabled and opens a shell.

    The UI believes the runner is offline (patched ``/health``), yet the "Shell"
    item stays enabled and picking it opens a working shell tab. A regression
    that re-gated shell creation on the UI's runner liveness would leave the
    item disabled (or the create 502-ing), so no shell tab would ever appear.
    (The server-side wake this rides on is unit-tested separately — see the
    module docstring.)
    """
    base_url, session_id = terminal_session
    _patch_runner_asleep(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")

    # The "+" menu's Shell item is NOT disabled just because the runner reads
    # offline — a wakeable session reconnects on create.
    rail.get_by_role("button", name="Open new").click()
    shell_item = page.get_by_role("menuitem", name=re.compile("Shell"))
    expect(shell_item).not_to_have_attribute("aria-disabled", "true")
    shell_item.click()

    # The shell opens as a rail tab and its xterm connects — proving the woken
    # runner served the create. Generous timeout: this waits on the wake ladder
    # plus the PTY spin-up, not just a connect.
    close_tab = rail.get_by_role("button", name=re.compile(r"^Close zsh · u-"))
    expect(close_tab).to_be_visible(timeout=60_000)
    terminal_view = rail.get_by_test_id("terminal-view").last
    expect(terminal_view).to_be_visible(timeout=20_000)
    expect(terminal_view).to_have_attribute("data-state", "connected", timeout=20_000)
