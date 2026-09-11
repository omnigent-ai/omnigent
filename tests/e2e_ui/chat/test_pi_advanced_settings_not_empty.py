"""E2E: a Pi session's Advanced settings dialog must not open empty.

After the composer-menu rework moved the Model/Effort rows into the inline
quick-switch menus, the
"Advanced settings…" dialog kept only harness-specific rows (Claude
permissions, Codex approvals) plus the deployment-gated sub-agent routing
row. A Pi session qualifies for none of them, so the composer menu offers a
dialog that opens with no content at all.

The test drives the real SPA through the reported journey — open the
composer's "Configure session" menu on a Pi session, click "Advanced
settings…" — with the session shaped as pi-native via the suite's standard
route-patch idiom (see ``test_codex_approval_mode_picker.py`` and
``test_harness_render_smoke.py``; the snapshot values mirror the render-smoke
matrix's pi case). It asserts the FIXED behavior in either acceptable shape:

* the menu stops offering a dead "Advanced settings…" entry for sessions
  with no applicable rows, or
* the dialog it opens contains at least one configuration control.

On the buggy build the entry is offered and the dialog opens empty, so this
fails with the empty-dialog message.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry

# Canonical pi-native snapshot shape: the wrapper label the real Pi wrapper
# stamps at session create (``omnigent/_wrapper_labels.py``), plus the
# runner-backed model options a live Pi session serves. Values mirror the
# ``pi`` case of ``test_harness_render_smoke.py``.
_PI_WRAPPER = "pi-native-ui"
_PI_HARNESS = "pi"
_PI_MODEL = "omnigent-openai/system.ai.gpt-5-6-sol"
_PI_MODEL_OPTIONS = [
    {
        "id": _PI_MODEL,
        "model": _PI_MODEL,
        "displayName": "system.ai.gpt-5-6-sol",
    }
]


def _patch_session_as_pi_native(page: Page, session_id: str) -> None:
    """Shape the browser's view of *session_id* into a pi-native snapshot.

    Patches only ``GET`` / ``PATCH /v1/sessions/{session_id}`` as the browser
    sees it (query strings included — the gear modal's ``getSessionSlim``
    refresh hits the same path), injecting the ``pi-native-ui`` wrapper label
    and Pi's runner-backed ``model_options`` verbatim.

    :param page: Playwright page, before navigation.
    :param session_id: Seeded session id to reshape.
    """
    latest_payload: dict | None = None

    def _handle(route: Route) -> None:
        nonlocal latest_payload
        request = route.request
        if urlparse(request.url).path != f"/v1/sessions/{session_id}":
            route.continue_()
            return
        headers = {"content-type": "application/json"}
        if request.method == "GET":
            response = fetch_with_retry(route)
            payload = response.json()
            headers = {**response.headers, **headers}
        elif request.method == "PATCH":
            request_body = json.loads(request.post_data or "{}")
            payload = dict(latest_payload or {})
            if "model_override" in request_body:
                payload["model_override"] = request_body["model_override"]
        else:
            route.continue_()
            return
        payload["labels"] = {**payload.get("labels", {}), "omnigent.wrapper": _PI_WRAPPER}
        payload["harness"] = _PI_HARNESS
        payload["llm_model"] = _PI_MODEL
        payload["model_options"] = _PI_MODEL_OPTIONS
        latest_payload = dict(payload)
        route.fulfill(status=200, headers=headers, body=json.dumps(payload))

    page.route("**/v1/sessions/**", _handle)


def test_pi_advanced_settings_dialog_is_not_empty(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The Advanced settings dialog offered to a Pi session must have content.

    Journey: open a Pi session → click the harness/model name in the composer
    ("Configure session") → click "Advanced settings…". The dialog that opens
    must contain at least one configuration control; a menu that no longer
    offers the entry at all (nothing applicable to configure) also passes.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real
        server-backed session; the browser snapshot is patched to pi-native.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _patch_session_as_pi_native(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    # Step: click the harness/model name in the composer ("Configure session").
    gear = page.get_by_test_id("composer-config-gear")
    expect(gear).to_be_visible(timeout=15_000)
    gear.click()
    expect(page.get_by_test_id("composer-agent-menu")).to_be_visible(timeout=10_000)

    # Fixed shape (a): the menu stops offering settings it cannot show.
    advanced = page.get_by_test_id("composer-advanced-settings")
    if advanced.count() == 0:
        return

    # Step: click "Advanced settings…".
    advanced.click()
    modal = page.get_by_test_id("composer-config-modal")
    expect(modal).to_be_visible(timeout=10_000)

    # Fixed shape (b): the dialog carries at least one configuration control.
    # Every gear-modal row renders an interactive control (Radix Select
    # triggers are ``role="combobox"``); the dialog chrome — the close X and
    # the Cancel/Save footer — are plain buttons, so they never match. On the
    # buggy build a Pi session matches nothing and this fails: the dialog is
    # empty.
    controls = modal.locator(
        '[role="combobox"], [role="switch"], [role="checkbox"], '
        '[role="radiogroup"], input, select, textarea'
    )
    expect(
        controls.first,
        "Advanced settings dialog opened empty for a Pi session: "
        "it offers no configuration controls",
    ).to_be_visible(timeout=5_000)
