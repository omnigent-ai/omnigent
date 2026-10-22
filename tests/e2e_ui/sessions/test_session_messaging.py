"""Owner communication switch and Canvas activity through the built SPA."""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterator
from uuid import uuid4

import pytest
from playwright.sync_api import Page, expect

from tests._helpers.live_server import isolated_local_server
from tests._helpers.session import bind_session_runner, post_session_bundle
from tests.e2e.conftest import isolated_mock_llm_server_url as isolated_mock_llm_server_url
from tests.e2e.test_session_messaging_e2e import _items, _owner_client, _sdk_bundle, _wait_for_text
from tests.e2e.test_session_messaging_e2e import messaging_processes as messaging_processes
from tests.e2e.test_session_messaging_e2e import messaging_runtime as messaging_runtime
from tests.e2e_ui.conftest import _build_hello_world_bundle, configure_mock_llm


def _login_page(page: Page, base: str, user: str = "alice") -> None:
    response = page.request.post(
        f"{base}/auth/login", data={"username": user, "password": "test-password-123"}
    )
    assert response.ok, response.text()


@pytest.fixture(scope="module")
def link_ui_server(built_spa, tmp_path_factory) -> Iterator[str]:
    with isolated_local_server(
        tmp_path_factory.mktemp("owner-communication-ui"),
        env={
            "OMNIGENT_FEATURES": "session_messaging,canvas",
            "OMNIGENT_AUTH_PROVIDER": "accounts",
            "OMNIGENT_ACCOUNTS_COOKIE_SECRET": secrets.token_hex(32),
            "OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME": "alice",
            "OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD": "test-password-123",
            "OMNIGENT_ACCOUNTS_AUTO_OPEN": "0",
        },
    ) as base:
        yield base


@pytest.mark.parametrize(
    "surface", ["conversation", "desktop-layout", "canvas", "mobile", "mobile-canvas"]
)
def test_owner_switch_persists_across_surfaces(
    page: Page, link_ui_server: str, tmp_path, surface: str
) -> None:
    """Chromium layout coverage; actual Electron and native apps need a separate drive."""
    base = link_ui_server
    is_canvas = surface.endswith("canvas")
    page.set_viewport_size(
        {"width": 390, "height": 844}
        if surface.startswith("mobile")
        else {"width": 1440, "height": 960}
    )
    if surface == "desktop-layout":
        page.add_init_script("""window.omnigentDesktop = {
            kind: 'electron', setBadgeCount() {},
            notify: () => Promise.resolve(false), onNotificationActivated: () => () => {},
            getServerPicker: () => Promise.resolve(null),
            switchServer: () => Promise.resolve(), openServerSetup() {},
        };""")
    _login_page(page, base)
    with _owner_client(base) as client:
        response = post_session_bundle(
            client.post, "/v1/sessions", _build_hello_world_bundle(), metadata={"title": "Manager"}
        )
        response.raise_for_status()
        manager = response.json()["session_id"]
        try:
            page.goto(base + (f"/canvas/c/{manager}" if is_canvas else f"/c/{manager}"))
            page.get_by_role("button", name="Session communication", exact=True).click()
            dialog = page.get_by_role("dialog", name="Session communication", exact=True)
            expect(dialog.get_by_role("status")).to_have_text("Communication is off.")
            expect(dialog.get_by_text("including future sessions", exact=False)).to_be_visible()
            page.screenshot(path=str(tmp_path / f"{surface}-off.png"), animations="disabled")
            dialog.get_by_role("button", name="Allow communication", exact=True).click()
            expect(dialog.get_by_role("status")).to_have_text("Communication is on.")
            expect(dialog.get_by_test_id("session-link")).to_have_count(0)
            assert client.get("/v1/session-communication/settings").json()["enabled"] is True
            page.screenshot(path=str(tmp_path / f"{surface}-on.png"), animations="disabled")
            page.reload()
            page.get_by_role("button", name="Session communication", exact=True).click()
            expect(dialog.get_by_role("status")).to_have_text("Communication is on.")
            dialog.get_by_role("button", name="Turn off communication", exact=True).click()
            expect(dialog.get_by_role("status")).to_have_text("Communication is off.")
            assert client.get("/v1/session-communication/settings").json()["enabled"] is False
            page.screenshot(path=str(tmp_path / f"{surface}-off-again.png"), animations="disabled")
        finally:
            client.delete(f"/v1/sessions/{manager}").raise_for_status()


