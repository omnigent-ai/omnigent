"""Browser journey: a web turn into native agy must stay open until agy answers.

Run without credentials using the local mock Gemini server::

    OMNIGENT_E2E_ANTIGRAVITY=mock uv run --no-sync pytest \
        tests/e2e_ui/shells/test_antigravity_turn_completion.py -v
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent.harnesses.antigravity_native.bridge import read_tmux_info
from tests.e2e_ui.shells.test_antigravity_tmux_recovery import (
    AntigravitySession,
    _wait_until,
    antigravity_model,  # noqa: F401 - pytest fixture
    antigravity_session,  # noqa: F401 - pytest fixture
)
from tests.e2e_ui.shells.test_terminal_direct_attach import _BLOCK_LOOPBACK_DIALS

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("OMNIGENT_E2E_ANTIGRAVITY") not in {"1", "mock"},
        reason="set OMNIGENT_E2E_ANTIGRAVITY=mock (no credentials) or 1 (live model)",
    ),
    pytest.mark.timeout(600),
]

# The agy fixture re-homes HOME before the browser launches, so pin the cache.
_BROWSERS = Path.home() / ".cache" / "ms-playwright"
if _BROWSERS.is_dir():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(_BROWSERS))

_MODEL_DELAY_S = 15
_QUEUED_PLACEHOLDER = "Send a follow-up (queued) — Esc to stop"
_ASSISTANT_BUBBLE = '[data-testid="message-bubble"][data-role="assistant"]'


class _SessionStreamLog:
    """Tail the session SSE stream and timestamp every ``response.completed``."""

    def __init__(self, base_url: str, session_id: str) -> None:
        self._url = f"{base_url}/v1/sessions/{session_id}/stream"
        self.completed_at: list[float] = []
        self.status: int | None = None
        self.error: str | None = None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            with httpx.Client(timeout=httpx.Timeout(10.0, read=None)) as client:
                with client.stream("GET", self._url) as response:
                    self.status = response.status_code
                    for line in response.iter_lines():
                        if not line.startswith("data: ") or line[6:].strip() == "[DONE]":
                            continue
                        try:
                            data = json.loads(line[6:])
                        except ValueError:
                            continue
                        if data.get("type") == "response.completed":
                            self.completed_at.append(time.monotonic())
        except Exception as exc:
            self.error = repr(exc)

    def first_completed_after(self, since: float) -> float | None:
        return next((t for t in self.completed_at if t >= since), None)


def _pane_text(session: AntigravitySession) -> str | None:
    info = read_tmux_info(session.bridge_dir)
    if info is None:
        return None
    result = subprocess.run(
        [
            session.tmux,
            "-S",
            info["socket_path"],
            "capture-pane",
            "-p",
            "-J",
            "-t",
            info["tmux_target"],
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout if result.returncode == 0 else None


def _wait_for_agy_boot(session: AntigravitySession, timeout_s: float = 150.0) -> None:
    """Wait until agy's TUI shows its prompt and the pane stops redrawing."""
    deadline = time.monotonic() + timeout_s
    last_text: str | None = None
    stable_since = time.monotonic()
    while time.monotonic() < deadline:
        text = _pane_text(session)
        if text and "? for shortcuts" in text:
            if text != last_text:
                last_text = text
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= 3.0:
                return
        time.sleep(0.5)
    raise AssertionError(f"agy TUI did not settle; last pane text:\n{last_text}")


def test_turn_completion_waits_for_agy_reply(
    request: pytest.FixtureRequest,
    antigravity_session: AntigravitySession,  # noqa: F811 - imported fixture
    antigravity_model: list[str] | None,  # noqa: F811 - imported fixture
) -> None:
    session = antigravity_session
    page: Page = request.getfixturevalue("page")
    page.add_init_script(_BLOCK_LOOPBACK_DIALS)
    page.goto(f"{session.base_url}/c/{session.session_id}?view=terminal")
    terminal = page.get_by_test_id("main-terminal-view").get_by_test_id("terminal-view")
    expect(terminal).to_have_attribute("data-state", "connected", timeout=120_000)
    _wait_for_agy_boot(session)

    stream = _SessionStreamLog(session.base_url, session.session_id)
    _wait_until(
        lambda: stream.status is not None, f"session stream did not connect: {stream.error}", 15
    )
    token = f"agy-e2e-{uuid.uuid4().hex[:8]}"
    page.get_by_test_id("view-mode-chat").click()
    composer = page.get_by_label("Message the agent")
    composer.fill(f"Reply {token} after {_MODEL_DELAY_S} seconds. No tools.")
    page.get_by_role("button", name="Send", exact=True).click()
    sent_at = time.monotonic()

    # agy's reply reaches the chat through the read driver; the turn must not
    # be reported complete, nor the composer leave its queued state, before it.
    # Confirm the composer HAS entered the queued state first, so the loop below
    # cannot misread a not-yet-applied placeholder as that state already ending.
    expect(composer).to_have_attribute("placeholder", _QUEUED_PLACEHOLDER, timeout=10_000)
    reply = page.locator(_ASSISTANT_BUBBLE).filter(has_text=token)
    placeholder_dropped_s: float | None = None
    while reply.count() == 0:
        assert time.monotonic() - sent_at < 120, "agy's reply never reached the chat"
        if (
            placeholder_dropped_s is None
            and composer.get_attribute("placeholder") != _QUEUED_PLACEHOLDER
        ):
            placeholder_dropped_s = time.monotonic() - sent_at
        page.wait_for_timeout(100)
    reply_s = time.monotonic() - sent_at
    if antigravity_model is not None:
        assert token in antigravity_model, "agy did not request this reply from the mock model"

    _wait_until(
        lambda: stream.first_completed_after(sent_at) is not None,
        f"turn never reported complete after agy replied at +{reply_s:.1f}s",
        30,
    )
    completed = stream.first_completed_after(sent_at)
    assert completed is not None
    completed_s = completed - sent_at
    # Deterministic oracle (mock model only): agy cannot finish before its own
    # scripted delay, so an earlier completion was reported on delivery — the
    # regression. A live model has no guaranteed delay, so skip the timing floor.
    if antigravity_model is not None:
        floor_s = _MODEL_DELAY_S - 1.0
        assert completed_s >= floor_s, (
            f"turn reported complete at +{completed_s:.1f}s, before agy's "
            f"{_MODEL_DELAY_S}s model delay could have elapsed"
        )
        assert placeholder_dropped_s is None or placeholder_dropped_s >= floor_s, (
            f"composer left its queued state at +{placeholder_dropped_s:.1f}s, before "
            f"agy's {_MODEL_DELAY_S}s model delay could have elapsed"
        )
    expect(page.get_by_test_id("working-indicator")).to_have_count(0, timeout=60_000)
    page.close()
