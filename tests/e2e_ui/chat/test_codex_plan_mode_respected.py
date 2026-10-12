"""E2E (live Codex + mock model): engaging Plan mode reaches the model.

In a bound codex-native session, engaging Plan mode from the composer shows the
"Plan mode" badge and the next turn's request to the model carries Codex's
built-in Plan Mode preset. Guards against the regression where the
collaboration-mode developer message carried only the wrapper's own note, so
the model received no planning instruction and coded immediately.

A real ``codex`` CLI runs in the session terminal against the in-process mock
Responses server (``native_codex_mock_session``); the mock captures the model
request so the preset is observable. The scripted reply cannot show the
model's plan-vs-code reaction, which is the downstream effect, not exercised
here.
"""

from __future__ import annotations

import json

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import reset_mock_llm, set_fallback_mock_llm
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

# Codex's built-in "plan" collaboration-mode preset (codex CLI >= 0.154.0): the
# model should receive this when Plan mode is engaged.
_PLAN_MODE_PRESET = "You are in **Plan Mode**"
# Opening of the codex-native wrapper's authored instructions (see
# omnigent/harnesses/codex_native/main.py).
_WRAPPER_INSTRUCTIONS_MARKER = "Codex is running in the session terminal"


def _developer_instruction_texts(requests: list[dict]) -> list[str]:
    """Collect every system/developer instruction string across captured requests.

    Covers both the top-level Responses ``instructions`` and the per-turn
    ``input`` developer/system messages, since Codex may carry collaboration
    instructions in either.
    """
    texts: list[str] = []
    for req in requests:
        if not isinstance(req, dict):
            continue
        instructions = req.get("instructions")
        if isinstance(instructions, str):
            texts.append(instructions)
        for item in req.get("input") or []:
            if not isinstance(item, dict) or item.get("role") not in ("developer", "system"):
                continue
            for block in item.get("content") or []:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    texts.append(block["text"])
    return texts


@pytest.mark.timeout(300)
def test_codex_plan_mode_reaches_the_model(
    page: Page,
    native_codex_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A coding turn under Plan mode carries Codex's Plan Mode preset."""
    base_url, session_id = native_codex_mock_session
    set_fallback_mock_llm(mock_llm_server_url, _CODEX_MOCK_MODEL, "Done.")

    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)

    # Bootstrap turn so the Codex thread's model is known before toggling Plan
    # (the plan-mode change otherwise 503s with "current model is unknown").
    _send(page, "Say hi.")
    expect(page.locator(_ASSISTANT).last).to_be_visible(timeout=60_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)

    page.get_by_test_id("composer-attach").click()
    plan_toggle = page.get_by_test_id("composer-plan-action")
    expect(plan_toggle).to_be_visible(timeout=30_000)
    expect(plan_toggle).to_have_attribute("aria-label", "Enter Plan mode")
    plan_toggle.click()
    expect(page.get_by_test_id("composer-plan-mode")).to_contain_text("Plan mode", timeout=30_000)

    reset_mock_llm(mock_llm_server_url)

    _send(page, "Add a hello.txt file with a greeting.")
    expect(page.locator(_ASSISTANT).last).to_be_visible(timeout=60_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)

    requests = httpx.get(f"{mock_llm_server_url}/mock/requests", timeout=10.0).json()["requests"]
    assert requests, "no model request was captured for the plan-mode coding turn"
    developer_texts = _developer_instruction_texts(requests)
    assert any(_PLAN_MODE_PRESET in text for text in developer_texts), (
        "plan-mode coding turn reached the model without Codex's Plan Mode preset; "
        f"developer instructions seen: {json.dumps(developer_texts)[:1500]}"
    )
    # The wrapper's authored instructions still arrive through the additive
    # top-level config key alongside the preset.
    assert any(_WRAPPER_INSTRUCTIONS_MARKER in text for text in developer_texts), (
        "the agent's authored instructions no longer reach the model; "
        f"developer instructions seen: {json.dumps(developer_texts)[:1500]}"
    )
