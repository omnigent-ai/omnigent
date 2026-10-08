"""A web shell command settles through the real Claude transcript forwarder."""

import shutil
import uuid

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent.onboarding.ambient import CLAUDE_CODE_MANAGED_SETTINGS_PATHS
from tests.e2e_ui.conftest import reset_mock_llm, set_fallback_mock_llm

from .test_message_render_parity import _ASSISTANT, _USER, _WORKING, _ensure_chat_view, _send
from .test_native_claude_render_parity import (
    _CLAUDE_MOCK_MODEL,
    _open_terminal_view,
    _wait_terminal_connected,
)


@pytest.fixture(scope="session", autouse=True)
def _requires_mockable_claude() -> None:
    for binary in ("claude", "tmux"):
        if shutil.which(binary) is None:
            pytest.skip(f"requires the real {binary} executable")
    if any(path.is_file() for path in CLAUDE_CODE_MANAGED_SETTINGS_PATHS):
        pytest.skip(
            "machine-managed Claude settings override mock auth; use an isolated container"
        )


@pytest.mark.nightly
@pytest.mark.timeout(240)
def test_web_shell_command_settles_before_the_next_prompt(
    page: Page,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    base_url, session_id = native_claude_mock_session
    marker = f"shell-settled-{uuid.uuid4().hex[:8]}"
    reply = f"prompt-settled-{uuid.uuid4().hex[:8]}"
    reset_mock_llm(mock_llm_server_url)
    for model in ("default", _CLAUDE_MOCK_MODEL):
        set_fallback_mock_llm(mock_llm_server_url, model, reply)

    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)
    _send(page, f"!echo {marker}")

    shell = page.locator(_USER, has_text=f"!echo {marker}")
    expect(shell).to_have_count(1, timeout=60_000)
    output = page.locator('[data-testid="terminal-command-card"][data-terminal-kind="output"]')
    expect(output).to_have_count(1, timeout=60_000)
    output.click()
    expect(page.get_by_text(marker, exact=True)).to_be_visible(timeout=30_000)
    expect(shell).to_have_count(1)
    expect(shell.get_by_test_id("copy-message-link")).to_be_enabled()
    with httpx.Client(base_url=base_url, timeout=15) as client:
        snapshot = client.get(f"/v1/sessions/{session_id}")
        snapshot.raise_for_status()
        assert snapshot.json()["pending_inputs"] == []

        _send(page, f"Reply with {reply}")
        expect(page.locator(_ASSISTANT, has_text=reply).first).to_be_visible(timeout=60_000)
        expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)
        expect(page.locator(_USER)).to_have_count(2)
        items = client.get(
            f"/v1/sessions/{session_id}/items", params={"limit": 100, "order": "asc"}
        )
        items.raise_for_status()
        errors = [item for item in items.json()["data"] if item["type"] == "error"]
        assert errors == []

    page.reload()
    _ensure_chat_view(page)
    expect(page.locator(_USER)).to_have_count(2, timeout=30_000)
    expect(shell).to_have_count(1)
    expect(page.locator(_ASSISTANT, has_text=reply).first).to_be_visible()
