"""An agy turn stays in progress until agy's reply arrives.

Drives the real ``agy`` CLI through the server, runner and tmux against a mock
Gemini that holds its reply, so agy is still working after the prompt lands::

    OMNIGENT_E2E_ANTIGRAVITY=mock uv run --no-sync pytest --ui-skip-build -v \
        tests/e2e_ui/shells/test_antigravity_native_turn_completion.py
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from omnigent_client import OmnigentClient
from omnigent_client._sessions_chat import SessionsChat
from playwright.sync_api import Page, expect

from omnigent.harnesses.antigravity_native.bridge import _AGY_IDLE_MARKER, read_tmux_info
from tests.e2e_ui.shells.test_antigravity_native_cold_start_mirror import (
    _pane_text,
    _playwright_browsers_outside_isolated_home,  # noqa: F401 - autouse fixture reused by name
)
from tests.e2e_ui.shells.test_antigravity_tmux_recovery import (
    AntigravitySession,
    _wait_until,
    antigravity_model,  # noqa: F401 - fixture reused by name
    antigravity_session,  # noqa: F401 - fixture reused by name
)
from tests.e2e_ui.shells.test_terminal_direct_attach import _BLOCK_LOOPBACK_DIALS

pytestmark = [
    pytest.mark.posix_only,
    pytest.mark.skipif(
        os.environ.get("OMNIGENT_E2E_ANTIGRAVITY") not in {"1", "mock"},
        reason="set OMNIGENT_E2E_ANTIGRAVITY=mock (no credentials) or 1 (live model)",
    ),
    pytest.mark.timeout(600),
]

_HOLD_S = 15
_IDLE_PLACEHOLDER = "Send a message…"


class _SessionStream:
    """Timestamp a session's SSE events relative to a caller-chosen start."""

    def __init__(self, base_url: str, session_id: str) -> None:
        self.events: list[tuple[float, dict]] = []
        self.start = time.monotonic()
        self._url = f"{base_url}/v1/sessions/{session_id}/stream"
        self._connected = threading.Event()
        threading.Thread(target=self._read, daemon=True).start()
        assert self._connected.wait(15), "session stream never opened"

    def _read(self) -> None:
        with httpx.stream("GET", self._url, timeout=httpx.Timeout(10, read=None)) as response:
            self._connected.set()
            for line in response.iter_lines():
                if line.startswith("data: ") and line != "data: [DONE]":
                    self.events.append((time.monotonic(), json.loads(line[6:])))

    def timeline(self) -> list[str]:
        lines = []
        for at, event in list(self.events):
            kind = event.get("type", "")
            if kind == "session.status":
                detail = event.get("status")
            elif kind in {"response.completed", "response.failed"}:
                detail = f"output={json.dumps(event['response'].get('output'))[:160]}"
            elif kind == "response.output_item.done":
                detail = json.dumps(event.get("item", {}).get("content"))[:160]
            else:
                continue
            lines.append(f"+{at - self.start:6.2f}s {kind} {detail}")
        return lines


def _task(token: str) -> str:
    return f"Reply {token} after {_HOLD_S} seconds. No tools."


def _wait_for_agy_prompt(session: AntigravitySession) -> None:
    _wait_until(lambda: read_tmux_info(session.bridge_dir) is not None, "agy pane never launched")
    _wait_until(lambda: _AGY_IDLE_MARKER in _pane_text(session), "agy TUI never became ready", 120)


def test_composer_stays_in_follow_up_mode_while_agy_works(
    request: pytest.FixtureRequest,
    antigravity_session: AntigravitySession,  # noqa: F811
    antigravity_model: list[str] | None,  # noqa: F811
) -> None:
    session = antigravity_session
    _wait_until(lambda: read_tmux_info(session.bridge_dir) is not None, "agy pane never launched")
    page = request.getfixturevalue("page")
    try:
        _drive_composer_journey(page, session, antigravity_model)
    finally:
        page.context.close()


