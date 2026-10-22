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
from tests.e2e.test_session_links_e2e import _items, _owner_client, _sdk_bundle
from tests.e2e.test_session_links_e2e import linked_runtime as linked_runtime
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
            "OMNIGENT_FEATURES": "session_links,canvas",
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
            assert client.get("/v1/session-orchestration/settings").json()["enabled"] is True
            page.screenshot(path=str(tmp_path / f"{surface}-on.png"), animations="disabled")
            page.reload()
            page.get_by_role("button", name="Session communication", exact=True).click()
            expect(dialog.get_by_role("status")).to_have_text("Communication is on.")
            dialog.get_by_role("button", name="Turn off communication", exact=True).click()
            expect(dialog.get_by_role("status")).to_have_text("Communication is off.")
            assert client.get("/v1/session-orchestration/settings").json()["enabled"] is False
            page.screenshot(path=str(tmp_path / f"{surface}-off-again.png"), animations="disabled")
        finally:
            client.delete(f"/v1/sessions/{manager}").raise_for_status()


@pytest.mark.timeout(180)
@pytest.mark.parametrize("harness", ["openai-agents"])
def test_enable_delegate_read_reply_and_turn_off(
    page: Page, built_spa, linked_runtime, harness, tmp_path
) -> None:
    """Real signed login, browser, server and runner; the SDK model is scripted."""
    base, mock, runner = linked_runtime
    suffix = uuid4().hex[:8]
    manager_title, worker_title = f"Manager {suffix}", f"Worker {suffix}"
    manager_prompt = f"Ask {worker_title} to report progress."
    worker_prompt = f"Report progress for task {suffix}."

    def configure(prompt, responses):
        configure_mock_llm(mock, responses, match=prompt, required_tools=["sys_session_links"])

    def tool(name, arguments, call_id):
        return {"name": name, "arguments": json.dumps(arguments), "call_id": call_id}

    def send(prompt):
        composer = page.get_by_role("textbox", name="Message the agent")
        composer.fill(prompt)
        composer.press("Enter")

    page.set_viewport_size({"width": 1440, "height": 960})
    _login_page(page, base)
    with _owner_client(base) as client:
        sessions = []
        project = client.post("/v1/projects", json={"name": f"Team {suffix}"})
        project.raise_for_status()
        project_id = project.json()["id"]
        try:
            for title in (manager_title, worker_title):
                created = post_session_bundle(
                    client.post,
                    "/v1/sessions",
                    _sdk_bundle(title, mock),
                    metadata={"title": title},
                )
                created.raise_for_status()
                sessions.append(created.json()["session_id"])
                bind_session_runner(client.patch, base, sessions[-1], runner)
                client.patch(
                    f"/v1/sessions/{sessions[-1]}", json={"project_id": project_id}
                ).raise_for_status()
            manager, worker = sessions
            page.goto(f"{base}/c/{manager}")
            page.get_by_role("button", name="Session communication", exact=True).click()
            dialog = page.get_by_role("dialog", name="Session communication", exact=True)
            dialog.get_by_role("button", name="Allow communication", exact=True).click()
            expect(dialog.get_by_role("status")).to_have_text("Communication is on.")
            page.screenshot(path=str(tmp_path / "journey-connected.png"), animations="disabled")
            page.keyboard.press("Escape")

            ordinary = "The work is complete. This note stays in the worker session."
            configure(worker_prompt, [{"text": ordinary}])
            configure(
                manager_prompt,
                [
                    {
                        "tool_calls": [
                            tool(
                                "sys_session_delegate",
                                {"target_id": worker, "message": worker_prompt},
                                f"delegate_{suffix}",
                            )
                        ]
                    },
                    {"text": "Sent the request to the worker."},
                ],
            )
            send(manager_prompt)
            expect(page.get_by_text("Sent the request to the worker.", exact=True)).to_be_visible(
                timeout=45_000
            )
            page.screenshot(path=str(tmp_path / "journey-delegated.png"), animations="disabled")
            manager_items = _items(client, manager)
            output = next(
                item["output"]
                for item in manager_items
                if item.get("call_id") == f"delegate_{suffix}"
                and item.get("type") == "function_call_output"
            )
            receipt = json.loads(output)
            assert receipt["delivery_state"] == "accepted", receipt
            assert ordinary not in json.dumps(manager_items)

            page.goto(f"{base}/c/{worker}")
            expect(page.get_by_text(ordinary, exact=True)).to_be_visible(timeout=45_000)
            page.screenshot(
                path=str(tmp_path / "journey-worker-received.png"), animations="disabled"
            )
            reply = "The assigned work is complete and the checks passed."
            configure(
                worker_prompt,
                [
                    {
                        "tool_calls": [
                            tool(
                                "sys_session_reply",
                                {"request_id": receipt["id"], "message": reply},
                                f"reply_{suffix}",
                            )
                        ]
                    },
                    {"text": "Sent an addressed reply to the manager."},
                ],
            )
            send("Send an addressed reply to the manager with the result.")
            expect(
                page.get_by_text("Sent an addressed reply to the manager.", exact=True)
            ).to_be_visible(timeout=45_000)
            page.goto(f"{base}/c/{manager}")
            expect(page.get_by_text(reply, exact=False)).to_be_visible(timeout=15_000)
            page.screenshot(
                path=str(tmp_path / "journey-addressed-reply.png"), animations="disabled"
            )
            assert ordinary not in json.dumps(_items(client, manager))
            page.reload()
            expect(page.get_by_text(reply, exact=False)).to_be_visible(timeout=15_000)
            page.get_by_role("button", name="Session communication", exact=True).click()
            dialog.get_by_role("button", name="Turn off communication", exact=True).click()
            expect(dialog.get_by_role("status")).to_have_text("Communication is off.")
            expect(dialog.get_by_test_id("session-link")).to_have_count(1)
            page.keyboard.press("Escape")
            configure(
                manager_prompt,
                [
                    {
                        "tool_calls": [
                            tool(
                                "sys_session_delegate",
                                {"target_id": worker, "message": "New work after turning off"},
                                f"refused_{suffix}",
                            )
                        ]
                    },
                    {"text": "Communication is off; I could not send new work."},
                ],
            )
            send("Check the worker after turning off.")
            expect(
                page.get_by_text("Communication is off; I could not send new work.", exact=True)
            ).to_be_visible(timeout=45_000)
            assert "New work after turning off" not in json.dumps(_items(client, worker))
            page.screenshot(path=str(tmp_path / "journey-disconnected.png"), animations="disabled")
            page.goto(f"{base}/canvas?canvas={project_id}")
            edge = page.get_by_role(
                "group",
                name=f"{manager_title} to {worker_title}: communication activity",
                exact=True,
            )
            expect(edge).to_be_visible(timeout=15_000)
            edge.focus()
            page.keyboard.press("Enter")
            details = page.get_by_role("dialog", name="Communication activity", exact=True)
            expect(details).to_be_visible()
            page.screenshot(
                path=str(tmp_path / "journey-canvas-activity.png"), animations="disabled"
            )
            client.put(
                f"/v1/sessions/{manager}/permissions", json={"user_id": "viewer", "level": 1}
            ).raise_for_status()
            _login_page(page, base, "viewer")
            page.goto(f"{base}/c/{manager}")
            expect(page.get_by_text(reply, exact=False)).to_be_visible(timeout=15_000)
            page.get_by_role("button", name="Session communication", exact=True).click()
            expect(
                dialog.get_by_role("button", name="Allow communication", exact=True)
            ).to_have_count(0)
            expect(dialog.get_by_test_id("session-link")).to_have_count(0)
            page.screenshot(
                path=str(tmp_path / "journey-shared-reader.png"), animations="disabled"
            )
        finally:
            for session_id in sessions:
                client.delete(f"/v1/sessions/{session_id}").raise_for_status()

            client.delete(f"/v1/projects/{project_id}").raise_for_status()
