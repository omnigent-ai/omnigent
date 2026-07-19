"""E2E: Codex goal controls work with real Codex and mocked Responses."""

from __future__ import annotations

from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import MockedCodexNativeSession
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _WORKING,
    _ensure_chat_view,
    _send,
)
from tests.e2e_ui.messages.test_native_codex_render_parity import (
    _open_terminal_view,
    _wait_terminal_connected,
)

_NATIVE_CODEX_TIMEOUT_MS = 180_000


def _goal_response(session_id: str, method: str, suffix: str = ""):
    """Build a Playwright response predicate for Codex goal routes."""

    def _matches(response) -> bool:
        parsed = urlparse(response.url)
        return (
            response.request.method == method
            and parsed.path == f"/v1/sessions/{session_id}/codex_goal{suffix}"
            and response.status == 200
        )

    return _matches


# Boots a real native Codex CLI and drives it via a prebuilt codex-parity
# sidecar (CI supplies the binary through CODEX_PARITY_SIDECAR_BIN, so no
# multi-minute cargo build runs here). timeout(300) matches the sibling
# native-Codex render-parity tests now that the sidecar build is out of band.
@pytest.mark.timeout(300)
def test_codex_goal_mode_with_mocked_responses(
    page: Page,
    mocked_native_codex_goal_session: MockedCodexNativeSession,
) -> None:
    """Set, pause/resume, and clear a Codex goal through the real UI/API path."""
    session = mocked_native_codex_goal_session
    page.goto(f"{session.base_url}/c/{session.session_id}")

    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)

    _send(page, "Bootstrap the mocked goal-mode e2e thread.")
    expect(page.locator(_ASSISTANT, has_text="E2E_GOAL_BOOTSTRAP").first).to_be_visible(
        timeout=_NATIVE_CODEX_TIMEOUT_MS
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_NATIVE_CODEX_TIMEOUT_MS)

    requests = session.sidecar.requests(min_count=1, timeout_ms=30_000)
    assert requests[0]["path"] == "/v1/responses"
    assert requests[0]["body"]["model"] == "mock-model"
    assert "Bootstrap the mocked goal-mode e2e thread." in str(requests[0]["body"]["input"])

    goal_toggle = page.get_by_test_id("goal-toggle")
    expect(goal_toggle).to_be_visible(timeout=30_000)
    expect(goal_toggle).to_have_attribute("aria-label", "Set Codex goal")

    with page.expect_response(_goal_response(session.session_id, "GET")):
        goal_toggle.click()
    expect(page.get_by_test_id("goal-empty")).to_be_visible(timeout=30_000)

    objective = "Finish the mocked Codex goal-mode e2e"
    page.get_by_test_id("goal-objective").fill(objective)
    page.get_by_test_id("goal-token-budget").fill("12345")
    page.get_by_test_id("goal-mode-paused").click()
    expect(page.get_by_test_id("goal-mode-paused")).to_have_attribute(
        "aria-checked",
        "true",
    )

    with page.expect_response(_goal_response(session.session_id, "PUT")):
        page.get_by_test_id("goal-save").click()

    current_goal = page.get_by_test_id("goal-current")
    expect(current_goal).to_contain_text("paused", timeout=30_000)
    expect(current_goal).to_contain_text(objective)
    expect(current_goal).to_contain_text("0 / 12,345 tokens")
    expect(page.get_by_test_id("composer-goal-mode")).to_contain_text("Goal paused")
    expect(page.get_by_test_id("goal-resume")).to_be_visible()

    with page.expect_response(_goal_response(session.session_id, "PATCH", "/status")):
        page.get_by_test_id("goal-resume").click()
    expect(current_goal).to_contain_text("active", timeout=30_000)
    expect(page.get_by_test_id("composer-goal-mode")).to_contain_text("Goal active")
    expect(page.get_by_test_id("goal-pause")).to_be_visible()

    with page.expect_response(_goal_response(session.session_id, "DELETE")):
        page.get_by_test_id("goal-clear").click()
    expect(page.get_by_test_id("goal-empty")).to_be_visible(timeout=30_000)
    expect(page.get_by_test_id("composer-goal-mode")).to_have_count(0)
    expect(goal_toggle).to_have_attribute("aria-label", "Set Codex goal")
