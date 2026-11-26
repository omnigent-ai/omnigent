"""Browser e2e: Codex Plan mode's "clear context and implement" moves the Terminal view along.

Choosing that answer on the web plan card continues in a replacement session.
Its Terminal view should show the fresh Codex thread that implements the plan,
not the TUI still attached to the planning thread.
"""

from __future__ import annotations

import contextlib
import re
import time
import uuid

import httpx
import pyte
import pytest
from playwright.sync_api import Page, expect
from websockets.exceptions import ConnectionClosed, InvalidHandshake
from websockets.sync.client import connect as ws_connect

from tests.e2e_ui.conftest import configure_mock_llm, reset_mock_llm, set_fallback_mock_llm
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

_TURN_TIMEOUT_MS = 90_000
_PLAN_CARD = '[data-testid="approval-card"][data-state="pending"]'
_CLEAR_CONTEXT = "Yes, clear context and implement"
_IMPLEMENTATION_PROMPT = "Implement the plan in a fresh context"
_SETTLE_S = 20.0


def _terminal_id(base_url: str, session_id: str) -> str:
    response = httpx.get(f"{base_url}/v1/sessions/{session_id}/resources", timeout=10.0)
    response.raise_for_status()
    return next(row["id"] for row in response.json()["data"] if row.get("type") == "terminal")


def _pane_text(base_url: str, session_id: str, terminal_id: str, seconds: float = 3.0) -> str:
    """Render what a read-only attach to the session's terminal currently shows."""
    url = base_url.replace("http://", "ws://", 1) + (
        f"/v1/sessions/{session_id}/resources/terminals/{terminal_id}/attach?read_only=true"
    )
    raw = bytearray()
    deadline = time.monotonic() + seconds
    try:
        websocket = ws_connect(url, open_timeout=15, max_size=None)
    except (InvalidHandshake, OSError) as exc:
        return f"<attach failed: {exc!r}>"
    with contextlib.suppress(ConnectionClosed, OSError), websocket:
        while time.monotonic() < deadline:
            try:
                frame = websocket.recv(timeout=max(0.1, deadline - time.monotonic()))
            except TimeoutError:
                break
            if isinstance(frame, bytes):
                raw.extend(frame)
    screen = pyte.Screen(200, 60)
    pyte.ByteStream(screen).feed(bytes(raw))
    return "\n".join(line.rstrip() for line in screen.display if line.strip())


def _settled_pane(base_url: str, session_id: str, terminal_id: str, wanted: str) -> str:
    deadline = time.monotonic() + _SETTLE_S
    pane = _pane_text(base_url, session_id, terminal_id)
    while wanted not in pane and time.monotonic() < deadline:
        time.sleep(2.0)
        pane = _pane_text(base_url, session_id, terminal_id)
    return pane


@pytest.mark.timeout(480)
def test_plan_clear_context_terminal_follows_new_session(
    request: pytest.FixtureRequest,
    native_codex_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    base_url, session_id = native_codex_mock_session
    warmup = f"warm-{uuid.uuid4().hex[:8]}"
    planning = f"plan-{uuid.uuid4().hex[:8]}"
    implemented = f"impl-{uuid.uuid4().hex[:8]}"
    plan = (
        "<proposed_plan>\n# Hello file\n\n- Create hello.txt containing a greeting.\n"
        "</proposed_plan>"
    )
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(mock_llm_server_url, [{"text": f"ok-{warmup}"}], key=warmup, match=warmup)
    configure_mock_llm(mock_llm_server_url, [{"text": plan}], key=planning, match=planning)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": f"Done {implemented}"}] * 3,
        key=implemented,
        match=_IMPLEMENTATION_PROMPT,
    )
    set_fallback_mock_llm(mock_llm_server_url, _CODEX_MOCK_MODEL, "")

    page: Page = request.getfixturevalue("page")
    new_session_id: str | None = None
    try:
        page.goto(f"{base_url}/c/{session_id}")
        _open_terminal_view(page)
        _wait_terminal_connected(page)
        _ensure_chat_view(page)
        # Codex rejects a Plan-mode switch until the session has a model from a first turn.
        _send(page, f"Say hello. Marker {warmup}.")
        expect(page.locator(_ASSISTANT, has_text=f"ok-{warmup}").first).to_be_visible(
            timeout=_TURN_TIMEOUT_MS
        )
        expect(page.locator(_WORKING)).to_have_count(0, timeout=_TURN_TIMEOUT_MS)

        page.get_by_test_id("composer-attach").click()
        plan_action = page.get_by_test_id("composer-plan-action")
        expect(plan_action).to_have_attribute("aria-label", "Enter Plan mode")
        plan_action.click()
        expect(page.get_by_test_id("composer-plan-mode")).to_have_count(1, timeout=30_000)
        _send(page, f"Add a hello.txt file with a greeting. Marker {planning}.")
        card = page.locator(_PLAN_CARD).filter(has_text="Implement this plan?")
        expect(card).to_be_visible(timeout=120_000)
        page.wait_for_timeout(1_500)

        card.get_by_role("radio", name=_CLEAR_CONTEXT).check()
        card.get_by_test_id("ask-user-question-submit").click()
        expect(page).to_have_url(
            re.compile(rf"/c/(?!{re.escape(session_id)})[0-9a-f]{{32}}"),
            timeout=_TURN_TIMEOUT_MS,
        )
        new_session_id = page.url.rsplit("/c/", 1)[1].split("?", 1)[0]
        _ensure_chat_view(page)
        expect(page.locator(_ASSISTANT, has_text=implemented).first).to_be_visible(
            timeout=_TURN_TIMEOUT_MS
        )
        expect(page.locator(_WORKING)).to_have_count(0, timeout=_TURN_TIMEOUT_MS)
        page.wait_for_timeout(2_000)

        _open_terminal_view(page)
        _wait_terminal_connected(page)
        terminal_id = _terminal_id(base_url, new_session_id)
        expect(page.locator('[data-testid="terminal-view"]').last).to_have_attribute(
            "data-terminal-id", terminal_id
        )
        pane = _settled_pane(base_url, new_session_id, terminal_id, implemented)

        page.reload()
        _open_terminal_view(page)
        _wait_terminal_connected(page)
        expect(page.locator('[data-testid="terminal-view"]').last).to_have_attribute(
            "data-terminal-id", terminal_id
        )
        reloaded_pane = _settled_pane(base_url, new_session_id, terminal_id, implemented)

        new_session = httpx.get(f"{base_url}/v1/sessions/{new_session_id}", timeout=10.0).json()
        context = (
            f"old session {session_id} -> new session {new_session_id} "
            f"(external_session_id={new_session.get('external_session_id')!r}, "
            f"terminal {terminal_id})"
        )
        failures = []
        for label, text in (("after clear", pane), ("after reload", reloaded_pane)):
            stale = [
                marker for marker in (planning, warmup, "Implement this plan?") if marker in text
            ]
            if implemented not in text or stale:
                failures.append(f"{label}: no {implemented!r}, stale {stale!r}\n{text}")
        assert not failures, (
            f"Terminal view shows the planning thread, not the fresh one; {context}\n"
            + "\n".join(failures)
        )
    finally:
        if new_session_id is not None:
            with contextlib.suppress(httpx.HTTPError):
                httpx.delete(f"{base_url}/v1/sessions/{new_session_id}", timeout=10.0)
