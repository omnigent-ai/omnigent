"""E2E (hermetic): every native harness renders its session without crashing.

A crash-safety matrix over ALL native model-picker harnesses. Each case shapes
the browser's view of one seeded session into that harness's snapshot — using
the harness's realistic ``model_options`` shape, including rows with an
explicit ``model: null`` (what cursor/kiro/opencode actually send on the wire,
typed ``model?: string``) — then drives the real SPA and asserts:

* the composer renders (the page does not blank),
* no uncaught page error / null-deref console error fires, and
* opening the gear renders the model control over those rows.

Why this exists: a null-``model`` cursor row once reached a model-id fold that
called ``.trim()`` on null and blanked the whole chat page. The earlier
per-harness tests used rows with ``model`` OMITTED (``undefined``), which a
``!== undefined`` guard tolerated, so the ``null`` shape slipped through. This
matrix pins the crash-safe render for every harness, with the null shape.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.chat.test_model_flows_contract import _install_stream_controller


def _patch_session_as_harness(
    page: Page,
    session_id: str,
    *,
    wrapper: str,
    harness: str,
    llm_model: str,
    model_options: list[dict],
    model_override: str | None = None,
) -> None:
    """Shape the browser's view of *session_id* into a *harness* snapshot.

    Patches only ``GET`` / ``PATCH /v1/sessions/{session_id}`` as the browser
    sees it (the same route-patch idiom as ``test_claude_model_picker.py``),
    injecting the harness wrapper label plus its ``model_options`` verbatim.

    :param page: Playwright page before navigation.
    :param session_id: Seeded session id to reshape.
    :param wrapper: ``omnigent.wrapper`` label, e.g. ``"cursor-native-ui"``.
    :param harness: Harness family, e.g. ``"cursor"``.
    :param llm_model: Reported model id for the session.
    :param model_options: Native picker rows exposed on the snapshot.
    :param model_override: Optional session-scoped model override to expose.
    """
    latest_payload: dict | None = None

    def _handle(route: Route) -> None:
        nonlocal latest_payload
        request = route.request
        if urlparse(request.url).path != f"/v1/sessions/{session_id}":
            route.continue_()
            return
        headers = {"content-type": "application/json"}
        if request.method == "GET":
            response = route.fetch()
            payload = response.json()
            headers = {**response.headers, **headers}
        elif request.method == "PATCH":
            request_body = json.loads(request.post_data or "{}")
            payload = dict(latest_payload or {})
            if "model_override" in request_body:
                payload["model_override"] = request_body["model_override"]
        else:
            route.continue_()
            return
        payload["labels"] = {**payload.get("labels", {}), "omnigent.wrapper": wrapper}
        payload["harness"] = harness
        payload["llm_model"] = llm_model
        payload["model_options"] = model_options
        if model_override is not None:
            payload["model_override"] = model_override
        latest_payload = dict(payload)
        route.fulfill(status=200, headers=headers, body=json.dumps(payload))

    page.route("**/v1/sessions/**", _handle)


# One case per native model-picker harness. The vendor-owns-model harnesses
# (cursor / kiro / opencode) carry rows with an explicit ``model: null`` — the
# exact wire shape that once blanked the page — plus, for cursor, a hostile
# ``id: None`` row to lock the fold's null-guards on both fields.
_HARNESS_CASES = [
    pytest.param(
        "claude-code-native-ui",
        "claude",
        "claude-opus-4-8[1m]",
        None,
        [
            {
                "id": "opus",
                "model": "claude-opus-4-8[1m]",
                "displayName": "Opus 4.8 (1M context)",
                "isDefault": True,
            },
            {"id": "sonnet", "model": "claude-sonnet-5", "displayName": "Sonnet 5"},
        ],
        id="claude",
    ),
    pytest.param(
        "codex-native-ui",
        "codex",
        "gpt-5.6-terra",
        None,
        [
            {
                "id": "gpt-5.6-terra",
                "model": "gpt-5.6-terra",
                "displayName": "GPT-5.6-Terra",
                "isDefault": True,
                "defaultReasoningEffort": "medium",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "low", "description": "Low"},
                    {"reasoningEffort": "medium", "description": "Medium"},
                ],
            },
            {"id": "gpt-5.6-luna", "model": "gpt-5.6-luna", "displayName": "GPT-5.6-Luna"},
        ],
        id="codex",
    ),
    pytest.param(
        "cursor-native-ui",
        "cursor",
        "default",
        "databricks-claude-opus-4-8",
        [
            {"id": "auto", "model": None, "displayName": "Auto"},
            {"id": "gpt-5.3-codex", "model": None, "displayName": "Codex 5.3"},
            {"id": "claude-opus-4-8", "model": None, "displayName": "Claude Opus 4.8"},
            {"id": "composer-2.5", "model": None, "displayName": "Composer 2.5"},
            # Hostile: both id and model null must not throw in the fold.
            {"id": None, "model": None, "displayName": "Broken row"},
        ],
        id="cursor",
    ),
    pytest.param(
        "kiro-native-ui",
        "kiro",
        "auto",
        "databricks-claude-haiku-4-5",
        [
            {"id": "auto", "model": None, "displayName": "Auto", "isDefault": True},
            {"id": "claude-haiku-4.5", "model": None, "displayName": "Claude Haiku 4.5"},
        ],
        id="kiro",
    ),
    pytest.param(
        "opencode-native-ui",
        "opencode",
        "anthropic/claude-sonnet-4-6",
        "databricks-claude-opus-4-8",
        [
            {
                "id": "anthropic/claude-sonnet-4-6",
                "model": None,
                "displayName": "anthropic/claude-sonnet-4-6",
            },
            {"id": "openai/gpt-5", "model": None, "displayName": "openai/gpt-5"},
        ],
        id="opencode",
    ),
    pytest.param(
        "pi-native-ui",
        "pi",
        "omnigent-openai/system.ai.gpt-5-6-sol",
        None,
        [
            {
                "id": "omnigent-openai/system.ai.gpt-5-6-sol",
                "model": "omnigent-openai/system.ai.gpt-5-6-sol",
                "displayName": "system.ai.gpt-5-6-sol",
            }
        ],
        id="pi",
    ),
]

_CRASH_MARKERS = ("Cannot read properties of null", "reading 'trim'", "is not a function")


@pytest.mark.parametrize(
    ("wrapper", "harness", "llm_model", "model_override", "model_options"), _HARNESS_CASES
)
def test_harness_session_renders_without_crashing(
    page: Page,
    seeded_session: tuple[str, str],
    wrapper: str,
    harness: str,
    llm_model: str,
    model_override: str | None,
    model_options: list[dict],
) -> None:
    """Every native harness renders its session + gear without a page crash.

    Loads a session shaped as *harness* (with its realistic rows, including
    explicit ``model: null``), then asserts the composer renders, no uncaught
    error fires, and the gear's model control renders over those rows.
    """
    base_url, session_id = seeded_session
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on(
        "console",
        lambda msg: errors.append(msg.text) if msg.type == "error" else None,
    )

    _install_stream_controller(page, session_id)
    _patch_session_as_harness(
        page,
        session_id,
        wrapper=wrapper,
        harness=harness,
        llm_model=llm_model,
        model_options=model_options,
        model_override=model_override,
    )

    page.goto(f"{base_url}/c/{session_id}")

    # The composer renders → the page did not blank on a render throw.
    expect(page.get_by_test_id("composer-config-gear")).to_be_visible(timeout=15_000)
    # The status label runs the model-id fold over the rows.
    expect(page.get_by_test_id("composer-model-effort-label")).to_have_count(1)

    # Opening the gear renders the model control, folding every row (the exact
    # path the null-``model`` cursor crash took).
    page.get_by_test_id("composer-config-gear").click()
    expect(page.get_by_test_id("composer-config-modal")).to_be_visible(timeout=10_000)
    expect(page.get_by_test_id("composer-config-model")).to_be_visible()
    page.get_by_test_id("composer-config-model").click()
    # The option list renders without throwing (rows folded into the picker).
    expect(page.locator('[role="option"]').first).to_be_visible(timeout=10_000)

    crash = [e for e in errors if any(m in e for m in _CRASH_MARKERS)]
    assert not crash, f"{harness}: render crashed with {crash!r} (all errors: {errors!r})"
