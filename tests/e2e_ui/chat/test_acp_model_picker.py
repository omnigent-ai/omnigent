"""E2E: curated model picker support for generic-ACP harnesses."""

from __future__ import annotations

import json
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry

_ACP_EXPECTED_ROWS = [
    ("deepseek-v4-pro", "deepseek-v4-pro"),
    ("gemini-3-8-flash", "gemini-3-8-flash"),
    ("GLM-5.3", "GLM-5.3"),
]

_ACP_MODEL_OPTIONS = [
    {
        "id": "deepseek-v4-pro",
        "model": "deepseek-v4-pro",
        "displayName": "deepseek-v4-pro",
        "isDefault": True,
    },
    {
        "id": "gemini-3-8-flash",
        "model": "gemini-3-8-flash",
        "displayName": "gemini-3-8-flash",
        "isDefault": False,
    },
    {
        "id": "GLM-5.3",
        "model": "GLM-5.3",
        "displayName": "GLM-5.3",
        "isDefault": False,
    },
]


def _patch_session_as_acp(
    page: Page,
    session_id: str,
    model_override: str | None = None,
    model_options: list[dict] | None = None,
    llm_model: str = "deepseek-v4-pro",
) -> list[dict]:
    """Patch the browser's session snapshot into a generic ACP response.

    The server fixture seeds a normal session so the page boots against
    the real app. This route intercept shapes GET and PATCH
    /v1/sessions/{session_id} to expose an ACP harness with curated model
    options.

    :param page: Playwright page before navigation.
    :param session_id: Session id to patch.
    :param model_override: Optional session model override.
    :param model_options: Curated catalog rows to expose.
    :param llm_model: Active bound model for the session.
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
            response = fetch_with_retry(route)
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

        payload["harness"] = "acp"
        payload["llm_model"] = llm_model
        payload["model_options"] = _ACP_MODEL_OPTIONS if model_options is None else model_options
        if model_override is not None:
            payload["model_override"] = model_override
        latest_payload = dict(payload)
        route.fulfill(
            status=200,
            headers=headers,
            body=json.dumps(payload),
        )

    page.route("**/v1/sessions/**", _handle)
    return patch_bodies


def test_acp_session_renders_curated_model_picker(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """An ACP session renders the curated model picker in the composer gear modal.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a server-backed session.
    """
    base_url, session_id = seeded_session
    _patch_session_as_acp(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    gear = page.get_by_test_id("composer-config-gear")
    expect(gear).to_be_visible(timeout=15_000)
    gear.click()

    model_btn = page.get_by_test_id("composer-config-model")
    expect(model_btn).to_be_visible()
    model_btn.click()

    rows = page.locator('[role="option"][data-model-id]')
    expect(rows).to_have_count(len(_ACP_EXPECTED_ROWS))
    for index, (model_id, label) in enumerate(_ACP_EXPECTED_ROWS):
        row = rows.nth(index)
        expect(row).to_have_attribute("data-model-id", model_id)
        expect(row).to_contain_text(label)


def test_acp_session_model_override_selection_persists(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Selecting a model in an ACP session sends a PATCH with model_override.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a server-backed session.
    """
    base_url, session_id = seeded_session
    patch_bodies = _patch_session_as_acp(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    gear = page.get_by_test_id("composer-config-gear")
    expect(gear).to_be_visible(timeout=15_000)
    gear.click()
    page.get_by_test_id("composer-config-model").click()

    page.locator('[role="option"][data-model-id="gemini-3-8-flash"]').click()
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and urlparse(response.url).path == f"/v1/sessions/{session_id}"
            and response.status == 200
        )
    ):
        page.get_by_test_id("composer-config-save").click()

    assert patch_bodies[-1] == {"model_override": "gemini-3-8-flash"}
