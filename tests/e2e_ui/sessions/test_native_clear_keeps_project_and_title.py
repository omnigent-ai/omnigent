"""Browser e2e: a native ``/clear`` keeps the session's project and name.

On claude-native and codex-native sessions, ``/clear`` from the web composer
continues in a replacement session and the SPA follows it. That replacement
should stay filed under the same project, keep the session's name and answer
the next message.
"""

from __future__ import annotations

import contextlib
import re
import time
import uuid

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.polling import poll_until
from tests.e2e_ui.conftest import set_fallback_mock_llm
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _WORKING,
    _ensure_chat_view,
    _send,
)
from tests.e2e_ui.messages.test_native_claude_render_parity import (
    _CLAUDE_MOCK_MODEL,
    _open_terminal_view,
    _wait_terminal_connected,
)
from tests.e2e_ui.messages.test_native_codex_render_parity import _CODEX_MOCK_MODEL
from tests.e2e_ui.sessions.test_header_move_to_project import _await_filed_under
from tests.e2e_ui.sessions.test_sidebar_projects import _section

_TURN_TIMEOUT_MS = 90_000
_ROTATION_TIMEOUT_MS = 90_000
_SETTLE_TIMEOUT_S = 20.0


def _session(base_url: str, session_id: str) -> dict:
    response = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    response.raise_for_status()
    return response.json()


def _sidebar_group(page: Page, session_id: str, project_name: str) -> str:
    row_link = f'a[href="/c/{session_id}"]'
    if _section(page, project_name).locator(row_link).count():
        return project_name
    if _section(page, "Sessions").locator(row_link).count():
        return "Sessions"
    return "absent"


def _observe(page: Page, base_url: str, session_id: str, project_name: str) -> dict:
    snapshot = _session(base_url, session_id)
    tag = page.get_by_test_id("header-project-tag")
    title = page.get_by_test_id("header-title")
    return {
        "project_id": snapshot.get("project_id"),
        "title": snapshot.get("title"),
        "header_project_tag": tag.get_attribute("aria-label") if tag.count() else None,
        "header_title": title.inner_text() if title.count() else None,
        "sidebar_group": _sidebar_group(page, session_id, project_name),
    }


@pytest.mark.timeout(420)
@pytest.mark.parametrize(
    ("session_fixture", "mock_model"),
    [
        ("native_claude_mock_session", _CLAUDE_MOCK_MODEL),
        ("native_codex_mock_session", _CODEX_MOCK_MODEL),
    ],
    ids=["claude-native", "codex-native"],
)
def test_native_clear_keeps_project_and_title(
    request: pytest.FixtureRequest,
    mock_llm_server_url: str,
    session_fixture: str,
    mock_model: str,
) -> None:
    """``/clear`` continues in a usable session filed in the same project with the same name."""
    base_url, session_id = request.getfixturevalue(session_fixture)
    uniq = uuid.uuid4().hex[:6]
    project_name = f"E2E Clear Project {uniq}"
    title = f"e2e-clear-{uniq}"
    reply = f"pong-{uniq}"
    set_fallback_mock_llm(mock_llm_server_url, "default", reply)
    set_fallback_mock_llm(mock_llm_server_url, mock_model, reply)

    page = request.getfixturevalue("page")
    project_id: str | None = None
    new_session_id: str | None = None
    try:
        page.goto(f"{base_url}/c/{session_id}")
        _open_terminal_view(page)
        _wait_terminal_connected(page)
        _ensure_chat_view(page)

        _send(page, f"Reply with the single word {reply}")
        expect(page.locator(_ASSISTANT)).to_have_count(1, timeout=_TURN_TIMEOUT_MS)
        expect(page.locator(_WORKING)).to_have_count(0, timeout=_TURN_TIMEOUT_MS)

        tag = page.get_by_test_id("header-project-tag")
        expect(tag).to_be_visible(timeout=30_000)
        tag.click()
        page.get_by_role("textbox", name="Search or create project").fill(project_name)
        page.get_by_role("menuitem", name=re.compile(f"^Create {re.escape(project_name)}")).click()
        expect(tag).to_have_attribute("aria-label", f"Project: {project_name}")
        project_id = _await_filed_under(base_url, session_id)
        assert project_id is not None

        page.get_by_test_id("header-title").click()
        rename_input = page.get_by_role("textbox", name="Session name")
        rename_input.fill(title)
        rename_input.press("Enter")
        expect(page.get_by_test_id("header-title")).to_have_text(title, timeout=15_000)
        folder = page.get_by_role("button", name=project_name, exact=True)
        if folder.get_attribute("aria-expanded") != "true":
            folder.click()
        expect(_section(page, project_name).locator(f'a[href="/c/{session_id}"]')).to_be_visible(
            timeout=15_000
        )
        poll_until(lambda: _session(base_url, session_id).get("title") == title, timeout_s=15.0)

        composer = page.get_by_label("Message the agent")
        composer.click()
        composer.fill("/clear")
        composer.press("Enter")
        expect(page).to_have_url(
            re.compile(rf"/c/(?!{re.escape(session_id)})[0-9a-f]{{32}}"),
            timeout=_ROTATION_TIMEOUT_MS,
        )
        new_session_id = page.url.rsplit("/c/", 1)[1].split("?", 1)[0]
        expect(page.get_by_test_id("header-title")).to_be_visible(timeout=30_000)

        expected = {
            "project_id": project_id,
            "title": title,
            "header_project_tag": f"Project: {project_name}",
            "header_title": title,
            "sidebar_group": project_name,
        }
        deadline = time.monotonic() + _SETTLE_TIMEOUT_S
        observed = _observe(page, base_url, new_session_id, project_name)
        while observed != expected and time.monotonic() < deadline:
            page.wait_for_timeout(1_000)
            observed = _observe(page, base_url, new_session_id, project_name)

        original = _session(base_url, session_id)
        assert observed == expected, (
            f"after /clear, session {new_session_id} (continued from {session_id}) "
            f"observed {observed}, expected {expected}; original session now has "
            f"project_id={original.get('project_id')!r} title={original.get('title')!r}"
        )

        _ensure_chat_view(page)
        _send(page, f"Reply with the single word {reply}")
        expect(page.locator(_ASSISTANT).filter(has_text=reply)).to_have_count(
            1, timeout=_TURN_TIMEOUT_MS
        )
        expect(page).to_have_url(re.compile(rf"/c/{new_session_id}"))
    finally:
        if new_session_id is not None:
            with contextlib.suppress(httpx.HTTPError):
                httpx.delete(f"{base_url}/v1/sessions/{new_session_id}", timeout=10.0)
        if project_id is not None:
            with contextlib.suppress(httpx.HTTPError):
                httpx.delete(f"{base_url}/v1/projects/{project_id}", timeout=10.0)
