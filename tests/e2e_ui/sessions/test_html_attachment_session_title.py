"""UI journey: an HTML file attached to a Codex-native session's first message.

The session name and the first user bubble must come from the typed prompt,
not the file's source. The real ``codex`` CLI runs against the mock LLM
(``native_codex_mock_session``).
"""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm, reset_mock_llm, set_fallback_mock_llm
from tests.e2e_ui.messages.test_message_render_parity import _ASSISTANT, _USER, _ensure_chat_view
from tests.e2e_ui.messages.test_native_codex_render_parity import (
    _CODEX_MOCK_MODEL,
    _open_terminal_view,
    _wait_terminal_connected,
)
from tests.e2e_ui.sessions.test_background_session_titles_setting import _open_general_settings

_log = logging.getLogger(__name__)

_HTML_FILE = Path(__file__).parent / "fixtures" / "uploaded_page.html"
_HTML_FRAGMENTS = ("<!doctype", "<html", "<head", "charset")
# The mirrored user turn arrives within seconds; the budget covers a cold Codex turn.
_SEED_TIMEOUT_S = 60.0
_MOCK_TURN_TIMEOUT_MS = 60_000


def _turn(mock_url: str) -> tuple[str, str, str]:
    """Script one mock turn; return ``(prompt, user_marker, assistant_token)``."""
    nonce = uuid.uuid4().hex[:8]
    user_marker, assistant_token = f"usr-{nonce}", f"ast-{nonce}"
    reset_mock_llm(mock_url)
    configure_mock_llm(mock_url, [{"text": assistant_token}], key=user_marker, match=user_marker)
    set_fallback_mock_llm(mock_url, _CODEX_MOCK_MODEL, "")
    prompt = (
        f"Context marker {user_marker}. "
        f"Reply with exactly this token and nothing else: {assistant_token}"
    )
    return prompt, user_marker, assistant_token


def _disable_background_titles(page: Page, base_url: str) -> None:
    toggle = _open_general_settings(page, base_url)
    expect(toggle).to_be_visible(timeout=30_000)
    expect(toggle).to_have_attribute("aria-checked", "true")
    toggle.click()
    expect(toggle).to_have_attribute("aria-checked", "false")
    page.wait_for_timeout(1_500)


def _open_chat_view(page: Page, base_url: str, session_id: str) -> None:
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)


def _attach_html_and_send(page: Page, prompt: str) -> None:
    page.locator(".chat-composer-form input[type=file]").set_input_files(str(_HTML_FILE))
    expect(page.get_by_role("button", name=f"Remove {_HTML_FILE.name}")).to_be_visible(
        timeout=15_000
    )
    page.wait_for_timeout(1_000)
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(prompt)
    page.wait_for_timeout(1_000)
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(_USER).first).to_be_visible(timeout=_MOCK_TURN_TIMEOUT_MS)


def _server_title(base_url: str, session_id: str) -> str | None:
    response = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    response.raise_for_status()
    title = response.json().get("title")
    return title if isinstance(title, str) else None


def _wait_for_seeded_title(base_url: str, session_id: str) -> str:
    deadline = time.monotonic() + _SEED_TIMEOUT_S
    while True:
        title = _server_title(base_url, session_id)
        if title is not None:
            return title
        if time.monotonic() >= deadline:
            raise AssertionError("session was never titled after its first message")
        time.sleep(0.5)


def _visible_names(page: Page, session_id: str) -> tuple[str, str]:
    """Return ``(header name, sidebar row name)`` as rendered."""
    header = page.get_by_test_id("header-title")
    expect(header).to_be_visible(timeout=30_000)
    row = page.get_by_test_id("sidebar-conversation-list").locator(f'a[href*="{session_id}"]')
    expect(row.first).to_be_visible(timeout=30_000)
    return header.inner_text().strip(), row.first.inner_text().strip()


def _leaked_fragments(text: str) -> list[str]:
    lowered = text.lower()
    return [fragment for fragment in _HTML_FRAGMENTS if fragment in lowered]


def _assert_named_from_prompt(name: str, *, where: str) -> None:
    leaked = _leaked_fragments(name)
    assert not leaked, f"{where} shows HTML source {leaked}: {name!r}"
    assert name.startswith("Context marker usr-"), f"{where} is not the typed prompt: {name!r}"


@pytest.mark.nightly
@pytest.mark.timeout(300)
@pytest.mark.browser_context_args(record_video_size={"width": 1280, "height": 720})
def test_html_attachment_first_message_names_session_from_prompt(
    request: pytest.FixtureRequest,
    native_codex_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """With automatic naming off, the seeded name is the typed prompt, not the HTML."""
    base_url, session_id = native_codex_mock_session
    prompt, user_marker, assistant_token = _turn(mock_llm_server_url)

    page: Page = request.getfixturevalue("page")
    _disable_background_titles(page, base_url)
    _open_chat_view(page, base_url, session_id)
    _attach_html_and_send(page, prompt)

    seeded = _wait_for_seeded_title(base_url, session_id)
    bubble = page.locator(_USER).first.inner_text()
    header, row = _visible_names(page, session_id)
    page.reload()
    header_after_reload, row_after_reload = _visible_names(page, session_id)
    _log.info(
        "session=%s stored=%r header=%r sidebar=%r after reload header=%r sidebar=%r bubble=%r",
        session_id,
        seeded,
        header,
        row,
        header_after_reload,
        row_after_reload,
        bubble[:300],
    )
    page.wait_for_timeout(3_000)

    # The live header and sidebar row may lag until the next refresh.
    _assert_named_from_prompt(header_after_reload, where="header after reload")
    _assert_named_from_prompt(row_after_reload, where="sidebar row after reload")
    _assert_named_from_prompt(seeded, where="stored title")
    # The bubble shows the attachment chip plus the typed prompt, never the file's source.
    leaked = _leaked_fragments(bubble)
    assert not leaked, f"first user bubble shows HTML source {leaked}: {bubble[:300]!r}"
    assert user_marker in bubble, f"first user bubble lost the typed prompt: {bubble[:300]!r}"
    expect(page.locator(_ASSISTANT, has_text=assistant_token).first).to_be_visible(
        timeout=_MOCK_TURN_TIMEOUT_MS
    )
