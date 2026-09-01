"""E2E: custom codex-native agents keep the Web Model / Effort controls.

Regression test: a top-level CUSTOM agent whose resolved harness is
``codex-native`` carries no ``omnigent.wrapper`` presentation label (it is
chat-first on purpose — see ``nativeCodingAgents.ts`` and the server's
wrapper-label-or-resolved-harness rule), but the composer's capability gates
(``supportsEffortControl`` / ``modelPickerKindForConv`` /
``effortLevelsForConv``) key on the wrapper label only. The session snapshot
correctly reports ``harness: "codex-native"`` and a persisted
``reasoning_effort``, yet the config gear — and with it the Model and Effort
controls — never renders.

The journey is the reporter's: define a custom agent with a native Codex
executor, start it as a top-level session, open it in Omnigent Web, and look
for the composer's configuration gear. The agent registration, session
create, harness resolution, and label shape are all real server behavior;
only the runner's async Codex ``model/list`` catalog report is stubbed into
the browser-visible snapshot (the same route-patch convention as
``test_codex_model_metadata.py``), so the effort-ladder assertions don't
depend on a live Codex CLI finishing its ~15s cold boot.
"""

from __future__ import annotations

import io
import json
import tarfile
from urllib.parse import urlparse

import httpx
import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import (
    _REPO_ROOT,
    _bind_session_runner,
    _ensure_runner_online,
    _server_state,
    fetch_with_retry,
)

# The reporter's spec, verbatim in shape: a strict ``spec_version: 1`` bundle
# with a native Codex executor and a custom prompt. No wrapper / terminal-first
# labels are stamped anywhere — that is the trigger.
_CUSTOM_CODEX_AGENT_YAML = """\
spec_version: 1
name: custom-codex-orchestrator

executor:
  type: omnigent
  model: gpt-5.6-sol
  config:
    harness: codex-native
    reasoning_effort: high

prompt: |
  You are a custom Codex orchestrator. Stay chat-first and delegate
  precisely. Do not refactor or wander.
"""

_MODEL_ID = "gpt-5.6-sol"
_MODEL_DISPLAY_NAME = "GPT-5.6-Sol"

# Raw Codex ``model/list`` rows as the runner reports them onto the session
# snapshot once the Codex app-server answers (same shape as
# ``test_codex_model_metadata.py``). Stubbed so the effort ladder is
# deterministic and independent of a live Codex CLI boot.
_CODEX_MODEL_OPTIONS = [
    {
        "id": _MODEL_ID,
        "model": _MODEL_ID,
        "displayName": _MODEL_DISPLAY_NAME,
        "defaultReasoningEffort": "high",
        "supportedReasoningEfforts": [
            {"reasoningEffort": "low", "description": "Low"},
            {"reasoningEffort": "medium", "description": "Medium"},
            {"reasoningEffort": "high", "description": "High"},
            {"reasoningEffort": "xhigh", "description": "Extra high"},
        ],
        "isDefault": True,
        "vendorMetadata": {"source": "codex"},
    }
]


