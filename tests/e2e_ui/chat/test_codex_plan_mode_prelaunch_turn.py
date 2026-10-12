"""E2E (live Codex + mock model): a pre-launch Plan pick governs the first turn.

A session created with the ``collaboration_mode`` label the New Session screen
seeds opens with the Plan badge, and its first web turn already reaches the
model with Codex's Plan Mode preset; the mock Responses server captures the
request so the preset is observable.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent.stores.conversation_store import CODEX_NATIVE_COLLABORATION_MODE_LABEL_KEY
from tests._helpers.native_session import create_native_session
from tests.e2e_ui.chat.test_codex_plan_mode_respected import (
    _PLAN_MODE_PRESET,
    _developer_instruction_texts,
)
from tests.e2e_ui.conftest import (
    _REPO_ROOT,
    _bind_session_runner,
    _ensure_runner_online,
    _server_state,
    _temp_omnigent_mock_config,
    reset_mock_llm,
    set_fallback_mock_llm,
)
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _WORKING,
    _ensure_chat_view,
    _send,
)
from tests.e2e_ui.messages.test_native_codex_render_parity import (
    _CODEX_MOCK_MODEL,
    _open_terminal_view,
    _wait_terminal_connected,
)


@pytest.fixture
def native_codex_plan_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A runner-bound codex-native session created with Plan mode armed."""
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    with _temp_omnigent_mock_config(
        mock_llm_server_url, "codex", workflow_owned=bool(_server_state.get("workflow_owned"))
    ):
        created = create_native_session(
            httpx,
            live_server,
            harness="codex",
            metadata={
                "workspace": str(_REPO_ROOT),
                "labels": {CODEX_NATIVE_COLLABORATION_MODE_LABEL_KEY: "plan"},
            },
        )
        session_id = str(created["session_id"])
        _bind_session_runner(live_server, session_id, runner_id)
        try:
            yield (live_server, session_id)
        finally:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
            if respawned is not None:
                respawned.terminate()
                try:
                    respawned.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    respawned.kill()
                    respawned.wait(timeout=5)


@pytest.mark.timeout(300)
def test_prelaunch_plan_mode_governs_the_first_turn(
    page: Page,
    native_codex_plan_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """The first coding request of a Plan-armed session carries the Plan Mode preset."""
    base_url, session_id = native_codex_plan_session
    set_fallback_mock_llm(mock_llm_server_url, _CODEX_MOCK_MODEL, "Done.")

    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)
    # The seeded label shows Plan mode before any prompt is sent.
    expect(page.get_by_test_id("composer-plan-mode")).to_contain_text("Plan mode", timeout=30_000)

    # The mock server is shared across tests: drop earlier captures and key the
    # turn on a unique marker so only this session's request can satisfy it.
    reset_mock_llm(mock_llm_server_url)
    marker = f"prelaunch-plan-{uuid.uuid4().hex[:8]}"
    _send(page, f"Add a hello.txt file with a greeting. Marker {marker}.")
    expect(page.locator(_ASSISTANT).last).to_be_visible(timeout=60_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)

    requests = httpx.get(f"{mock_llm_server_url}/mock/requests", timeout=10.0).json()["requests"]
    coding_turns = [
        request
        for request in requests
        if isinstance(request, dict) and marker in json.dumps(request)
    ]
    assert coding_turns, "no model request carried the first coding prompt"
    developer_texts = _developer_instruction_texts(coding_turns[:1])
    assert any(_PLAN_MODE_PRESET in text for text in developer_texts), (
        "the first turn of a Plan-armed session reached the model without Codex's "
        f"Plan Mode preset; developer instructions seen: {json.dumps(developer_texts)[:1500]}"
    )
    # The session still reports Plan mode after the turn: the runner applied it,
    # so nothing reconciled the label back to Default.
    expect(page.get_by_test_id("composer-plan-mode")).to_contain_text("Plan mode")
