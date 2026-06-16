"""E2E: codex-native model controls render Codex-returned metadata raw."""

from __future__ import annotations

import json
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect


def _patch_session_as_codex_native(page: Page, session_id: str) -> None:
    """Patch the browser's session snapshot into a codex-native response.

    The server fixture seeds a normal ``hello_world`` session so the page can
    boot against the real app/server. This route patch changes only
    ``GET /v1/sessions/{session_id}`` responses as seen by the browser,
    simulating the AP snapshot after a codex-native runner has returned raw
    Codex ``model/list`` metadata.

    :param page: Playwright page before navigation.
    :param session_id: Session id to patch, e.g. ``"conv_abc123"``.
    :returns: None.
    """

    def _handle(route: Route) -> None:
        request = route.request
        parsed = urlparse(request.url)
        if request.method != "GET" or parsed.path != f"/v1/sessions/{session_id}":
            route.continue_()
            return

        response = route.fetch()
        payload = response.json()
        payload["labels"] = {
            **payload.get("labels", {}),
            "omnigent.wrapper": "codex-native-ui",
        }
        payload["harness"] = "codex"
        payload["llm_model"] = "gpt-5.5"
        payload["reasoning_effort"] = "xhigh"
        payload["codex_model_options"] = [
            {
                "id": "gpt-5.5",
                "model": "databricks-gpt-5-5",
                "displayName": "Codex Pretty 5.5",
                "defaultReasoningEffort": "xhigh",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "low", "description": "Low from Codex"},
                    {
                        "reasoningEffort": "xhigh",
                        "description": "Raw xhigh from Codex",
                        "codexOnly": True,
                    },
                ],
                "isDefault": True,
                "vendorMetadata": {"source": "codex"},
            }
        ]
        headers = {**response.headers, "content-type": "application/json"}
        route.fulfill(
            status=response.status,
            headers=headers,
            body=json.dumps(payload),
        )

    page.route("**/v1/sessions/**", _handle)


def test_codex_native_picker_uses_raw_model_metadata(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Render Codex's display name and effort id without local conversion.

    This covers the user-facing path that triggered the PR cleanup: the
    session snapshot carries raw Codex ``model/list`` objects, the model menu
    uses Codex's ``displayName`` when present, and the Codex effort row is not
    visually title-cased by the shared effort-menu styling.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser snapshot is patched to codex-native.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _patch_session_as_codex_native(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    expect(page.get_by_test_id("composer-model-effort")).to_contain_text(
        "Codex Pretty 5.5 xhigh",
        timeout=15_000,
    )

    trigger = page.get_by_test_id("agent-picker-trigger")
    expect(trigger).to_be_visible()
    expect(trigger).to_contain_text("Codex")
    trigger.click()

    model_row = page.locator('[data-testid="model-picker-item"][data-model-id="gpt-5.5"]')
    expect(model_row).to_be_visible()
    expect(model_row).to_contain_text("Codex Pretty 5.5")

    effort_row = page.locator('[data-testid="effort-picker-item"][data-effort-level="xhigh"]')
    expect(effort_row).to_be_visible()
    expect(effort_row).to_contain_text("xhigh")
    assert effort_row.evaluate("el => getComputedStyle(el).textTransform") == "none"
