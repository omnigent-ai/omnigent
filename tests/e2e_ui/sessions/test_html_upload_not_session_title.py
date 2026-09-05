r"""UI journey: uploading an HTML file must not make its source the session title.

On a native Codex ("codex-native") session, attaching an ``.html`` file to the
first prompt must not make the *HTML source code* the session title. The web
composer uploads the file as an ``input_file`` block (title seeding ignores
those), but if ``codex_native_executor._file_block_to_input_item`` inlines a
``text/*`` attachment's full content as a plain ``text`` input item (instead of
the ``[Attached file: ...]`` marker that title seeding strips), Codex echoes
the turn's ``userMessage`` back, the forwarder's ``_post_user_message`` joins
every text block — raw HTML included — into one ``input_text``, and
``_seed_missing_title_from_user_message`` synthesizes the session title from
it, so the sidebar shows ``<!DOCTYPE html> <html lang=...`` instead of a
meaningful name.

The journey is the real user path: open a codex-native session, wait for the
TUI to attach, attach an ``.html`` file plus a short typed prompt in the chat
composer, send, and let the native round-trip seed the title. The assertion
pins the *correct* behavior — the persisted session title must not contain the
attached file's HTML source — so this test fails while the bug is live and
passes once it is fixed.

LLM calls are served by the in-process mock LLM server (see
``native_codex_mock_session`` in ``conftest.py``); run with ``LLM_API_KEY``
unset so the mock provider config is written.
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
from tests.e2e_ui.messages.test_message_render_parity import (
    _ensure_chat_view,
    _send,
)
from tests.e2e_ui.messages.test_native_codex_render_parity import (
    _open_terminal_view,
    _wait_terminal_connected,
)

_log = logging.getLogger(__name__)

# Must match the model in the mock openai provider config written by the
# native_codex_mock_session fixture (conftest._CODEX_MOCK_MODEL).
_CODEX_MOCK_MODEL = "gpt-4o"

# The attached page. Distinctive first line: if any of it leaks into the
# session title, the title-seeding path inlined the file's source.
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
</html>
"""

# Fragments of the attachment's source that must never appear in the title.
# Lowercased comparison; covers the doctype, the tag soup, and the head text.
_LEAK_FRAGMENTS = ("<!doctype", "<html", "charset", "uploaded sample page")

# How long the seeded title may take to land after the send: covers the
# forward into the codex app-server thread plus the userMessage mirror back.
_TITLE_WAIT_S = 120.0


def _session_title(base_url: str, session_id: str) -> str | None:
    """Fetch the session's persisted title from the server.

    :param base_url: Spawned server base URL.
    :param session_id: The session/conversation id.
    :returns: The ``title`` field, or ``None`` when unset.
    """
    snap = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    snap.raise_for_status()
    title = snap.json().get("title")
    return title if isinstance(title, str) else None


def _wait_for_title(base_url: str, session_id: str) -> str:
    """Poll until the session title is seeded (non-empty) and return it.

    :param base_url: Spawned server base URL.
    :param session_id: The session/conversation id.
    :returns: The seeded title.
    """
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
        f"round-tripped (last value: {title!r}); transcript: {items.text[:4000]}"
    )


@pytest.mark.nightly
@pytest.mark.timeout(300)
def test_html_upload_does_not_become_session_title(
    page: Page,
    native_codex_mock_session: tuple[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """An HTML attachment's source must not be seeded as the session title."""
    base_url, session_id = native_codex_mock_session
    _log.info("native-codex mock session ready: base_url=%s session_id=%s", base_url, session_id)

    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _log.info("Codex TUI attached (terminal-view connected)")
    _ensure_chat_view(page)

    # One composer turn: content-based routing keys the mock's reply to the
    # typed marker so extra internal LLM calls can't consume the response.
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

    # Attach the HTML file exactly as a user would: the paperclip's hidden
    # file input (set_input_files fires the same change event the OS picker
    # does), then confirm the chip rendered before sending.
    sample = tmp_path / _HTML_NAME
    sample.write_text(_HTML_BODY)
    file_input = page.locator('input[type="file"][accept*="image/"]')
    file_input.set_input_files(str(sample))
    expect(page.get_by_role("button", name=f"Remove {_HTML_NAME}")).to_be_visible(timeout=10_000)

    # First (title-seeding) message of the session: typed text + attachment.
    _send(
        page,
        f"Context marker {user_marker}. "
        f"Reply with exactly this token and nothing else: {assistant_token}",
    )

    # Title seeding rides the native round-trip: codex echoes the turn's
    # userMessage back through the transcript forwarder, and the server seeds
    # the untitled conversation from it. Poll the server-side title (the
    # authoritative signal) rather than the assistant reply — the seed lands
    # before the assistant text streams.
    title = _wait_for_title(base_url, session_id)
    _log.info("seeded session title: %r", title)

    # Show the seeded title on the user-visible surface (the sidebar row) so a
    # recorded run films the outcome; the row's presence is not the assertion.
    page.reload()
    expect(page.locator(f'a[href="/c/{session_id}"]')).to_be_visible(timeout=30_000)
    page.wait_for_timeout(1_500)

    # The bug: the title is the attachment's source, e.g.
    # '<!DOCTYPE html> <html lang="en"> <head> <meta charset="utf…'.
    lowered = title.lower()
    leaked = [fragment for fragment in _LEAK_FRAGMENTS if fragment in lowered]
    assert not leaked, (
        f"session title leaked the attached HTML file's source (fragments {leaked!r}): {title!r}"
    )