@pytest.mark.timeout(180)
@pytest.mark.parametrize("harness", ["openai-agents"])
def test_browser_messages_and_canvas_activity(
    page, built_spa, messaging_runtime, tmp_path, harness
):
    base, mock, runner = messaging_runtime
    suffix = uuid4().hex[:8]
    page.set_viewport_size({"width": 1440, "height": 960})
    _login_page(page, base)
    with _owner_client(base) as client:
        sessions = []
        project = client.post("/v1/projects", json={"name": f"Messaging {suffix}"})
        project.raise_for_status()
        for title in ("Manager", "Worker"):
            created = post_session_bundle(
                client.post, "/v1/sessions", _sdk_bundle(title, mock), metadata={"title": title}
            )
            created.raise_for_status()
            sessions.append(created.json()["session_id"])
            bind_session_runner(client.patch, base, sessions[-1], runner)
            client.patch(
                f"/v1/sessions/{sessions[-1]}", json={"project_id": project.json()["id"]}
            ).raise_for_status()
        manager, worker = sessions

        def configure(marker, responses):
            configure_mock_llm(
                mock, responses, match=marker, required_tools=["sys_session_message"]
            )

        def tool(target, message):
            return {
                "name": "sys_session_message",
                "arguments": json.dumps({"session_id": target, "message": message}),
                "call_id": uuid4().hex,
            }

        def send(text):
            composer = page.get_by_role("textbox", name="Message the agent")
            composer.fill(text)
            composer.press("Enter")

        try:
            page.goto(f"{base}/c/{manager}")
            page.get_by_role("button", name="Session communication", exact=True).click()
            dialog = page.get_by_role("dialog", name="Session communication", exact=True)
            dialog.get_by_role("button", name="Allow communication", exact=True).click()
            expect(dialog.get_by_role("status")).to_have_text("Communication is on.")
            page.screenshot(path=str(tmp_path / "switch-on.png"), animations="disabled")
            page.keyboard.press("Escape")
            start, work, reply = f"ASSIGN_{suffix}", f"WORK_{suffix}", f"RESULT_{suffix}"
            configure(work, [{"text": "Private worker notes"}])
            configure(
                start, [{"tool_calls": [tool(worker, work)]}, {"text": "Sent work to the worker"}]
            )
            send(start)
            expect(page.get_by_text("Sent work to the worker", exact=True)).to_be_visible(
                timeout=60000
            )
            _wait_for_text(client, worker, "Private worker notes")
            assert "Private worker notes" not in json.dumps(_items(client, manager))
            page.goto(f"{base}/c/{worker}")
            expect(page.get_by_text("Private worker notes", exact=True)).to_be_visible()
            page.screenshot(path=str(tmp_path / "worker-received.png"), animations="disabled")
            configure(reply, [{"text": "Manager received the explicit reply"}])
            configure(work, [{"tool_calls": [tool(manager, reply)]}, {"text": "Sent result back"}])
            send("Send the result back")
            expect(page.get_by_text("Sent result back", exact=True)).to_be_visible(timeout=60000)
            _wait_for_text(client, manager, "Manager received the explicit reply")
            page.goto(f"{base}/canvas?canvas={project.json()['id']}")
            expect(page.locator(".react-flow__edge")).to_have_count(2, timeout=30000)
            page.screenshot(path=str(tmp_path / "canvas-activity.png"), animations="disabled")
            page.goto(f"{base}/c/{manager}")
            expect(
                page.get_by_text("Manager received the explicit reply", exact=True)
            ).to_be_visible()
            page.screenshot(path=str(tmp_path / "manager-reply.png"), animations="disabled")
            page.get_by_role("button", name="Session communication", exact=True).click()
            dialog.get_by_role("button", name="Turn off communication", exact=True).click()
            expect(dialog.get_by_role("status")).to_have_text("Communication is off.")
            page.screenshot(path=str(tmp_path / "switch-off.png"), animations="disabled")
        finally:
            for session_id in sessions:
                client.delete(f"/v1/sessions/{session_id}")
            client.delete(f"/v1/projects/{project.json()['id']}")
