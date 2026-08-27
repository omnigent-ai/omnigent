"""E2E: importing recent local sessions from Settings and the empty landing.

Two new user-facing surfaces drive ``POST /v1/imports/local`` (the chosen host
reads + normalizes its own transcripts over the tunnel; the server persists
each as its frame arrives):

* Settings › "Import sessions" (``ImportSessionsPanel``) — pick a machine,
  harness, and count, then import; the result links each new session.
* The empty landing (``NewChatLandingScreen``) — a one-click "Import your N
  most recent sessions" plus a "Choose what to import" link into Settings.

The transcripts live on the caller's machine and the host round-trip needs a
live tunnel, so — like the visual and ``start_session`` suites — these stub the
landing's data endpoints (``/v1/hosts``, ``/v1/sessions``) and the import POST
with ``page.route``. That makes the flow a pure function of the built bundle +
these stubs, exercising the real UI wiring without a real host read.
"""

from __future__ import annotations

import json
import re

from playwright.sync_api import Page, Route, expect

_HOST_ID = "host_e2e"
_HOSTS_BODY = {
    "hosts": [{"host_id": _HOST_ID, "name": "e2e-host", "owner": "e2e", "status": "online"}]
}
# Bare session list/scan endpoint, but NOT ``/v1/sessions/{id}/...`` nor the
# ``/v1/sessions/updates`` WebSocket. Stubbed empty so the landing reads as the
# no-sessions empty state (``live_server`` is session-scoped, so other tests'
# sessions would otherwise leak in).
_SESSIONS_RE = re.compile(r"/v1/sessions(\?.*)?$")
_EMPTY_LIST_BODY = {"object": "list", "data": [], "has_more": False}


def _fulfill_json(route: Route, body: dict[str, object]) -> None:
    route.fulfill(status=200, content_type="application/json", body=json.dumps(body))


def test_settings_import_panel_imports_and_links_sessions(
    page: Page,
    live_server: str,
) -> None:
    """Settings › Import: submit imports the current host's recent sessions and links them.

    :param page: Playwright page fixture (fresh context per test).
    :param live_server: Base URL of the spawned server serving the built SPA.
    """
    captured: dict[str, object] = {}

    def _handle_import(route: Route) -> None:
        captured["post"] = route.request.post_data_json
        _fulfill_json(
            route,
            {
                "imported": 2,
                "already_imported": 0,
                "failed": 0,
                "sessions": [
                    {"session_id": "conv_imp_1", "title": "First imported"},
                    # A session with no synthesizable title still links.
                    {"session_id": "conv_imp_2", "title": None},
                ],
            },
        )

    page.route("**/v1/hosts", lambda r: _fulfill_json(r, _HOSTS_BODY))
    page.route("**/v1/imports/local", _handle_import)

    page.goto(f"{live_server}/settings/import")

    # An online host is present, so the panel (not the "no machines" notice)
    # renders with its machine / harness / count pickers.
    expect(page.get_by_test_id("import-sessions-panel")).to_be_visible(timeout=30_000)
    expect(page.get_by_test_id("import-source-select")).to_be_visible()
    expect(page.get_by_test_id("import-limit-select")).to_be_visible()

    page.get_by_test_id("import-submit").click()

    expect(page.get_by_test_id("import-result")).to_contain_text("Imported 2", timeout=30_000)
    expect(page.get_by_test_id("import-result-link-conv_imp_1")).to_contain_text("First imported")
    # The null-title session links under the placeholder label rather than 500-ing.
    expect(page.get_by_test_id("import-result-link-conv_imp_2")).to_contain_text(
        "Untitled session"
    )

    # Panel defaults: the online host, all harnesses, the 25-session count.
    assert captured["post"] == {"host_id": _HOST_ID, "source": "all", "limit": 25}


def test_empty_landing_choose_what_to_import_opens_settings(
    page: Page,
    live_server: str,
) -> None:
    """Empty landing: "Choose what to import" navigates into Settings › Import."""
    page.route(_SESSIONS_RE, lambda r: _fulfill_json(r, _EMPTY_LIST_BODY))
    page.route("**/v1/hosts", lambda r: _fulfill_json(r, {"hosts": []}))

    page.goto(f"{live_server}/")

    expect(page.get_by_test_id("new-chat-landing")).to_be_visible(timeout=30_000)
    # No sessions yet, so the landing offers the import affordances.
    expect(page.get_by_test_id("landing-quick-import")).to_be_visible(timeout=30_000)
    page.get_by_test_id("landing-import-sessions").click()

    page.wait_for_url("**/settings/import", timeout=30_000)
    # With no online host the panel shows the connect-a-machine notice, proving
    # the section mounted (rather than the full picker) — either is fine here.
    expect(page.get_by_test_id("import-no-hosts")).to_be_visible(timeout=30_000)


def test_empty_landing_quick_import_imports_recent_sessions(
    page: Page,
    live_server: str,
) -> None:
    """Empty landing: the one-click button imports from the online host and reports the count."""

    def _handle_import(route: Route) -> None:
        _fulfill_json(
            route,
            {
                "imported": 3,
                "already_imported": 0,
                "failed": 0,
                "sessions": [
                    {"session_id": f"conv_q_{i}", "title": f"Session {i}"} for i in range(3)
                ],
            },
        )

    page.route(_SESSIONS_RE, lambda r: _fulfill_json(r, _EMPTY_LIST_BODY))
    page.route("**/v1/hosts", lambda r: _fulfill_json(r, _HOSTS_BODY))
    page.route("**/v1/imports/local", _handle_import)

    page.goto(f"{live_server}/")

    quick_import = page.get_by_test_id("landing-quick-import")
    expect(quick_import).to_be_visible(timeout=30_000)
    quick_import.click()

    expect(page.get_by_test_id("landing-quick-import-result")).to_contain_text(
        "Imported 3 sessions", timeout=30_000
    )
