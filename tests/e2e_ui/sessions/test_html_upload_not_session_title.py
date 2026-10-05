"""UI journey: an uploaded HTML file's source must not become the session title.

Real web SPA and real Codex CLI against the mock LLM (``native_codex_mock_session``)."""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm, reset_mock_llm, set_fallback_mock_llm
from tests.e2e_ui.messages.test_message_render_parity import _USER, _ensure_chat_view, _send
from tests.e2e_ui.messages.test_native_codex_render_parity import (
    _CODEX_MOCK_MODEL,
    _open_terminal_view,
    _wait_terminal_connected,
)

_log = logging.getLogger(__name__)

_HTML_NAME = "uploaded_page.html"
_HTML_BODY = """<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>Uploaded sample page</title>
  </head>
  <body>
    <h1>Session title bug fixture</h1>
    <p>This file's source must never become the session name.</p>
  </body>
</html>"""

# Lowercased fragments of the attachment's source that must never reach the
# title or the rendered bubble.
_LEAK_FRAGMENTS = ("<!doctype", "<html", "charset", "uploaded sample page")

# Seeding rides the native round-trip (forward into the Codex app-server, echo
# back through the transcript forwarder), so allow for a cold Codex turn.
_TITLE_WAIT_S = 120.0


def _session_title(base_url: str, session_id: str) -> str | None:
    snap = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    snap.raise_for_status()
    title = snap.json().get("title")
    return title if isinstance(title, str) else None


def _wait_for_title(base_url: str, session_id: str) -> str:
    deadline = time.monotonic() + _TITLE_WAIT_S
    title: str | None = None
    while time.monotonic() < deadline:
        title = _session_title(base_url, session_id)
        if title:
            return title
        time.sleep(1.0)
    items = httpx.get(f"{base_url}/v1/sessions/{session_id}/items", timeout=10.0)
    raise AssertionError(
        "session title was never seeded after the first user message "
        f"(last value: {title!r}); transcript: {items.text[:4000]}"
    )


def _leaked_fragments(text: str) -> list[str]:
    lowered = text.lower()
    return [fragment for fragment in _LEAK_FRAGMENTS if fragment in lowered]


def _disable_automatic_session_names(page: Page, base_url: str) -> None:
    """Keep the seeded title visible: a generated one could overwrite it before it is read."""
    page.goto(f"{base_url}/settings/general")
    toggle = page.get_by_test_id("background-session-titles-toggle")
    expect(toggle).to_be_visible(timeout=30_000)
    expect(toggle).to_have_attribute("aria-checked", "true")
    toggle.click()
    expect(toggle).to_have_attribute("aria-checked", "false")


@pytest.mark.nightly
@pytest.mark.timeout(300)
def test_html_upload_does_not_become_session_title(
    page: Page,
    native_codex_mock_session: tuple[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """The first message's HTML attachment must not supply the session title."""
    base_url, session_id = native_codex_mock_session
    _log.info("native-codex mock session ready: base_url=%s session_id=%s", base_url, session_id)

    _disable_automatic_session_names(page, base_url)

    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _log.info("Codex TUI attached (terminal-view connected)")
    _ensure_chat_view(page)

    # Content-based routing keys the mock's reply to the typed marker so extra
    # internal LLM calls can't consume the response.
    nonce = uuid.uuid4().hex[:8]
    user_marker = f"usr-{nonce}"
    assistant_token = f"ast-{nonce}"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": assistant_token}],
        key=user_marker,
        match=user_marker,
    )
    set_fallback_mock_llm(mock_llm_server_url, _CODEX_MOCK_MODEL, "")

    # Attach through the paperclip's hidden file input (same change event the
    # OS picker fires) and confirm the chip rendered before sending.
    sample = tmp_path / _HTML_NAME
    sample.write_text(_HTML_BODY)
    page.locator('input[type="file"][accept*="image/"]').set_input_files(str(sample))
    expect(page.get_by_role("button", name=f"Remove {_HTML_NAME}")).to_be_visible(timeout=10_000)

    _send(
        page,
        f"Context marker {user_marker}. Reply with exactly this token and nothing else: "
        f"{assistant_token}",
    )

    # The server-side title is the authoritative signal; it lands before the
    # assistant reply streams.
    title = _wait_for_title(base_url, session_id)
    _log.info("seeded session title: %r", title)

    user_bubble = page.locator(_USER).first
    expect(user_bubble).to_be_visible(timeout=60_000)
    bubble_text = user_bubble.inner_text()
    _log.info("first user bubble text: %r", bubble_text[:600])

    # Show the title where the user reads it, and hold it so a recording can be read.
    page.reload()
    header_title = page.get_by_test_id("header-title")
    expect(header_title).to_be_visible(timeout=30_000)
    expect(page.locator(f'a[href="/c/{session_id}"]').first).to_be_visible(timeout=30_000)
    _log.info("header title text: %r", header_title.inner_text())
    page.wait_for_timeout(2_500)

    leaked = _leaked_fragments(title)
    assert not leaked, (
        f"session title leaked the attached HTML file's source (fragments {leaked!r}): {title!r}"
    )
    assert user_marker in title, f"session title was not seeded from the typed prompt: {title!r}"
    expect(header_title).to_contain_text(user_marker)
    bubble_leaked = _leaked_fragments(bubble_text)
    assert not bubble_leaked, (
        f"user bubble rendered the attached HTML file's source (fragments {bubble_leaked!r})"
    )
    assert user_marker in bubble_text, f"user bubble lost the typed prompt: {bubble_text[:300]!r}"
