"""Desktop recovery in the built SPA with controlled host and server contracts."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, Route, expect

from tests._helpers.desktop_host_recovery import (
    ARCA_DESKTOP_BRIDGE_INIT_SCRIPT,
    LOCAL_DESKTOP_BRIDGE_INIT_SCRIPT,
    THIS_MACHINE_HOST_ID,
)
from tests.browser_ui.chat.session_contract import (
    ChatSessionContract,
    install_chat_session_routes,
    list_payload,
)


@pytest.mark.parametrize(
    ("host_kind", "navigate_before_refusal"),
    [
        pytest.param("arca", False, id="stale-health-arca"),
        pytest.param("arca", True, id="late-refusal-arca"),
        pytest.param("laptop", True, id="late-refusal-laptop"),
    ],
)
def test_desktop_send_recovers_without_an_offline_health_edge(
    page: Page,
    chat_session_contract: ChatSessionContract,
    output_path: str,
    host_kind: str,
    navigate_before_refusal: bool,
) -> None:
    """Reconnect and retry the original payload while health stays online."""
    chat = chat_session_contract
    is_arca = host_kind == "arca"
    page.add_init_script(
        ARCA_DESKTOP_BRIDGE_INIT_SCRIPT if is_arca else LOCAL_DESKTOP_BRIDGE_INIT_SCRIPT
    )
    page.add_init_script("window.localStorage.removeItem('omnigent:arca-host-id');")
    page.set_viewport_size({"width": 1280, "height": 900})
    if not is_arca:
        chat.host_id = THIS_MACHINE_HOST_ID
    chat.update_session(title="Reconnect this session", host_id=chat.host_id, host_resumable=False)
    chat.reject_uploads = False
    other = ChatSessionContract(
        page=page,
        contract=chat.contract,
        stream=chat.stream,
        session_id="another-session",
        host_id=chat.host_id,
    )
    other.update_session(title="Another session")
    install_chat_session_routes(other)
    contract = chat.contract
    contract.json(
        "/v1/sessions", lambda _: list_payload([chat.session_snapshot(), other.session_snapshot()])
    )
    contract.json(
        "/health",
        {
            "sessions": {
                chat.session_id: {"runner_online": True, "host_online": True},
                other.session_id: {"runner_online": True, "host_online": True},
            }
        },
    )
    contract.json(
        "/v1/hosts",
        {
            "hosts": [
                {
                    "host_id": chat.host_id,
                    "name": "demo's arca" if is_arca else "This Mac",
                    "owner": "local",
                    "status": "online",
                    "configured_harnesses": {"openai-agents": True},
                }
            ]
        },
    )
    posts: list[dict] = []
    pending: list[Route] = []
    events_path = f"/v1/sessions/{chat.session_id}/events"

    def post_event(route: Route) -> None:
        if route.request.method != "POST":
            route.fallback()
            return
        posts.append(route.request.post_data_json)
        if len(posts) == 1:
            pending.append(route)
        else:
            route.fulfill(status=200, json={"queued": True, "item_id": "recovered-message"})

    contract.route(re.compile(rf"{re.escape(events_path)}$"), post_event)
    page.goto(chat.url)
    chat.wait_for_stream()
    expect(page.get_by_test_id("composer-host-select")).to_have_attribute(
        "aria-label", re.compile(r", online$")
    )
    composer = page.get_by_role("textbox", name="Message the agent", exact=True)
    composer.fill("Continue here with the attached notes.")
    page.locator('form.chat-composer-form input[type="file"]').set_input_files(
        {"name": "recovery-notes.txt", "mimeType": "text/plain", "buffer": b"Retain these notes."}
    )
    with page.expect_request(lambda request: request.url.endswith(events_path)):
        composer.press("Enter")
    expect(page.get_by_text("Continue here with the attached notes.", exact=True)).to_be_visible()
    assert len(pending) == 1
    if navigate_before_refusal:
        page.locator(f'a[href="/c/{other.session_id}"]').first.click()
        expect(page).to_have_url(other.url)
        composer.fill("Keep this other draft.")
    pending[0].fulfill(
        status=503,
        json={"error": {"code": "host_offline", "message": "Waiting for the host."}},
    )
    progress = "Reconnecting Arca…" if is_arca else "Reconnecting this machine…"
    expect(page.get_by_text(progress, exact=True)).to_be_visible()
    expect(page.get_by_test_id("reconnect-session-dialog")).not_to_be_visible()
    calls = "window.__connectArcaCalls" if is_arca else "window.__controlHostCalls.length"
    assert page.evaluate(calls) == 1
    assert len(posts) == 1

    finish = "window.__finishArcaReconnect" if is_arca else "window.__finishReconnect"
    with page.expect_request(lambda request: request.url.endswith(events_path), timeout=10_000):
        page.evaluate(f"{finish}({{ ok: true }})")
    expect(composer).to_have_value("Keep this other draft." if navigate_before_refusal else "")
    expect(page.get_by_text("Waiting for host", exact=True)).not_to_be_visible()
    assert len(posts) == 2
    assert posts[0]["data"] == posts[1]["data"]
    assert any(block["type"] == "input_file" for block in posts[1]["data"]["content"])
    assert not other.event_posts
    assert page.evaluate(calls) == 1
    evidence = Path(output_path)
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "requests.json").write_text(
        json.dumps(
            {"host": host_kind, "navigated": navigate_before_refusal, "posts": posts}, indent=2
        )
    )
    page.screenshot(path=str(evidence / "recovered.png"))
