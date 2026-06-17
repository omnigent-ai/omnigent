"""E2E: codex-native model controls render Codex-returned metadata raw."""

from __future__ import annotations

import json
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect


def _patch_session_as_codex_native(page: Page, session_id: str) -> list[dict]:
    """Patch the browser's session snapshot into a codex-native response.

    The server fixture seeds a normal ``hello_world`` session so the page can
    boot against the real app/server. This route patch changes only
    ``GET`` and ``PATCH /v1/sessions/{session_id}`` responses as seen by the
    browser, simulating the AP snapshot after a codex-native runner has
    returned raw Codex ``model/list`` metadata.

    :param page: Playwright page before navigation.
    :param session_id: Session id to patch, e.g. ``"conv_abc123"``.
    :returns: Captured PATCH request bodies.
    """
    latest_payload: dict | None = None
    patch_bodies: list[dict] = []

    def _handle(route: Route) -> None:
        nonlocal latest_payload
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
            payload = dict(latest_payload or {})
            if "collaboration_mode" in request_body:
                labels = dict(payload.get("labels", {}))
                labels["omnigent.codex_native.collaboration_mode"] = request_body[
                    "collaboration_mode"
                ]
                payload["labels"] = labels
        else:
            route.continue_()
            return

        payload["labels"] = {
            **payload.get("labels", {}),
            "omnigent.wrapper": "codex-native-ui",
        }
        payload["harness"] = "codex"
        payload["llm_model"] = "gpt-5.5"
        payload["reasoning_effort"] = "xhigh"
        payload["model_options"] = [
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
        latest_payload = dict(payload)
        route.fulfill(
            status=200,
            headers=headers,
            body=json.dumps(payload),
        )

    page.route("**/v1/sessions/**", _handle)
    return patch_bodies


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


def test_codex_native_plan_mode_toggle_uses_codex_session_patch(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Toggle Codex Plan mode through the session PATCH route.

    The browser must expose the Plan button only for the codex-native wrapper,
    send the typed ``collaboration_mode`` field, and render the persistent status
    badge from Codex's raw ``omnigent.codex_native.collaboration_mode`` label
    returned by the session snapshot.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser snapshot is patched to codex-native.
    :returns: None.
    """
    base_url, session_id = seeded_session
    patch_bodies = _patch_session_as_codex_native(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    plan_toggle = page.get_by_test_id("codex-plan-mode-toggle")
    expect(plan_toggle).to_be_visible(timeout=15_000)
    expect(plan_toggle).to_have_attribute("aria-label", "Enter Plan mode")
    expect(plan_toggle).to_have_attribute("aria-pressed", "false")

    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and urlparse(response.url).path == f"/v1/sessions/{session_id}"
            and response.status == 200
        )
    ):
        plan_toggle.click()

    assert patch_bodies[-1] == {"collaboration_mode": "plan"}
    expect(plan_toggle).to_have_attribute("aria-label", "Exit Plan mode")
    expect(plan_toggle).to_have_attribute("aria-pressed", "true")
    expect(page.get_by_test_id("composer-plan-mode")).to_contain_text("Plan mode")

    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and urlparse(response.url).path == f"/v1/sessions/{session_id}"
            and response.status == 200
        )
    ):
        plan_toggle.click()

    assert patch_bodies[-1] == {"collaboration_mode": "default"}
    expect(plan_toggle).to_have_attribute("aria-label", "Enter Plan mode")
    expect(plan_toggle).to_have_attribute("aria-pressed", "false")
    expect(page.get_by_test_id("composer-plan-mode")).to_have_count(0)
