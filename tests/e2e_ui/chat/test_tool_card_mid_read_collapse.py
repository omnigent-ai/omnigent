"""Browser regression for a user-opened tool card surviving a live run fold."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm

_COMPOSER = "Send a message…"
_WORKING = '[data-testid="working-indicator"]'

_GATE_TIMEOUT_S = 90.0


def _wait_for_gate(mock_url: str, timeout_s: float = _GATE_TIMEOUT_S) -> None:
    """Wait until an LLM request is parked on the mock's gate."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if httpx.get(f"{mock_url}/gate/pending", timeout=5.0).json()["pending"]:
            return
        time.sleep(0.2)
    raise AssertionError(f"mock LLM gate never went pending within {timeout_s:.0f}s")


def _release_gate(mock_url: str) -> None:
    """Release the oldest gated LLM request."""
    resp = httpx.post(f"{mock_url}/gate/release", timeout=5.0)
    resp.raise_for_status()
    assert resp.json()["released"] is True


def _shell_step(step: int, *, block: bool = False) -> dict[str, Any]:
    """Script one numbered shell tool call, optionally gated."""
    cfg: dict[str, Any] = {
        "tool_calls": [
            {
                "call_id": f"call_midread_step{step}",
                "name": "sys_os_shell",
                "arguments": json.dumps({"command": f"echo midread-step-{step}"}),
            }
        ]
    }
    if block:
        cfg["block"] = True
    return cfg


def test_expanded_tool_card_survives_next_tool_call(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A tool card the user expanded stays visible when the next step lands."""
    base_url, session_id = seeded_session
    token = f"midread-{uuid.uuid4().hex[:8]}"

    # Gate the fourth tool and final reply to inspect the live fold mid-turn.
    configure_mock_llm(
        mock_llm_server_url,
        [
            _shell_step(1),
            _shell_step(2),
            _shell_step(3),
            _shell_step(4, block=True),
            {"text": "All four preparation steps are done.", "block": True},
        ],
        key="midread-steps",
        match=token,
    )

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(f"Run the four preparation steps one at a time. {token}")
    page.get_by_role("button", name="Send", exact=True).click()

    for step in (1, 2, 3):
        expect(page.get_by_text(f"midread-step-{step}").first).to_be_visible(timeout=90_000)

    _wait_for_gate(mock_llm_server_url)

    step1_card = page.locator('[data-slot="collapsible"]').filter(has_text="midread-step-1").first
    step1_card.locator('[data-slot="collapsible-trigger"]').first.click()
    expect(step1_card.get_by_text("Parameters", exact=True)).to_be_visible()

    # Let the card remain open before the next tool arrives.
    page.wait_for_timeout(1_500)

    _release_gate(mock_llm_server_url)
    expect(page.get_by_text("midread-step-4").first).to_be_visible(timeout=90_000)

    expect(page.get_by_text("midread-step-1").first).to_be_visible()
    expect(
        page.locator('[data-slot="collapsible"]')
        .filter(has_text="midread-step-1")
        .first.get_by_text("Parameters", exact=True)
    ).to_be_visible()

    # Let the turn settle before fixture teardown.
    _wait_for_gate(mock_llm_server_url)
    _release_gate(mock_llm_server_url)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=90_000)
