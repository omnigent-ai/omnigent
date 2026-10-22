"""E2E: a Claude sub-agent's approval stays reachable from its Codex parent's Terminal view."""

from __future__ import annotations

import secrets
import threading

import httpx
import pytest
from playwright.sync_api import expect

from tests._helpers.native_session import create_native_session
from tests._helpers.polling import poll_until
from tests.e2e_ui.approvals.test_inbox_approval import _is_parked, _park_permission_hook

_PENDING_CARD = '[data-testid="approval-card"][data-state="pending"]'


@pytest.mark.timeout(180)
def test_subagent_approval_is_answerable_from_parent_terminal_view(
    request: pytest.FixtureRequest,
    live_server: str,
) -> None:
    parent_id = create_native_session(
        httpx,
        live_server,
        harness="codex",
        metadata={"title": "Codex commit review"},
    )["session_id"]
    child_id = create_native_session(
        httpx,
        live_server,
        harness="claude",
        metadata={"title": "Commit range review", "parent_session_id": parent_id},
    )["session_id"]

    elicitation_id = f"elicit_claude_{secrets.token_hex(16)}"
    sink: dict = {}
    worker = threading.Thread(
        target=_park_permission_hook,
        args=(
            live_server,
            child_id,
            elicitation_id,
            sink,
            "git log --oneline upstream-master..HEAD | head -50",
        ),
        daemon=True,
    )
    worker.start()
    try:
        poll_until(lambda: _is_parked(live_server, parent_id, elicitation_id), timeout_s=30.0)

        page = request.getfixturevalue("page")
        page.goto(f"{live_server}/c/{parent_id}")
        page.get_by_test_id("view-mode-chat").click()
        expect(page.locator(_PENDING_CARD).first).to_be_visible(timeout=30_000)

        page.get_by_test_id("view-mode-terminal").click()
        expect(page.get_by_test_id("main-terminal-view")).to_have_attribute(
            "data-visible", "true", timeout=15_000
        )
        card = page.get_by_test_id("terminal-subagent-approvals").locator(_PENDING_CARD).first
        expect(card).to_be_visible(timeout=10_000)
        expect(card).to_contain_text("Bash")
        card.get_by_role("button", name="Approve", exact=True).click()

        worker.join(timeout=30)
        assert not worker.is_alive(), "the Approve click did not resolve the permission hook"
        assert "error" not in sink, sink.get("error")
        response = sink["resp"]
        assert response.status_code == 200 and response.content, (
            response.status_code,
            response.text,
        )
        behavior = response.json()["hookSpecificOutput"]["decision"]["behavior"]
        assert behavior == "allow"
    finally:
        if worker.is_alive():
            httpx.post(
                f"{live_server}/v1/sessions/{child_id}/elicitations/{elicitation_id}/resolve",
                json={"action": "decline"},
                timeout=15.0,
            )
            worker.join(timeout=30)
