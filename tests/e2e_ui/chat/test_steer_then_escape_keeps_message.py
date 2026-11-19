"""E2E: a steered follow-up interrupted with Esc stays in the chat or returns to the composer."""

from __future__ import annotations

import contextlib
import uuid

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.polling import poll_until
from tests.e2e_ui.conftest import configure_mock_llm, reset_mock_llm, set_fallback_mock_llm
from tests.e2e_ui.messages.test_message_render_parity import _USER, _ensure_chat_view
from tests.e2e_ui.messages.test_native_codex_render_parity import _CODEX_MOCK_MODEL

_COMPOSER_LABEL = "Message the agent"
_PENDING_STEER_HINT = "The agent will read this after its current step"
_SETTLE_MS = 10_000


def _steer_kept(page: Page, steer: str) -> bool:
    in_chat = page.locator(_USER, has_text=steer).count() > 0
    return in_chat or steer in page.get_by_label(_COMPOSER_LABEL).input_value()


@pytest.mark.timeout(300)
def test_codex_steer_interrupted_with_escape_stays_in_chat_or_composer(
    request: pytest.FixtureRequest,
    native_codex_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    base_url, session_id = native_codex_mock_session
    token = uuid.uuid4().hex[:8]
    first = f"first-{token} please work on this"
    steer = f"steer-{uuid.uuid4().hex[:8]} change of plan"
    reset_mock_llm(mock_llm_server_url)
    set_fallback_mock_llm(mock_llm_server_url, _CODEX_MOCK_MODEL, f"reply-{token}")
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": f"held-{token}", "block": True}],
        key=f"held-{token}",
        match=token,
        required_tools=["exec_command"],
    )
    try:
        page = request.getfixturevalue("page")
        page.add_init_script("localStorage.setItem('omnigent:background-session-titles', 'off')")
        page.goto(f"{base_url}/c/{session_id}")
        _ensure_chat_view(page)
        composer = page.get_by_label(_COMPOSER_LABEL)
        expect(composer).to_be_editable(timeout=60_000)
        composer.fill(first)
        composer.press("Enter")
        interrupt = page.get_by_role("button", name="Interrupt", exact=True)
        expect(interrupt).to_be_visible(timeout=90_000)
        gate_url = f"{mock_llm_server_url}/gate/pending"
        poll_until(lambda: httpx.get(gate_url, timeout=5.0).json()["pending"], timeout_s=90.0)

        composer.fill(steer)
        composer.press("Enter")
        expect(page.locator(_USER, has_text=steer)).to_contain_text(
            _PENDING_STEER_HINT, timeout=30_000
        )
        composer.press("Escape")
        expect(interrupt).to_have_count(0, timeout=30_000)
        page.wait_for_timeout(_SETTLE_MS)
        kept = {"after Esc": _steer_kept(page, steer)}

        page.reload()
        expect(page.locator(_USER, has_text=first).first).to_be_visible(timeout=30_000)
        page.wait_for_timeout(_SETTLE_MS)
        kept["after reload"] = _steer_kept(page, steer)
    finally:
        with contextlib.suppress(httpx.HTTPError):
            httpx.post(f"{mock_llm_server_url}/gate/release", timeout=5.0)
    assert all(kept.values()), f"steered message neither in the chat nor in the composer: {kept}"
