"""E2E: claude-native model picker follows its live Databricks catalog."""

from __future__ import annotations

import json
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect

_EXPECTED_ROWS = [
    ("opus", "Opus 4.10"),
    ("sonnet", "Sonnet 5"),
    ("haiku", "Haiku 4.5"),
]
_MODEL_OPTIONS = [
    {
        "id": "opus",
        "model": "system.ai.claude-opus-4-10",
        "displayName": "Opus 4.10",
        "isDefault": False,
    },
    {
        "id": "sonnet",
        "model": "system.ai.claude-sonnet-5",
        "displayName": "Sonnet 5",
        "isDefault": True,
    },
    {
        "id": "haiku",
        "model": "system.ai.claude-haiku-4-5",
        "displayName": "Haiku 4.5",
        "isDefault": False,
    },
]


def _patch_session_as_claude_native(
    page: Page,
    session_id: str,
    model_override: str | None = None,
    catalog_state: dict[str, bool] | None = None,
) -> list[dict]:
    """Patch the browser's session snapshot into a claude-native response.

    The server fixture seeds a normal ``hello_world`` session so the page can
    boot against the real app/server. This route patch changes only ``GET``
    and ``PATCH /v1/sessions/{session_id}`` responses as seen by the browser,
    simulating a Claude-native session whose launch-time Databricks query
    returned only Opus 4.10, Sonnet 5, and Haiku 4.5.

    :param page: Playwright page before navigation.
    :param session_id: Session id to patch, e.g. ``"conv_abc123"``.
    :param model_override: Optional session-scoped model override to expose.
    :param catalog_state: Optional mutable readiness gate for delayed options.
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
        payload["llm_model"] = "system.ai.claude-sonnet-5"
        payload["model_options"] = (
            _MODEL_OPTIONS if catalog_state is None or catalog_state["ready"] else []
        )
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


def test_claude_native_picker_lists_only_live_databricks_models(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The picker shows friendly labels for only the live gateway aliases.

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

    sonnet_row = page.locator('[data-testid="model-picker-item"][data-model-id="sonnet"]')
    expect(sonnet_row).to_have_attribute("data-active", "true")
    expect(page.locator('[data-testid="model-picker-item"][data-model-id="fable"]')).to_have_count(
        0
    )
    expect(
        page.locator('[data-testid="model-picker-item"][data-model-id="sonnet_5"]')
    ).to_have_count(0)


def test_claude_native_picker_updates_after_delayed_catalog(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A live catalog event fills the UI and applies a compatible sticky alias."""
    base_url, session_id = seeded_session
    catalog_state = {"ready": False}
    patch_bodies = _patch_session_as_claude_native(
        page,
        session_id,
        catalog_state=catalog_state,
    )
    stream_script = """
        (() => {
          const sessionId = __SESSION_ID__;
          const originalFetch = window.fetch.bind(window);
          window.fetch = (input, init) => {
            const url = typeof input === "string" ? input : input.url;
            const streamPath = `/v1/sessions/${sessionId}/stream`;
            if (new URL(url, window.location.origin).pathname === streamPath) {
              const body = new ReadableStream({
                start(controller) {
                  window.__claudeModelStreamController = controller;
                },
              });
              return Promise.resolve(new Response(body, {
                status: 200,
                headers: { "content-type": "text/event-stream" },
              }));
            }
            return originalFetch(input, init);
          };
        })()
        """.replace("__SESSION_ID__", json.dumps(session_id))
    page.add_init_script(
        stream_script,
    )
    page.add_init_script("window.localStorage.setItem('omnigent.picker.model', 'opus')")

    page.goto(f"{base_url}/c/{session_id}")

    trigger = page.get_by_test_id("agent-picker-trigger")
    expect(trigger).to_contain_text("system.ai.claude-sonnet-5", timeout=15_000)
    page.wait_for_function("window.__claudeModelStreamController !== undefined")

    catalog_state["ready"] = True
    page.evaluate(
        """
        ({ sessionId }) => {
          const frame = `event: session.model_options\ndata: ${JSON.stringify({
            conversation_id: sessionId,
          })}\n\n`;
          window.__claudeModelStreamController.enqueue(new TextEncoder().encode(frame));
        }
        """,
        {"sessionId": session_id},
    )

    expect(trigger).to_contain_text("Opus 4.10", timeout=10_000)
    assert {"model_override": "opus", "silent": True} in patch_bodies
    trigger.click()
    expect(page.locator('[data-testid="model-picker-item"]')).to_have_count(len(_EXPECTED_ROWS))


def test_claude_native_alias_selection_persists(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Picking Opus PATCHes its alias and the trigger shows the live label.

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
        page.locator('[data-testid="model-picker-item"][data-model-id="opus"]').click()

    assert patch_bodies[-1] == {"model_override": "opus"}
    expect(trigger).to_contain_text("Opus 4.10")


def test_claude_native_picker_prefers_session_override_over_sticky_model(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The active row follows the session override, not another session's pick."""
    page.add_init_script("window.localStorage.setItem('omnigent.picker.model', 'haiku')")
    base_url, session_id = seeded_session
    _patch_session_as_claude_native(page, session_id, model_override="opus")

    page.goto(f"{base_url}/c/{session_id}")

    trigger = page.get_by_test_id("agent-picker-trigger")
    expect(trigger).to_be_visible(timeout=15_000)
    trigger.click()

    expect(
        page.locator('[data-testid="model-picker-item"][data-model-id="opus"]')
    ).to_have_attribute("data-active", "true")
    expect(
        page.locator('[data-testid="model-picker-item"][data-model-id="haiku"]')
    ).not_to_have_attribute("data-active", "true")
