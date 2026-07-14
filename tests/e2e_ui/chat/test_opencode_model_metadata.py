"""E2E: opencode-native exposes its live model and session model catalog.

opencode owns its model (the user switches it inside the opencode TUI), but it
mirrors the live model into the session's ``model_override`` — set at launch and
updated by the forwarder on every in-TUI switch. The web UI surfaces *that* in
the model pill and lists the session-scoped catalog returned by the runner.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect

# Launch-resolved default the runner booted opencode with.
LAUNCH_MODEL = "openrouter/nemotron"
# The model the user switched to inside the opencode TUI; the forwarder mirrored
# it into ``model_override``. This — not the launch default — must show.
LIVE_TUI_MODEL = "openrouter/llama-3.3-70b-instruct"
ALTERNATE_MODEL = "opencode-go/glm-5.2"


def _patch_session_as_opencode_native(page: Page, session_id: str) -> list[dict]:
    """Patch the browser's session snapshot into an opencode-native response.

    The server fixture seeds a normal ``hello_world`` session so the page can
    boot against the real app/server. This route patch rewrites only the
    ``GET /v1/sessions/{session_id}`` response as seen by the browser, mirroring
    the AP snapshot after an opencode-native runner has mirrored its live TUI
    model into ``model_override`` and carrying the runner-discovered model
    catalog. PATCH responses mirror model selections back into the snapshot.

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
            "omnigent.wrapper": "opencode-native-ui",
        }
        payload["harness"] = "opencode"
        payload["llm_model"] = LAUNCH_MODEL
        payload.setdefault("model_override", LIVE_TUI_MODEL)
        payload["model_options"] = [
            {"id": LIVE_TUI_MODEL, "displayName": LIVE_TUI_MODEL},
            {"id": ALTERNATE_MODEL, "displayName": ALTERNATE_MODEL},
        ]
        latest_payload = dict(payload)
        route.fulfill(
            status=200,
            headers=headers,
            body=json.dumps(payload),
        )

    page.route("**/v1/sessions/**", _handle)
    return patch_bodies


def test_opencode_native_model_command_opens_picker_and_persists_pick(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Bare ``/model`` opens the catalog and a pick persists its override.

    Covers the user-facing path required by the PR: bare ``/model`` opens the
    server-backed catalog, and selecting a row PATCHes the same
    ``model_override`` used by the native bridge.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser snapshot is patched to opencode-native.
    :returns: None.
    """
    base_url, session_id = seeded_session
    patch_bodies = _patch_session_as_opencode_native(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    trigger = page.get_by_test_id("agent-picker-trigger")
    expect(trigger).to_be_visible(timeout=15_000)

    # opencode is identified as its own native wrapper in the status tray.
    expect(page.get_by_test_id("composer-harness")).to_contain_text("OpenCode")

    # Bare /model opens the existing picker with the session-scoped catalog.
    composer = page.get_by_placeholder("Ask the agent anything…")
    composer.fill("/model ")
    composer.press("Enter")

    current_row = page.locator(
        f'[data-testid="model-picker-item"][data-model-id="{LIVE_TUI_MODEL}"]'
    )
    alternate_row = page.locator(
        f'[data-testid="model-picker-item"][data-model-id="{ALTERNATE_MODEL}"]'
    )
    expect(current_row).to_be_visible()
    expect(alternate_row).to_be_visible()

    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and urlparse(response.url).path == f"/v1/sessions/{session_id}"
            and response.status == 200
        )
    ):
        alternate_row.click()

    assert patch_bodies[-1] == {"model_override": ALTERNATE_MODEL}
    expect(trigger).to_contain_text(ALTERNATE_MODEL)
