"""E2E: kiro-native model picker renders the kiro catalog and persists a pick."""

from __future__ import annotations

import json
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect


def _patch_session_as_kiro_native(page: Page, session_id: str) -> list[dict]:
    """Patch the browser's session snapshot into a kiro-native response.

    The server fixture seeds a normal session so the page boots against the real
    app/server. This route patch rewrites only ``GET``/``PATCH
    /v1/sessions/{session_id}`` as seen by the browser into a kiro-native
    snapshot carrying the curated kiro ``model_options`` (the shape
    :func:`omnigent.kiro_native.kiro_base_model_options` serves) and a persisted
    ``model_override``.

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
            if "model_override" in request_body:
                payload["model_override"] = request_body["model_override"]
        else:
            route.continue_()
            return

        payload["labels"] = {
            **payload.get("labels", {}),
            "omnigent.wrapper": "kiro-native-ui",
        }
        payload["harness"] = "kiro-native"
        payload["model_options"] = [
            {"id": "auto", "displayName": "Auto", "isDefault": True, "isCurrent": False},
            {
                "id": "claude-haiku-4.5",
                "displayName": "Claude Haiku 4.5",
                "isDefault": False,
                "isCurrent": False,
            },
            {"id": "glm-5", "displayName": "GLM-5", "isDefault": False, "isCurrent": False},
        ]
        payload.setdefault("model_override", "claude-haiku-4.5")
        latest_payload = dict(payload)
        route.fulfill(status=200, headers=headers, body=json.dumps(payload))

    page.route("**/v1/sessions/**", _handle)
    return patch_bodies


def test_kiro_native_picker_lists_models_and_persists_pick(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The kiro-native picker renders the curated catalog and persists a pick.

    kiro applies the chosen model as ``--model`` at launch, so the picker writes
    the selection to ``model_override`` (no in-session mirror). This covers the
    snapshot-driven picker render and the PATCH that carries the pick.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser snapshot is patched to kiro-native.
    :returns: None.
    """
    base_url, session_id = seeded_session
    patch_bodies = _patch_session_as_kiro_native(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    trigger = page.get_by_test_id("agent-picker-trigger")
    expect(trigger).to_be_visible(timeout=15_000)
    trigger.click()

    # The curated kiro catalog renders with its display names.
    haiku_row = page.locator('[data-testid="model-picker-item"][data-model-id="claude-haiku-4.5"]')
    expect(haiku_row).to_be_visible()
    expect(haiku_row).to_contain_text("Claude Haiku 4.5")
    expect(
        page.locator('[data-testid="model-picker-item"][data-model-id="glm-5"]')
    ).to_be_visible()

    # Picking a model PATCHes model_override (consumed at launch via --model).
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and urlparse(response.url).path == f"/v1/sessions/{session_id}"
            and response.status == 200
        )
    ):
        page.locator('[data-testid="model-picker-item"][data-model-id="glm-5"]').click()

    assert patch_bodies[-1] == {"model_override": "glm-5"}