def _drive_composer_journey(
    page: Page, session: AntigravitySession, model_requests: list[str] | None
) -> None:
    page.add_init_script(_BLOCK_LOOPBACK_DIALS)
    page.goto(f"{session.base_url}/c/{session.session_id}?view=terminal")
    terminal = page.get_by_test_id("main-terminal-view").get_by_test_id("terminal-view")
    expect(terminal).to_have_attribute("data-state", "connected", timeout=120_000)
    _wait_for_agy_prompt(session)
    page.wait_for_timeout(1_500)

    token = f"agy-e2e-{uuid.uuid4().hex[:8]}"
    page.get_by_test_id("view-mode-chat").click()
    composer = page.get_by_label("Message the agent")
    composer.fill(_task(token))
    stream = _SessionStream(session.base_url, session.session_id)
    page.get_by_role("button", name="Send", exact=True).click()
    stream.start = sent = time.monotonic()

    samples: list[tuple[float, str | None]] = []
    while time.monotonic() - sent < 5:
        samples.append((round(time.monotonic() - sent, 2), composer.get_attribute("placeholder")))
        page.wait_for_timeout(250)

    page.get_by_test_id("view-mode-terminal").click()
    expect(terminal).to_have_attribute("data-state", "connected", timeout=30_000)
    pane_while_working = _pane_text(session)
    _wait_until(lambda: _pane_text(session).count(token) >= 2, "agy never answered in its TUI", 90)
    answered_at = round(time.monotonic() - sent, 2)
    page.wait_for_timeout(3_000)

    print("placeholder samples:", json.dumps(samples))
    print("pane while agy worked:", pane_while_working.strip().splitlines()[-8:])
    print(f"agy answered in its TUI by +{answered_at}s")
    print("pane after answer:", _pane_text(session).strip().splitlines()[-8:])
    print("session stream:", *stream.timeline(), sep="\n  ")
    if model_requests is not None:
        assert token in model_requests, "agy did not request this reply from the mock model"
    follow_up = [index for index, (_, text) in enumerate(samples) if text != _IDLE_PLACEHOLDER]
    assert follow_up, f"composer never entered follow-up mode: {samples}"
    reset = [at for at, text in samples[follow_up[0] :] if text == _IDLE_PLACEHOLDER]
    assert not reset, (
        f"composer reset to {_IDLE_PLACEHOLDER!r} at +{reset[0]}s while agy was still "
        f"working; agy answered in its TUI at +{answered_at}s"
    )


def test_sdk_send_returns_after_agy_replies(
    antigravity_session: AntigravitySession,  # noqa: F811
) -> None:
    session = antigravity_session
    _wait_for_agy_prompt(session)
    token = f"agy-e2e-{uuid.uuid4().hex[:8]}"

    async def send() -> list[tuple[float, str, str]]:
        async with OmnigentClient(base_url=session.base_url) as client:
            chat = SessionsChat(
                namespace=client.sessions,
                files_uploader=None,
                files_getter=None,
                session=await client.sessions.get(session.session_id),
            )
            started = time.monotonic()
            seen = []
            async for event in chat.send(_task(token)):
                detail = "" if event.type.endswith(".delta") else event.model_dump_json()[:300]
                seen.append((round(time.monotonic() - started, 2), event.type, detail))
            return seen

    sent = time.monotonic()
    # Playwright's loop owns this thread, so the SDK client runs on its own.
    with ThreadPoolExecutor(1) as pool:
        events = pool.submit(asyncio.run, send()).result()
    returned_at = round(time.monotonic() - sent, 2)
    pane_at_return = _pane_text(session)
    _wait_until(lambda: _pane_text(session).count(token) >= 2, "agy never answered in its TUI", 90)
    answered_at = round(time.monotonic() - sent, 2)
    print(f"chat.send returned at +{returned_at}s; agy answered in its TUI by +{answered_at}s")
    print("pane when chat.send returned:", pane_at_return.strip().splitlines()[-6:])
    print("chat.send events:", *events, sep="\n  ")
    assert any(token in detail for _, _, detail in events), (
        f"chat.send returned at +{returned_at}s on {events[-1][1]} without agy's reply; "
        f"agy answered in its TUI by +{answered_at}s: {events[-1][2]}"
    )
