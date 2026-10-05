"""E2E: importing recent local sessions from Settings and the empty landing.

Two user-facing surfaces drive the host-mediated import (the chosen host reads
+ normalizes its own transcripts over the tunnel; the server persists each as
its frame arrives):

* Settings › "Import sessions" (``ImportSessionsPanel``) — pick a machine,
  harness, and count, then import via ``POST /v1/imports/local/stream``; the
  result lists each new session as its NDJSON frame lands.
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
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
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
        _fulfill_ndjson(
            route,
            [
                {"event": "session", "session_id": "conv_imp_1", "title": "First imported"},
                # A session with no synthesizable title still links.
                {"event": "session", "session_id": "conv_imp_2", "title": None},
                {"event": "done", "imported": 2, "already_imported": 0, "failed": 0},
            ],
        )

    page.route("**/v1/hosts", lambda r: _fulfill_json(r, _HOSTS_BODY))
    page.route("**/v1/imports/local/stream", _handle_import)

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


@pytest.mark.parametrize("submit_via", ["button", "enter"])
def test_settings_import_panel_replaces_exact_snapshot_after_confirmation(
    page: Page,
    live_server: str,
    submit_via: str,
) -> None:
    """Replacement is opt-in and confirmed from both exact-ID submit gestures."""
    captured: dict[str, object] = {}

    def _handle_import(route: Route) -> None:
        captured["post"] = route.request.post_data_json
        _fulfill_ndjson(
            route,
            [
                {"event": "session", "session_id": "conv_replaced", "title": "Latest"},
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
    session_input = page.get_by_test_id("import-session-id")
    session_input.fill("session-exact")
    page.get_by_test_id("import-replace-toggle").click()

    if submit_via == "button":
        page.get_by_test_id("import-submit").click()
    else:
        session_input.press("Enter")
    expect(page.get_by_role("dialog")).to_be_visible()
    expect(page.get_by_role("dialog")).to_contain_text("Omnigent-only changes")
    assert "post" not in captured

    if submit_via == "button":
        page.get_by_test_id("import-replace-cancel").click()
    else:
        page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert "post" not in captured

    if submit_via == "button":
        page.get_by_test_id("import-submit").click()
    else:
        session_input.press("Enter")
    page.get_by_test_id("import-replace-confirm").click()
    expect(page.get_by_test_id("import-result")).to_contain_text("Imported 1", timeout=30_000)
    assert captured["post"] == {
        "host_id": _HOST_ID,
        "source": "codex",
        "limit": 25,
        "session_id": "session-exact",
        "force": True,
    }


def test_replaced_exact_session_rehydrates_on_same_id_navigation(
    page: Page,
    live_server: str,
    output_path: str,
) -> None:
    """Replacement drops retained chat state before navigating back to its id."""
    external_id = f"e2e-replace-{uuid4().hex}"
    old_text = "old imported snapshot text"
    new_text = "new imported snapshot text"
    imported = httpx.post(
        f"{live_server}/v1/imports",
        json={
            "source": "claude",
            "external_session_id": external_id,
            "items": [
                {
                    "type": "message",
                    "response_id": "old-response",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": old_text}],
                    },
                }
            ],
        },
        timeout=30.0,
    )
    imported.raise_for_status()
    session_id = imported.json()["session_id"]

    def _handle_replacement(route: Route) -> None:
        replacement = httpx.post(
            f"{live_server}/v1/imports",
            json={
                "source": "claude",
                "external_session_id": external_id,
                "force": True,
                "items": [
                    {
                        "type": "message",
                        "response_id": "new-response",
                        "data": {
                            "role": "user",
                            "content": [{"type": "input_text", "text": new_text}],
                        },
                    }
                ],
            },
            timeout=30.0,
        )
        replacement.raise_for_status()
        _fulfill_ndjson(
            route,
            [
                {"event": "session", "session_id": session_id, "title": old_text},
                {"event": "done", "imported": 1, "already_imported": 0, "failed": 0},
            ],
        )

    page.route("**/v1/hosts", lambda r: _fulfill_json(r, _HOSTS_BODY))
    page.route("**/v1/imports/local/stream", _handle_replacement)

    page.goto(f"{live_server}/c/{session_id}")
    old_bubble = page.locator('[data-testid="message-bubble"][data-role="user"]').filter(
        has_text=old_text
    )
    expect(old_bubble).to_be_visible(timeout=30_000)

    page.evaluate("(id) => { window.__importDocument = id; }", external_id)
    page.get_by_role("link", name="Settings", exact=True).click()
    page.get_by_role("link", name="Import sessions", exact=True).click()
    expect(page.get_by_test_id("import-sessions-panel")).to_be_visible(timeout=30_000)
    page.get_by_test_id("import-mode-select").click()
    page.get_by_role("option", name="Session by ID").click()
    page.get_by_test_id("import-session-id").fill(external_id)
    page.get_by_test_id("import-replace-toggle").click()
    page.get_by_test_id("import-submit").click()
    page.screenshot(
        path=str(Path(output_path) / "replace-confirmation.png"), animations="disabled"
    )
    page.get_by_test_id("import-replace-confirm").click()
    expect(page.get_by_test_id("import-result")).to_contain_text("Imported 1", timeout=30_000)

    # The browser keeps the same URL id across this navigation. A stale
    # conversationRegistry entry would paint ``old_text`` and never fetch the
    # replacement's new item ids.
    page.get_by_test_id(f"import-result-link-{session_id}").click()
    new_bubble = page.locator('[data-testid="message-bubble"][data-role="user"]').filter(
        has_text=new_text
    )
    expect(new_bubble).to_be_visible(timeout=30_000)
    expect(old_bubble).to_have_count(0)
    assert page.evaluate("window.__importDocument") == external_id
    page.screenshot(path=str(Path(output_path) / "replacement-transcript.png"))


def test_replaced_exact_session_rehydrates_after_partial_stream_failure(
    page: Page,
    live_server: str,
    output_path: str,
) -> None:
    """A committed replacement stays fresh when its import stream fails afterward."""
    external_id = f"e2e-partial-replace-{uuid4().hex}"
    old_text = "old partial imported snapshot text"
    new_text = "new partial imported snapshot text"
    imported = httpx.post(
        f"{live_server}/v1/imports",
        json={
            "source": "claude",
            "external_session_id": external_id,
            "items": [
                {
                    "type": "message",
                    "response_id": "old-response",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": old_text}],
                    },
                }
            ],
        },
        timeout=30.0,
    )
    imported.raise_for_status()
    session_id = imported.json()["session_id"]

    def _handle_partial_failure(route: Route) -> None:
        replacement = httpx.post(
            f"{live_server}/v1/imports",
            json={
                "source": "claude",
                "external_session_id": external_id,
                "force": True,
                "items": [
                    {
                        "type": "message",
                        "response_id": "new-response",
                        "data": {
                            "role": "user",
                            "content": [{"type": "input_text", "text": new_text}],
                        },
                    }
                ],
            },
            timeout=30.0,
        )
        replacement.raise_for_status()
        _fulfill_ndjson(
            route,
            [
                {"event": "session", "session_id": session_id, "title": new_text},
                {"event": "error", "message": "host disconnected after replacement"},
                {"event": "done", "imported": 1, "already_imported": 0, "failed": 0},
            ],
        )

    page.route("**/v1/hosts", lambda r: _fulfill_json(r, _HOSTS_BODY))
    page.route("**/v1/imports/local/stream", _handle_partial_failure)

    page.goto(f"{live_server}/c/{session_id}")
    old_bubble = page.locator('[data-testid="message-bubble"][data-role="user"]').filter(
        has_text=old_text
    )
    expect(old_bubble).to_be_visible(timeout=30_000)

    page.evaluate("(id) => { window.__importDocument = id; }", external_id)
    page.get_by_role("link", name="Settings", exact=True).click()
    page.get_by_role("link", name="Import sessions", exact=True).click()
    expect(page.get_by_test_id("import-sessions-panel")).to_be_visible(timeout=30_000)
    page.get_by_test_id("import-mode-select").click()
    page.get_by_role("option", name="Session by ID").click()
    page.get_by_test_id("import-session-id").fill(external_id)
    page.get_by_test_id("import-replace-toggle").click()
    page.get_by_test_id("import-submit").click()
    page.get_by_test_id("import-replace-confirm").click()
    expect(page.get_by_test_id("import-error")).to_contain_text(
        "host disconnected after replacement", timeout=30_000
    )
    expect(page.get_by_test_id(f"import-result-link-{session_id}")).to_be_visible()
    page.screenshot(path=str(Path(output_path) / "partial-import-result.png"))

    # The replacement committed before the stream error. Same-id navigation
    # must therefore bind the new transcript even though the API rejected.
    page.get_by_test_id(f"import-result-link-{session_id}").click()
    new_bubble = page.locator('[data-testid="message-bubble"][data-role="user"]').filter(
        has_text=new_text
    )
    expect(new_bubble).to_be_visible(timeout=30_000)
    expect(old_bubble).to_have_count(0)
    assert page.evaluate("window.__importDocument") == external_id
    page.screenshot(path=str(Path(output_path) / "partial-replacement-transcript.png"))


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
