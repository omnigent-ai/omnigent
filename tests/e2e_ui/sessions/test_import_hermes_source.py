"""E2E: importing Hermes sessions from Settings › Import.

The Hermes source rides the same host-mediated local-import flow as the other
harnesses (``POST /v1/imports/local/stream``); what is Hermes-specific is the
source picker: the panel must offer a Hermes row and submit ``source: hermes``
so the host reads ``~/.hermes/state.db``. Like the sibling import tests, the
host round-trip is stubbed with ``page.route`` — the flow is a pure function of
the built bundle plus these stubs.
"""

from __future__ import annotations

import json

from playwright.sync_api import Page, Route, expect

_HOST_ID = "host_e2e"
_HOSTS_BODY = {
    "hosts": [{"host_id": _HOST_ID, "name": "e2e-host", "owner": "e2e", "status": "online"}]
}


def _fulfill_json(route: Route, body: dict[str, object]) -> None:
    route.fulfill(status=200, content_type="application/json", body=json.dumps(body))


def _fulfill_ndjson(route: Route, events: list[dict[str, object]]) -> None:
    """Fulfill the import POST with the endpoint's NDJSON stream shape."""
    body = "".join(json.dumps(e) + "\n" for e in events)
    route.fulfill(status=200, content_type="application/x-ndjson", body=body)


def test_settings_import_panel_offers_and_submits_hermes_source(
    page: Page,
    live_server: str,
) -> None:
    """Settings › Import: the harness list offers Hermes and submits it as the source."""
    captured: dict[str, object] = {}

    def _handle_import(route: Route) -> None:
        captured["post"] = route.request.post_data_json
        _fulfill_ndjson(
            route,
            [
                {"event": "session", "session_id": "conv_hermes_1", "title": "Hermes imported"},
                {"event": "done", "imported": 1, "already_imported": 0, "failed": 0},
            ],
        )

    page.route("**/v1/hosts", lambda r: _fulfill_json(r, _HOSTS_BODY))
    page.route("**/v1/imports/local/stream", _handle_import)

    page.goto(f"{live_server}/settings/import")

    expect(page.get_by_test_id("import-sessions-panel")).to_be_visible(timeout=30_000)
    page.get_by_test_id("import-source-select").click()
    # The harness picker lists Hermes alongside the other local sources.
    page.get_by_role("option", name="Hermes", exact=True).click()
    page.get_by_test_id("import-submit").click()

    expect(page.get_by_test_id("import-result")).to_contain_text("Imported 1", timeout=30_000)
    # The submitted harness selection is the Hermes source, not "all".
    assert captured["post"] == {"host_id": _HOST_ID, "source": "hermes", "limit": 25}
