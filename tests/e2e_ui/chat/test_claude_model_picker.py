"""E2E: claude-native model picker offers Fable and both Sonnet generations."""

from __future__ import annotations

import json
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect

# Capability order, most powerful first; "sonnet_4_6" is Claude Code's one
# custom /model slot pinned to the older Sonnet generation.
_EXPECTED_ROWS = [
    ("fable", "Fable"),
    ("opus", "Opus"),
    ("sonnet", "Sonnet 5"),
    ("sonnet_4_6", "Sonnet 4.6"),
    ("haiku", "Haiku"),
]


def _patch_session_as_claude_native(page: Page, session_id: str) -> list[dict]:
    """Patch the browser's session snapshot into a claude-native response.

    The server fixture seeds a normal ``hello_world`` session so the page can
    boot against the real app/server. This route patch changes only ``GET``
    and ``PATCH /v1/sessions/{session_id}`` responses as seen by the browser,
    simulating the snapshot of a claude-native (Claude Code terminal) session
    bound to a concrete Sonnet 4.6 gateway model.

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
            "omnigent.wrapper": "claude-code-native-ui",
        }
        payload["harness"] = "claude"
        # A concrete older-generation id: must light up the "Sonnet 4.6" row,
        # not the generic "sonnet" row (both ids contain "sonnet").
        payload["llm_model"] = "databricks-claude-sonnet-4-6"
        latest_payload = dict(payload)
        route.fulfill(
            status=200,
            headers=headers,
            body=json.dumps(payload),
        )

    page.route("**/v1/sessions/**", _handle)
    return patch_bodies


def test_claude_native_picker_lists_fable_and_both_sonnets(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The picker offers Fable, Opus, Sonnet 5, Sonnet 4.6, and Haiku.

    Covers the user-facing change: Fable returns to the list, and the two
    Sonnet generations are separate, version-labelled rows. The bound
    Sonnet 4.6 model must highlight its own row rather than the generic
    Sonnet row — the substring-disambiguation this change adds.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser snapshot is patched to claude-native.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _patch_session_as_claude_native(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    trigger = page.get_by_test_id("agent-picker-trigger")
    expect(trigger).to_be_visible(timeout=15_000)
    trigger.click()

    rows = page.locator('[data-testid="model-picker-item"]')
    expect(rows).to_have_count(len(_EXPECTED_ROWS))
    for index, (model_id, label) in enumerate(_EXPECTED_ROWS):
        row = rows.nth(index)
        expect(row).to_have_attribute("data-model-id", model_id)
        expect(row).to_contain_text(label)

    # The bound databricks-claude-sonnet-4-6 model implicitly selects its own
    # row; the generic "sonnet" row (Sonnet 5) must not light up too.
    sonnet_46_row = page.locator('[data-testid="model-picker-item"][data-model-id="sonnet_4_6"]')
    expect(sonnet_46_row).to_have_attribute("data-active", "true")
    sonnet_5_row = page.locator('[data-testid="model-picker-item"][data-model-id="sonnet"]')
    expect(sonnet_5_row).not_to_have_attribute("data-active", "true")


def test_claude_native_sonnet_4_6_selection_persists(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Picking Sonnet 4.6 PATCHes its id and the trigger shows the label.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser snapshot is patched to claude-native.
    :returns: None.
    """
    base_url, session_id = seeded_session
    patch_bodies = _patch_session_as_claude_native(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    trigger = page.get_by_test_id("agent-picker-trigger")
    expect(trigger).to_be_visible(timeout=15_000)
    trigger.click()

    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and urlparse(response.url).path == f"/v1/sessions/{session_id}"
            and response.status == 200
        )
    ):
        page.locator('[data-testid="model-picker-item"][data-model-id="sonnet_4_6"]').click()

    assert patch_bodies[-1] == {"model_override": "sonnet_4_6"}
    expect(trigger).to_contain_text("Sonnet 4.6")
