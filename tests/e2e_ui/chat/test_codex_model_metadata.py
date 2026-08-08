"""E2E: codex-native model controls render Codex-returned metadata raw."""

from __future__ import annotations

import json
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect


def _patch_session_as_codex_native(
    page: Page,
    session_id: str,
    *,
    llm_model: str | None = "gpt-5.5",
    reasoning_effort: str = "xhigh",
    model_options: list[dict] | None = None,
    host_id: str | None = None,
    model_override: str | None = None,
) -> list[dict]:
    """Patch the browser's session snapshot into a codex-native response.

    The server fixture seeds a normal ``hello_world`` session so the page can
    boot against the real app/server. This route patch changes only
    ``GET`` and ``PATCH /v1/sessions/{session_id}`` responses as seen by the
    browser, simulating the AP snapshot after a codex-native runner has
    returned raw Codex ``model/list`` metadata.

    :param page: Playwright page before navigation.
    :param session_id: Session id to patch, e.g. ``"conv_abc123"``.
    :param llm_model: Bound model id the snapshot reports.
    :param reasoning_effort: Session effort the snapshot reports.
    :param model_options: Codex ``model/list`` rows; ``None`` uses the
        default gpt-5.5 row, ``[]`` simulates a fresh session whose runner
        has not pushed the catalog yet.
    :param host_id: Optional host binding to expose on the snapshot.
    :param model_override: Optional pinned model to expose on the snapshot.
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
        payload["llm_model"] = llm_model
        payload["reasoning_effort"] = reasoning_effort
        if host_id is not None:
            payload["host_id"] = host_id
        if model_override is not None:
            payload["model_override"] = model_override
        payload["model_options"] = (
            [
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
            if model_options is None
            else model_options
        )
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


def test_codex_effort_outside_model_ladder_reads_default(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """An effort the model's ladder doesn't offer renders as Default, not blank.

    A sticky cross-session pick (or a mirrored session effort) can hold a
    value outside the selected model's ladder — e.g. ``xhigh`` on a translated
    arm whose ladder is low/medium/high. Radix renders an empty trigger for a
    value no item declares, so the gear's Effort row read as blank. The value
    is clamped to the ladder for display only: the trigger reads Default, the
    ladder options still open, and saving the untouched row writes nothing.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser snapshot is patched to codex-native.
    :returns: None.
    """
    base_url, session_id = seeded_session
    patch_bodies = _patch_session_as_codex_native(
        page,
        session_id,
        llm_model="glm-5-2",
        reasoning_effort="xhigh",
        model_options=[
            {
                "id": "glm-5-2",
                "model": "system.ai.glm-5-2",
                "displayName": "glm-5-2",
                "defaultReasoningEffort": "medium",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "low"},
                    {"reasoningEffort": "medium"},
                    {"reasoningEffort": "high"},
                ],
                "isDefault": True,
            }
        ],
    )

    page.goto(f"{base_url}/c/{session_id}")

    label = page.get_by_test_id("composer-model-effort-label")
    expect(label).to_contain_text("glm-5-2", timeout=15_000)
    # The bottom bar must not name an effort the model can't run either.
    expect(label).not_to_contain_text("xhigh")
    page.get_by_test_id("composer-config-gear").click()
    expect(page.get_by_test_id("composer-config-modal")).to_be_visible()

    # The trigger reads Default instead of rendering blank.
    effort_trigger = page.get_by_test_id("composer-config-effort")
    expect(effort_trigger).to_contain_text("Default")

    # The clamp is display-only: saving the untouched row writes nothing.
    page.get_by_role("button", name="Save").click()
    expect(page.get_by_test_id("composer-config-modal")).to_be_hidden()
    assert not any("reasoning_effort" in body for body in patch_bodies)

    # The row still functions: the ladder opens (no xhigh — it's the model's
    # own ladder) and an explicit pick writes through.
    page.get_by_test_id("composer-config-gear").click()
    expect(page.get_by_test_id("composer-config-modal")).to_be_visible()
    effort_trigger.click()
    for level in ("low", "medium", "high"):
        expect(page.locator(f'[role="option"][data-effort-level="{level}"]')).to_be_visible()
    expect(page.locator('[role="option"][data-effort-level="xhigh"]')).to_have_count(0)
    page.locator('[role="option"][data-effort-level="medium"]').click()
    expect(effort_trigger).to_contain_text("medium")
    page.get_by_role("button", name="Save").click()
    expect(page.get_by_test_id("composer-config-modal")).to_be_hidden()
    expect(page.get_by_test_id("composer-model-effort-label")).to_contain_text(
        "medium", timeout=10_000
    )
    assert any(body.get("reasoning_effort") == "medium" for body in patch_bodies)


def test_codex_gear_seeds_effort_ladder_from_host_catalog(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The gear's model/effort options appear before the runner's catalog.

    A fresh codex session carries no ``model_options`` until the runner
    pushes codex's live catalog seconds after launch — which hid the Effort
    row entirely, then let the ladder resolve against the wrong (default)
    model. The page now seeds the pickers from the host's pre-launch
    ``model-options`` API and resolves the ladder against the session's
    pinned model, so a glm session shows glm's ladder immediately and an
    out-of-ladder sticky effort reads Default.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser snapshot is patched to codex-native.
    :returns: None.
    """
    base_url, session_id = seeded_session
    glm_row = {
        "id": "glm-5-2",
        "model": "system.ai.glm-5-2",
        "displayName": "glm-5-2",
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [
            {"reasoningEffort": "low"},
            {"reasoningEffort": "medium"},
            {"reasoningEffort": "high"},
        ],
        "isDefault": True,
    }
    seed_hits: list[str] = []

    def handle_host_options(route: Route) -> None:
        seed_hits.append(route.request.url)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"models": [glm_row], "routable_models": ["glm-5-2"]}),
        )

    page.route("**/v1/hosts/hostseed/model-options*", handle_host_options)
    _patch_session_as_codex_native(
        page,
        session_id,
        llm_model=None,
        reasoning_effort="xhigh",
        model_options=[],
        host_id="hostseed",
        model_override="glm-5-2",
    )

    page.goto(f"{base_url}/c/{session_id}")

    page.get_by_test_id("composer-config-gear").click()
    expect(page.get_by_test_id("composer-config-modal")).to_be_visible()
    # The Effort row exists immediately (it used to be hidden until the
    # runner catalog landed) and reads Default: xhigh is outside glm's
    # seeded ladder.
    effort_trigger = page.get_by_test_id("composer-config-effort")
    expect(effort_trigger).to_be_visible()
    expect(effort_trigger).to_contain_text("Default")
    effort_trigger.click()
    for level in ("low", "medium", "high"):
        expect(page.locator(f'[role="option"][data-effort-level="{level}"]')).to_be_visible()
    expect(page.locator('[role="option"][data-effort-level="xhigh"]')).to_have_count(0)
    assert seed_hits, "the host model-options API was never consulted"


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
