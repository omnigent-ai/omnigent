"""E2E: importing recent local sessions from Settings and the empty landing.

Two user-facing surfaces drive the host-mediated import (the chosen host reads
+ normalizes its own transcripts over the tunnel; the server persists each as
its frame arrives):

* Settings › "Import sessions" (``ImportSessionsPanel``) — pick a machine,
  harness, and count, then import via ``POST /v1/imports/local/stream``; the
  result shows an aggregate count with a link to the normal session list.
* The empty landing (``NewChatLandingScreen``) — a single "Import your recent
  sessions" button that opens Settings › Import.

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


def _fulfill_ndjson(route: Route, events: list[dict[str, object]]) -> None:
    """Fulfill the import POST with the endpoint's NDJSON stream shape."""
    body = "".join(json.dumps(e) + "\n" for e in events)
    route.fulfill(status=200, content_type="application/x-ndjson", body=body)


def test_settings_import_panel_shows_compact_summary(
    page: Page,
    live_server: str,
) -> None:
    """Settings › Import keeps large batches compact and links to the normal list.

    :param page: Playwright page fixture (fresh context per test).
    :param live_server: Base URL of the spawned server serving the built SPA.
    """
    captured: dict[str, object] = {}

    def _handle_import(route: Route) -> None:
        captured["post"] = route.request.post_data_json
        _fulfill_ndjson(
            route,
            [
                {"event": "session", "session_id": f"conv_imp_{i}", "title": f"Imported {i}"}
                for i in range(35)
            ]
            + [{"event": "done", "imported": 35, "already_imported": 0, "failed": 0}],
        )

    page.route("**/v1/hosts", lambda r: _fulfill_json(r, _HOSTS_BODY))
    page.route("**/v1/imports/local/stream", _handle_import)

    page.goto(f"{live_server}/settings/import")

    # An online host is present, so the panel (not the "no machines" notice)
    # renders with its machine / harness / count pickers.
    expect(page.get_by_test_id("import-sessions-panel")).to_be_visible(timeout=30_000)
    expect(page.get_by_test_id("import-source-select")).to_be_visible()
    expect(page.get_by_test_id("import-limit-select")).to_be_visible()

    page.get_by_test_id("import-limit-select").click()
    page.get_by_role("option", name="Last 50", exact=True).click()
    page.get_by_test_id("import-submit").click()

    expect(page.get_by_test_id("import-result")).to_contain_text("Imported 35", timeout=30_000)
    panel = page.get_by_test_id("import-sessions-panel")
    expect(panel.get_by_role("list")).to_have_count(0)
    expect(panel.get_by_role("link", name="View sessions")).to_have_attribute("href", "/")
    assert captured["post"] == {"host_id": _HOST_ID, "source": "all", "limit": 50}


def test_settings_import_panel_imports_one_session_by_id(
    page: Page,
    live_server: str,
) -> None:
    """Settings can import an exact harness session without showing a session list."""
    captured: dict[str, object] = {}

    def _handle_import(route: Route) -> None:
        captured["post"] = route.request.post_data_json
        _fulfill_ndjson(
            route,
            [
                {"event": "session", "session_id": "conv_exact", "title": "Exact import"},
                {"event": "done", "imported": 1, "already_imported": 0, "failed": 0},
            ],
        )

    page.route("**/v1/hosts", lambda r: _fulfill_json(r, _HOSTS_BODY))
    page.route("**/v1/imports/local/stream", _handle_import)
    page.goto(f"{live_server}/settings/import")

    expect(page.get_by_test_id("import-sessions-panel")).to_be_visible(timeout=30_000)
    page.get_by_test_id("import-mode-select").click()
    page.get_by_role("option", name="Session by ID").click()
    page.get_by_test_id("import-source-select").click()
    page.get_by_role("option", name="Codex").click()
    page.get_by_test_id("import-session-id").fill("session-exact")
    page.get_by_test_id("import-submit").click()

    expect(page.get_by_test_id("import-result")).to_contain_text("Imported 1", timeout=30_000)
    assert captured["post"] == {
        "host_id": _HOST_ID,
        "source": "codex",
        "limit": 25,
        "session_id": "session-exact",
    }


def test_empty_landing_import_button_opens_settings(
    page: Page,
    live_server: str,
) -> None:
    """Empty landing: the single import button navigates into Settings › Import."""
    page.route(_SESSIONS_RE, lambda r: _fulfill_json(r, _EMPTY_LIST_BODY))
    page.route("**/v1/hosts", lambda r: _fulfill_json(r, {"hosts": []}))

    page.goto(f"{live_server}/")

    expect(page.get_by_test_id("new-chat-landing")).to_be_visible(timeout=30_000)
    # No sessions yet, so the landing offers the single import affordance.
    import_button = page.get_by_test_id("landing-import-sessions")
    expect(import_button).to_be_visible(timeout=30_000)
    expect(import_button).to_contain_text("Import your recent sessions")
    import_button.click()

    page.wait_for_url("**/settings/import", timeout=30_000)
    # With no online host the panel shows the connect-a-machine notice, proving
    # the section mounted (rather than the full picker) — either is fine here.
    expect(page.get_by_test_id("import-no-hosts")).to_be_visible(timeout=30_000)
