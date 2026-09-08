"""E2E coverage for native Codex status recovery after a server restart."""

from __future__ import annotations

import json

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.codex_parity.helpers import (
    ev_assistant_message,
    ev_completed,
    ev_function_call,
    ev_response_created,
)
from tests.e2e_ui.conftest import MockedCodexNativeSession
from tests.e2e_ui.messages.test_message_render_parity import _select_view_mode, _send

_PROMPT = "Keep working while the Omnigent server restarts."
_RESPONSES = [
    [
        ev_response_created("resp-restart-tool"),
        ev_function_call(
            "call_restart_sleep",
            "exec_command",
            json.dumps({"cmd": "sleep 90"}),
        ),
        ev_completed("resp-restart-tool"),
    ],
    [
        ev_response_created("resp-restart-finished"),
        ev_assistant_message("msg-restart-finished", "Restart work finished."),
        ev_completed("resp-restart-finished"),
    ],
]
_TIMEOUT_MS = 180_000


@pytest.mark.parametrize(
    "mocked_native_codex_session",
    [_RESPONSES],
    indirect=True,
    ids=["server-restart"],
)
@pytest.mark.timeout(300)
def test_codex_working_indicator_survives_server_restart(
    page: Page,
    mocked_native_codex_session: MockedCodexNativeSession,
) -> None:
    """A fresh server snapshot keeps Working visible for an existing Codex turn."""
    session = mocked_native_codex_session
    page.goto(f"{session.base_url}/c/{session.session_id}")
    _select_view_mode(page, "Chat")
    _send(page, _PROMPT)

    working = page.locator('[data-testid="working-indicator"]')
    expect(working).to_be_visible(timeout=_TIMEOUT_MS)
    requests = session.sidecar.requests(min_count=1, timeout_ms=30_000)
    assert any(_PROMPT in str(request["body"]["input"]) for request in requests)
    sleep_call = page.locator('button[title="sleep 90"]').first
    expect(sleep_call.locator(".animate-spin")).to_be_visible(timeout=_TIMEOUT_MS)

    # Recycle the server process only. The runner, Codex app-server, and its
    # long-running shell call survive, matching a production App restart.
    session.restart_server()
    snapshot = httpx.get(
        f"{session.base_url}/v1/sessions/{session.session_id}",
        timeout=30.0,
    )
    snapshot.raise_for_status()
    assert snapshot.json()["status"] == "running"
    assert snapshot.json()["runner_online"] is True

    # Hydrate from the new process rather than relying on pre-restart UI state.
    page.reload()
    expect(working).to_be_visible(timeout=_TIMEOUT_MS)
