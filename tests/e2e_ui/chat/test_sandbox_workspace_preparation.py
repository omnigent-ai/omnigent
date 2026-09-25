"""Prepared workspaces expose their launch stage through snapshots and live events."""

from __future__ import annotations

from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.chat.test_model_flows_contract import _install_stream_controller, _push_sse
from tests.e2e_ui.conftest import fetch_with_retry


def test_workspace_preparation_stage_updates_and_survives_reload(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Keep cloning copy, show live workspace preparation, and clear once ready."""
    base_url, session_id = seeded_session
    session_path = f"/v1/sessions/{session_id}"
    stage = "cloning"

    def snapshot(route: Route) -> None:
        if urlparse(route.request.url).path != session_path or route.request.method != "GET":
            route.fallback()
            return
        response = fetch_with_retry(route)
        body = response.json()
        body["sandbox_status"] = None if stage == "ready" else {"stage": stage, "error": None}
        route.fulfill(response=response, json=body)

    page.route(f"**{session_path}*", snapshot)
    _install_stream_controller(page, session_id)
    page.goto(f"{base_url}/c/{session_id}")
    indicator = page.get_by_test_id("runner-starting-indicator")
    expect(indicator).to_contain_text("Cloning repository…", timeout=15_000)

    stage = "preparing_workspace"
    _push_sse(
        page,
        "session.sandbox_status",
        {
            "type": "session.sandbox_status",
            "conversation_id": session_id,
            "stage": stage,
            "error": None,
        },
    )
    expect(indicator).to_contain_text("Preparing workspace…")
    expect(indicator).not_to_contain_text("Cloning repository")

    page.reload()
    expect(indicator).to_contain_text("Preparing workspace…", timeout=15_000)

    stage = "ready"
    _push_sse(
        page,
        "session.sandbox_status",
        {
            "type": "session.sandbox_status",
            "conversation_id": session_id,
            "stage": stage,
            "error": None,
        },
    )
    expect(indicator).to_have_count(0)
