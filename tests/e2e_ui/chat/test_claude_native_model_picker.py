"""E2E: the Claude Code (claude-native) model picker offers and selects Fable.

Covers the user-facing behavior this PR un-withheld: ``Fable`` is a
selectable row in the Claude Code model picker (``CLAUDE_NATIVE_MODELS`` in
``web/src/lib/claudeNativeModels.ts``). The flow: open the picker, confirm
the Fable row renders and is selectable, click it, and confirm the pick takes
effect — the session ``model_override`` PATCH carries ``"fable"`` and the row
plus the composer trigger settle on Fable.

Like ``test_codex_model_metadata.py`` this fakes the session snapshot with a
route patch rather than booting a real Claude CLI (which needs credentials CI
does not have — see ``test_fork_switch_agent.py`` and ``COVERAGE_GAPS.md``).
The picker keys purely off the ``omnigent.wrapper`` label and the static local
model catalog, so patching the snapshot exercises the real picker UI.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect


def _patch_session_as_claude_native(page: Page, session_id: str) -> list[dict]:
    """Patch the browser's session snapshot into a claude-native response.

    The server fixture seeds a normal ``hello_world`` (openai-agents) session
    so the page boots against the real app/server. This route patch changes
    only ``GET`` and ``PATCH /v1/sessions/{session_id}`` responses as seen by
    the browser, presenting the session as a Claude Code (claude-native)
    wrapper bound to Opus. Selecting a model then PATCHes ``model_override``;
    the handler echoes the new value back so the picker settles on it.

    :param page: Playwright page before navigation.
    :param session_id: Session id to patch, e.g. ``"conv_abc123"``.
    :returns: Captured PATCH request bodies.
    """
    latest_model_override: str | None = None
    patch_bodies: list[dict] = []

    def _handle(route: Route) -> None:
        nonlocal latest_model_override
        request = route.request
        parsed = urlparse(request.url)
        if parsed.path != f"/v1/sessions/{session_id}":
            route.continue_()
            return

        headers = {"content-type": "application/json"}
        if request.method == "GET":
            response = route.fetch()
            payload = response.json()
            headers = {**response.headers, **headers}
        elif request.method == "PATCH":
            request_body = json.loads(request.post_data or "{}")
            patch_bodies.append(request_body)
            response = route.fetch()
            payload = response.json()
            headers = {**response.headers, **headers}
            if "model_override" in request_body:
                # "default" is the wire clear alias; treat it as "no override".
                sent = request_body["model_override"]
                latest_model_override = None if sent == "default" else sent
        else:
            route.continue_()
            return

        payload["labels"] = {
            **payload.get("labels", {}),
            "omnigent.wrapper": "claude-code-native-ui",
        }
        payload["harness"] = "claude"
        # Bind the session to Opus so it is the initially-active row: Fable is
        # NOT selected until the user picks it, which is what this test proves.
        payload["llm_model"] = "opus"
        payload["model_override"] = latest_model_override
        route.fulfill(
            status=200,
            headers=headers,
            body=json.dumps(payload),
        )

    page.route("**/v1/sessions/**", _handle)
    return patch_bodies


def test_claude_native_picker_selects_fable(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Open the Claude Code model picker, pick Fable, and confirm it applies.

    Fable is the top-tier alias this PR un-withheld in
    ``CLAUDE_NATIVE_MODELS``. It must render as a selectable picker row and,
    when chosen, drive the session ``model_override`` PATCH and become the
    active model both in the dropdown and on the composer trigger.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a real
        server-backed session; the browser snapshot is patched to
        claude-native.
    :returns: None.
    """
    base_url, session_id = seeded_session
    patch_bodies = _patch_session_as_claude_native(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    # The composer identity resolves to Claude, and the picker trigger starts
    # on the bound Opus model (Fable is not selected yet).
    expect(page.get_by_test_id("composer-harness")).to_contain_text("Claude", timeout=15_000)
    trigger = page.get_by_test_id("agent-picker-trigger")
    expect(trigger).to_contain_text("Opus", timeout=15_000)

    # Open the model picker: the Fable row renders, is labelled, and is not
    # yet the active selection.
    trigger.click()
    fable_row = page.locator('[data-testid="model-picker-item"][data-model-id="fable"]')
    expect(fable_row).to_be_visible()
    expect(fable_row).to_contain_text("Fable")
    expect(fable_row).not_to_have_attribute("data-active", "true")

    # Selecting Fable PATCHes the session model override with the bare alias.
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and urlparse(response.url).path == f"/v1/sessions/{session_id}"
            and response.status == 200
        )
    ):
        fable_row.click()

    assert patch_bodies[-1] == {"model_override": "fable"}, patch_bodies

    # The pick took effect: the composer trigger now names Fable.
    expect(trigger).to_contain_text("Fable")

    # Reopening the picker shows the Fable row as the active selection. Wait
    # for the just-selected menu to fully close (its items detach) before
    # reopening, so the reopen click can't race Radix's dismiss layer.
    expect(page.locator('[data-testid="model-picker-item"]')).to_have_count(0)
    trigger.click()
    active_row = page.locator('[data-testid="model-picker-item"][data-model-id="fable"]')
    expect(active_row).to_be_visible()
    expect(active_row).to_have_attribute("data-active", "true")
