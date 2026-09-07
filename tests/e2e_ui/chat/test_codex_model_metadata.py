"""E2E: codex-native model controls render Codex-returned metadata raw."""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry


def _patch_session_as_codex_native(
    page: Page,
    session_id: str,
    *,
    custom_agent: bool = False,
) -> list[dict]:
    """Patch the browser's session snapshot into a codex-native response.

    The server fixture seeds a normal ``hello_world`` session so the page can
    boot against the real app/server. This route patch changes only
    ``GET`` and ``PATCH /v1/sessions/{session_id}`` responses as seen by the
    browser, simulating the AP snapshot after a codex-native runner has
    returned raw Codex ``model/list`` metadata.

    :param page: Playwright page before navigation.
    :param session_id: Session id to patch, e.g. ``"conv_abc123"``.
    :param custom_agent: Remove the presentation wrapper label and report the
        resolved ``codex-native`` harness, matching a YAML custom agent.
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
            # A real server persists the collaboration_mode a prior PATCH set, so
            # a session rebind/refetch after toggling Plan mode still reports it.
            # The seeded hello_world session carries no such label, so carry the
            # last mode this mock recorded forward — otherwise a rebind (e.g. a
            # stream re-bind) re-derives Plan mode as off and the toggle snaps
            # back to "Enter Plan mode" mid-test.
            prior_labels = (latest_payload or {}).get("labels", {})
            prior_mode = prior_labels.get("omnigent.codex_native.collaboration_mode")
            if prior_mode is not None:
                payload["labels"] = {
                    **payload.get("labels", {}),
                    "omnigent.codex_native.collaboration_mode": prior_mode,
                }
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

        labels = dict(payload.get("labels", {}))
        if custom_agent:
            labels.pop("omnigent.wrapper", None)
        else:
            labels["omnigent.wrapper"] = "codex-native-ui"
        payload["labels"] = labels
        payload["harness"] = "codex-native" if custom_agent else "codex"
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

    # The read-only composer label shows the resolved model + effort; the
    # harness identity moved into the config gear's hover tooltip.
    label = page.get_by_test_id("composer-model-effort-label")
    expect(label).to_contain_text("Codex Pretty 5.5 xhigh", timeout=15_000)

    page.get_by_test_id("composer-config-gear").hover()
    expect(page.get_by_test_id("composer-config-gear-tooltip")).to_contain_text("Codex")

    # Open the config modal; its Model dropdown renders Codex's displayName raw.
    page.get_by_test_id("composer-config-gear").click()
    expect(page.get_by_test_id("composer-config-modal")).to_be_visible()
    page.get_by_test_id("composer-config-model").click()
    model_row = page.locator('[role="option"][data-model-id="gpt-5.5"]')
    expect(model_row).to_be_visible()
    expect(model_row).to_contain_text("Codex Pretty 5.5")
    # Re-select the current model to close the listbox without sending Escape
    # to the surrounding dialog.
    model_row.click()
    expect(model_row).to_be_hidden()
    effort_trigger = page.get_by_test_id("composer-config-effort")
    expect(effort_trigger).to_be_visible()
    effort_trigger.click()
    effort_row = page.locator('[role="option"][data-effort-level="xhigh"]')
    expect(effort_row).to_be_visible()
    expect(effort_row).to_contain_text("xhigh")
    # Codex effort ids render raw (not title-cased) even in the shared Select.
    assert effort_row.evaluate("el => getComputedStyle(el).textTransform") == "none"


def test_custom_codex_native_agent_keeps_model_and_effort_controls(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Expose Codex controls from the resolved harness without a wrapper label.

    YAML custom agents do not receive the built-in ``omnigent.wrapper``
    presentation label. The authoritative session snapshot still identifies
    their resolved harness as ``codex-native``, which is sufficient evidence
    that model and reasoning-effort overrides are supported.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser snapshot is patched to a custom codex-native agent.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _patch_session_as_codex_native(page, session_id, custom_agent=True)

    page.goto(f"{base_url}/c/{session_id}")

    label = page.get_by_test_id("composer-model-effort-label")
    expect(label).to_contain_text("Codex Pretty 5.5 xhigh", timeout=15_000)

    gear = page.get_by_test_id("composer-config-gear")
    expect(gear).to_be_visible()
    gear.click()
    expect(page.get_by_test_id("composer-config-model")).to_be_visible()
    expect(page.get_by_test_id("composer-config-effort")).to_contain_text("xhigh")


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


_PRE_CATALOG_HOST_ID = "host_pre_catalog_probe"
_HOST_PROBE_ROWS = [
    {
        "id": "gpt-5.6-luna",
        "model": "gpt-5.6-luna",
        "displayName": "GPT-5.6-Luna",
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [
            {"reasoningEffort": "low", "description": "Low"},
            {"reasoningEffort": "medium", "description": "Medium"},
            {"reasoningEffort": "xhigh", "description": "Extra high"},
        ],
        "isDefault": True,
    }
]


def _patch_precatalog_codex_session_on_host(page: Page, session_id: str) -> None:
    """Shape ``session_id`` as a host-bound codex session with no catalog yet.

    The snapshot's ``model_options`` stay empty for the whole test — the
    state a fresh session is in while codex app-server boots — while the
    host's pre-launch probe route serves cached rows. Liveness is pinned
    online so the gear stays enabled despite the fake host id.

    :param page: Playwright page before navigation.
    :param session_id: Session id to patch.
    """

    def _patch_snapshot(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != f"/v1/sessions/{session_id}":
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
        payload["labels"] = {
            **payload.get("labels", {}),
            "omnigent.wrapper": "codex-native-ui",
        }
        payload["harness"] = "codex"
        payload["llm_model"] = "gpt-5.6-luna"
        payload["model_options"] = []
        payload["host_id"] = _PRE_CATALOG_HOST_ID
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    def _serve_host_probe(route: Route) -> None:
        route.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps({"models": _HOST_PROBE_ROWS}),
        )

    def _force_online_health(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != "/health":
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
        online = {"runner_online": True, "host_online": True}
        if isinstance(payload.get("sessions"), dict):
            payload["sessions"][session_id] = online
        if isinstance(payload.get("session"), dict):
            payload["session"] = {**payload["session"], **online}
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    page.route("**/v1/sessions/**", _patch_snapshot)
    page.route(
        f"**/v1/hosts/{_PRE_CATALOG_HOST_ID}/harnesses/codex-native/model-options",
        _serve_host_probe,
    )
    page.route(re.compile(r"/health(\?|$)"), _force_online_health)


def test_codex_gear_offers_host_probe_rows_before_the_session_catalog(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Model and Effort never wait on the session's own catalog resolving.

    A fresh codex session's catalog only arrives once codex app-server
    answers ``model/list`` (seconds to ~15s cold) — until then the gear used
    to show a sparse Model row and no Effort row at all. With the session
    catalog empty, the gear rides the host's cached pre-launch probe rows:
    the Model menu lists them and the Effort menu offers their reasoning
    efforts immediately. The session's own catalog supersedes them when it
    lands (covered by the raw-metadata test above).

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real
        server-backed session; the browser view is patched to a host-bound
        codex-native shape whose catalog never resolves.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _patch_precatalog_codex_session_on_host(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    gear = page.get_by_test_id("composer-config-gear")
    expect(gear).to_be_visible(timeout=15_000)
    gear.click()
    expect(page.get_by_test_id("composer-config-modal")).to_be_visible()

    # The Effort row is present although the session catalog is still empty.
    effort_trigger = page.get_by_test_id("composer-config-effort")
    expect(effort_trigger).to_be_visible(timeout=10_000)

    # The Model menu lists the host probe row under its display name.
    page.get_by_test_id("composer-config-model").click()
    model_row = page.locator('[role="option"][data-model-id="gpt-5.6-luna"]')
    expect(model_row).to_be_visible()
    expect(model_row).to_contain_text("GPT-5.6-Luna")
    # Re-select the current model to close the listbox without sending
    # Escape to the surrounding dialog.
    model_row.click()
    expect(model_row).to_be_hidden()

    # The Effort menu offers exactly the host row's reasoning efforts.
    effort_trigger.click()
    for level in ("low", "medium", "xhigh"):
        expect(page.locator(f'[role="option"][data-effort-level="{level}"]')).to_be_visible()
