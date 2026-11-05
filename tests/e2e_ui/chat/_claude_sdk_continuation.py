"""Shared lifecycle and page helpers for the claude-sdk ``/clear`` and ``/rewind`` journeys."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _ensure_runner_online, _server_state

ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
USER = '[data-testid="message-bubble"][data-role="user"]'
WORKING = '[data-testid="working-indicator"]'
COMPOSER = "Send a message…"


def send_and_wait(page: Page, text: str, reply: str) -> None:
    """Send *text* from the composer and wait for *reply* and an idle turn."""
    page.get_by_placeholder(COMPOSER).fill(text)
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(ASSISTANT).filter(has_text=reply)).to_be_visible(timeout=180_000)
    expect(page.locator(WORKING)).to_have_count(0, timeout=180_000)


def session_mentions(base_url: str, session_id: str, text: str) -> bool:
    """Whether one of *session_id*'s newest messages contains *text*."""
    items = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items", params={"order": "desc", "limit": 5}
    ).json()["data"]
    return any(text in part.get("text", "") for item in items for part in item.get("content", []))


@dataclass
class ClaudeSdkRun:
    """The shared runner a journey uses, and the sessions it made there."""

    runner_id: str
    created: list[str] = field(default_factory=list)


@pytest.fixture
def claude_sdk_run(
    live_server: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[ClaudeSdkRun]:
    """Bring the shared runner online; afterwards delete the journey's sessions.

    A runner this fixture had to respawn is stopped again.
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    run = ClaudeSdkRun(runner_id=str(_server_state["runner_id"]))
    try:
        yield run
    finally:
        try:
            for session_id in run.created:
                httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        finally:
            # A failed delete must not leave the respawned runner behind.
            if respawned is not None:
                respawned.terminate()
                try:
                    respawned.wait(timeout=5)
                except Exception:  # best-effort teardown
                    respawned.kill()
                    respawned.wait(timeout=5)
