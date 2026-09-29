"""E2E: the web Plan toggle must put a codex-native turn in Codex Plan Mode. The turn
sent after engaging Plan must reach the model with Codex's Plan preset instructions;
while the bug is live the badge says Plan mode but the request carries none."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm, reset_mock_llm, set_fallback_mock_llm
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _WORKING,
    _ensure_chat_view,
    _send,
)
from tests.e2e_ui.messages.test_native_codex_render_parity import (
    _CODEX_MOCK_MODEL,
    _MOCK_TURN_TIMEOUT_MS,
    _open_terminal_view,
    _wait_terminal_connected,
)

# Opening rule of the developer instructions Codex's own Plan preset delivers.
_CODEX_PLAN_MODE_MARKER = "You are in **Plan Mode**"
_PLAN_TOGGLE_ATTEMPTS = 8
_PLAN_BADGE_TIMEOUT_MS = 15_000


def _developer_text(request: dict) -> str:
    """Return the system/developer-facing text of a captured model request."""
    parts: list[str] = []
    instructions = request.get("instructions")
    if isinstance(instructions, str):
        parts.append(instructions)
    for item in request.get("input") or []:
        if not isinstance(item, dict) or item.get("role") not in ("developer", "system"):
            continue
        content = item.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(
                block["text"]
                for block in content
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            )
    return "\n".join(parts)


def _coding_turn_developer_text(mock_url: str, marker: str) -> str:
    """Developer text of the codex agent turn whose prompt carries *marker*. Codex also
    fires title-generation calls that quote the user's message, so key on the
    ``<collaboration_mode>`` block only the agent's own turn carries to skip them."""
    requests = httpx.get(f"{mock_url}/mock/requests", timeout=10.0).json()["requests"]
    turns: list[str] = []
    for request in requests:
        if not isinstance(request, dict) or marker not in json.dumps(request):
            continue
        text = _developer_text(request)
        if "<collaboration_mode>" in text:
            turns.append(text)
    assert turns, f"no codex agent turn carried {marker!r} with a collaboration mode block"
    return turns[-1]


def _enter_plan_mode(page: Page) -> None:
    for _ in range(_PLAN_TOGGLE_ATTEMPTS):
        page.get_by_test_id("composer-attach").click()
        toggle = page.get_by_test_id("composer-plan-action")
        expect(toggle).to_be_visible(timeout=15_000)
        if toggle.get_attribute("data-active") == "true":
            page.keyboard.press("Escape")
            return
        toggle.click()
        try:
            expect(page.get_by_test_id("composer-plan-mode")).to_contain_text(
                "Plan mode", timeout=_PLAN_BADGE_TIMEOUT_MS
            )
            return
        except AssertionError:
            page.wait_for_timeout(5_000)
    pytest.fail("the composer never confirmed Plan mode")


@pytest.mark.timeout(420)
def test_web_plan_mode_toggle_delivers_codex_plan_mode_instructions(
    page: Page,
    native_codex_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A coding request sent after engaging Plan mode reaches the model in Plan Mode."""
    base_url, session_id = native_codex_mock_session
    nonce = uuid.uuid4().hex[:8]
    boot_marker, boot_reply = f"boot-{nonce}", f"booted-{nonce}"
    code_marker, code_reply = f"code-{nonce}", f"planned-{nonce}"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url, [{"text": boot_reply}], key=boot_marker, match=boot_marker
    )
    configure_mock_llm(
        mock_llm_server_url, [{"text": code_reply}], key=code_marker, match=code_marker
    )
    set_fallback_mock_llm(mock_llm_server_url, _CODEX_MOCK_MODEL, "")

    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)

    # Codex accepts a collaboration-mode update only once a thread is loaded and
    # its model is known; the first committed turn establishes both.
    _send(page, f"Say hello. Marker {boot_marker}.")
    expect(page.locator(_ASSISTANT, has_text=boot_reply).first).to_be_visible(
        timeout=_MOCK_TURN_TIMEOUT_MS
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_MOCK_TURN_TIMEOUT_MS)

    _enter_plan_mode(page)
    expect(page.get_by_test_id("composer-plan-mode")).to_contain_text("Plan mode")

    _send(page, f"Add a hello.txt file with a greeting. Marker {code_marker}.")
    expect(page.locator(_ASSISTANT, has_text=code_reply).first).to_be_visible(
        timeout=_MOCK_TURN_TIMEOUT_MS
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_MOCK_TURN_TIMEOUT_MS)

    developer_text = _coding_turn_developer_text(mock_llm_server_url, code_marker)
    assert _CODEX_PLAN_MODE_MARKER in developer_text, (
        "Plan mode was engaged in the composer, but the codex-native turn reached the "
        f"model with no Plan Mode instructions; developer text was:\n{developer_text[:1500]}"
    )