def _create_custom_codex_session(base_url: str, runner_id: str) -> str:
    """Register the custom codex-native agent and bind its session.

    Mirrors ``_create_bundled_session`` but pins a per-session workspace in
    the create metadata: runner-owned Codex terminals hard-require one
    (``_codex_session_workspace`` raises when neither the session workspace
    nor ``OMNIGENT_RUNNER_WORKSPACE`` is set). Crucially, NO labels are
    passed — a custom agent bound to a native harness gets no
    ``omnigent.wrapper`` label, which is the exact shape under test.

    :param base_url: Spawned server base URL.
    :param runner_id: The token-bound runner id to bind.
    :returns: The new session/conversation id.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = _CUSTOM_CODEX_AGENT_YAML.encode()
        info = tarfile.TarInfo("config.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    create = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({"workspace": str(_REPO_ROOT)})},
        files={"bundle": ("agent.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    _bind_session_runner(base_url, session_id, runner_id)
    return session_id


def _patch_codex_catalog_report(page: Page, session_id: str) -> None:
    """Overlay the runner's Codex catalog report onto the session snapshot.

    Patches only the fields the codex-native runner fills in asynchronously
    after Codex answers ``model/list`` — ``model_options``, the reported
    ``llm_model``, and the effective ``reasoning_effort``. The bug's trigger
    fields (``labels`` without a wrapper entry, ``harness`` resolved to
    ``codex-native``) are passed through from the real server untouched.

    :param page: Playwright page, before navigation.
    :param session_id: Session id whose GET snapshot to overlay.
    """

    def _handle(route: Route) -> None:
        request = route.request
        if urlparse(request.url).path != f"/v1/sessions/{session_id}" or request.method != "GET":
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
        payload["model_options"] = _CODEX_MODEL_OPTIONS
        payload["llm_model"] = _MODEL_ID
        payload["reasoning_effort"] = "high"
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    page.route("**/v1/sessions/**", _handle)


def test_custom_codex_native_session_shows_model_and_effort_controls(
    page: Page,
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A label-less codex-native session exposes the Model / Effort controls.

    Journey (from the bug report): define a custom agent with a native Codex
    executor → start it as a top-level session → open it in Omnigent Web →
    the composer must show the configuration gear with the Codex model picker
    and the model-derived reasoning-effort selector, exactly as the built-in
    Codex wrapper session does.

    While the bug is live the capability gates see ``labels == {}`` and fail
    closed, the gear never mounts, and the first ``expect`` below times out.

    :param page: Playwright page fixture.
    :param live_server: Spawned server fixture; its runner is reused.
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    session_id = _create_custom_codex_session(live_server, runner_id)
    try:
        # Precondition, asserted against the REAL server (no route patch):
        # the snapshot reports the resolved native harness and carries no
        # wrapper presentation label. This is the exact
        # ``{ labels: {}, harness: "codex-native" }`` shape from the report;
        # if it ever stops holding, the test is failing on a changed trigger,
        # not on the capability gate under test.
        snapshot = httpx.get(f"{live_server}/v1/sessions/{session_id}", timeout=10.0).json()
        assert snapshot["harness"] == "codex-native", snapshot["harness"]
        assert "omnigent.wrapper" not in (snapshot.get("labels") or {})

        _patch_codex_catalog_report(page, session_id)
        page.goto(f"{live_server}/c/{session_id}")

        # The composer must offer the session configuration gear. This is
        # the user-visible failure point: with the label-only gates the gear
        # renders nothing for a custom codex-native session.
        gear = page.get_by_test_id("composer-config-gear")
        expect(gear).to_be_visible(timeout=15_000)
        gear.click()
        expect(page.get_by_test_id("composer-config-modal")).to_be_visible()

        # The Codex model picker is selected and lists Codex's raw catalog.
        model_trigger = page.get_by_test_id("composer-config-model")
        expect(model_trigger).to_be_visible()
        model_trigger.click()
        model_row = page.locator(f'[role="option"][data-model-id="{_MODEL_ID}"]')
        expect(model_row).to_be_visible()
        expect(model_row).to_contain_text(_MODEL_DISPLAY_NAME)
        # Re-select the current model to close the listbox without sending
        # Escape to the surrounding dialog.
        model_row.click()
        expect(model_row).to_be_hidden()

        # The effort control is enabled and derives its ladder from the
        # selected Codex model's supportedReasoningEfforts.
        effort_trigger = page.get_by_test_id("composer-config-effort")
        expect(effort_trigger).to_be_visible()
        effort_trigger.click()
        for effort in ("low", "medium", "high", "xhigh"):
            expect(page.locator(f'[role="option"][data-effort-level="{effort}"]')).to_be_visible()
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except Exception:
                respawned.kill()
