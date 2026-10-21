"""Human approval and independent Canvas directions through the built SPA and real server."""

from __future__ import annotations

import json
from collections.abc import Iterator
from uuid import uuid4

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests._helpers.live_server import isolated_local_server
from tests._helpers.session import bind_session_runner, post_session_bundle
from tests.e2e.conftest import isolated_mock_llm_server_url as isolated_mock_llm_server_url
from tests.e2e.test_session_links_e2e import _items, _sdk_bundle
from tests.e2e.test_session_links_e2e import linked_runtime as linked_runtime
from tests.e2e_ui.conftest import _build_hello_world_bundle, configure_mock_llm
from tests.helpers.ui_configuration import prepared_repro_environment


@pytest.fixture(scope="module")
def link_ui_server(built_spa, tmp_path_factory) -> Iterator[str]:
    prepared = prepared_repro_environment()
    if base := prepared["OMNIGENT_REPRO_SERVER_URL"]:
        features = httpx.get(f"{base}/v1/info", trust_env=False).json()["features"]
        assert features.get("session_links") and features.get("canvas"), features
        yield base
    else:
        with isolated_local_server(
            tmp_path_factory.mktemp("session-links-ui"),
            env={"OMNIGENT_FEATURES": "session_links,canvas"},
        ) as base:
            yield base


@pytest.mark.parametrize(
    "surface", ["conversation", "desktop-layout", "canvas", "mobile", "mobile-canvas"]
)
def test_connect_reload_and_revoke_each_direction(
    page: Page, link_ui_server: str, tmp_path, surface: str
) -> None:
    """Desktop layout uses Chromium shell chrome; it does not test an Electron process."""
    base = link_ui_server
    is_canvas = surface.endswith("canvas")
    page.set_viewport_size(
        {"width": 390, "height": 844}
        if surface.startswith("mobile")
        else {"width": 1440, "height": 960}
    )
    if surface == "desktop-layout":
        page.add_init_script(
            """window.omnigentDesktop = {
                kind: 'electron', setBadgeCount() {},
                notify: () => Promise.resolve(false), onNotificationActivated: () => () => {},
                getServerPicker: () => Promise.resolve(null),
                switchServer: () => Promise.resolve(),
                openServerSetup() {},
            };"""
        )
    suffix = uuid4().hex[:8]
    names = [f"Manager {suffix}", f"Worker {suffix}"]
    bundle = _build_hello_world_bundle()
    with httpx.Client(base_url=base, trust_env=False, timeout=30) as client:
        sessions = []
        project = client.post("/v1/projects", json={"name": f"Linked work {suffix}"})
        project.raise_for_status()
        project_id = project.json()["id"]
        try:
            for title in names:
                response = post_session_bundle(
                    client.post, "/v1/sessions", bundle, metadata={"title": title}
                )
                response.raise_for_status()
                session_id = response.json()["session_id"]
                sessions.append(session_id)
                client.patch(
                    f"/v1/sessions/{session_id}", json={"project_id": project_id}
                ).raise_for_status()
            manager, worker = sessions
            route = f"/canvas/c/{manager}?canvas={project_id}" if is_canvas else f"/c/{manager}"
            page.goto(base + route)
            page.get_by_role("button", name="Session links", exact=True).click()
            dialog = page.get_by_role("dialog", name="Session links", exact=True)
            dialog.get_by_role("textbox", name="Find a session you own").fill(names[1])
            dialog.get_by_role("button", name=names[1], exact=False).click()
            page.screenshot(
                path=str(tmp_path / f"{surface}-before-connect.png"), animations="disabled"
            )
            dialog.get_by_role("button", name="Connect", exact=True).click()
            expect(dialog.get_by_test_id("session-link")).to_have_count(1)
            page.screenshot(path=str(tmp_path / f"{surface}-connected.png"), animations="disabled")
            assert (
                client.get(f"/v1/sessions/{manager}/links").json()["data"][0]["target_id"]
                == worker
            )
            page.keyboard.press("Escape")
            page.reload()
            page.get_by_role("button", name="Session links", exact=True).click()
            expect(dialog.get_by_test_id("session-link")).to_have_count(1)
            dialog.get_by_role("button", name="Disconnect", exact=True).click()
            expect(dialog.get_by_test_id("session-link")).to_have_count(0)
            assert client.get(f"/v1/sessions/{manager}/links").json()["data"] == []
            page.screenshot(
                path=str(tmp_path / f"{surface}-disconnected.png"), animations="disabled"
            )
            page.keyboard.press("Escape")

            if not is_canvas:
                return
            for source, target_name in ((manager, names[1]), (worker, names[0])):
                page.goto(f"{base}/canvas/c/{source}?canvas={project_id}")
                page.get_by_role("button", name="Session links", exact=True).click()
                dialog.get_by_role("textbox", name="Find a session you own").fill(target_name)
                dialog.get_by_role("button", name=target_name, exact=False).click()
                dialog.get_by_role("button", name="Connect", exact=True).click()
                expect(dialog.get_by_test_id("session-link")).to_have_count(
                    1 if source == manager else 2
                )
                page.keyboard.press("Escape")
            page.goto(f"{base}/canvas?canvas={project_id}")
            edges = page.locator(".react-flow__edge")
            expect(edges).to_have_count(2)
            page.screenshot(
                path=str(tmp_path / f"{surface}-directions.png"), animations="disabled"
            )
            edge = page.get_by_role("group", name=f"{names[1]} to {names[0]}: linked", exact=True)
            edge.focus()
            page.keyboard.press("Enter")
            details = page.get_by_role("dialog", name="Session link", exact=True)
            expect(details).to_be_visible()
            expect(details.get_by_role("button", name="Disconnect", exact=True)).to_be_visible()
            assert len(client.get(f"/v1/sessions/{worker}/links").json()["data"]) == 1
            page.screenshot(
                path=str(tmp_path / f"{surface}-direction-selected.png"), animations="disabled"
            )
            details.get_by_role("button", name="Disconnect", exact=True).click()
            expect(details).not_to_be_visible()
            expect(edges).to_have_count(1)
            assert client.get(f"/v1/sessions/{worker}/links").json()["data"] == []
            assert (
                client.get(f"/v1/sessions/{manager}/links").json()["data"][0]["target_id"]
                == worker
            )
            page.reload()
            expect(edges).to_have_count(1)
            page.screenshot(
                path=str(tmp_path / f"{surface}-one-direction-retained.png"), animations="disabled"
            )
        finally:
            for session_id in sessions:
                client.delete(f"/v1/sessions/{session_id}").raise_for_status()
            client.delete(f"/v1/projects/{project_id}").raise_for_status()


@pytest.mark.timeout(180)
@pytest.mark.parametrize("harness", ["openai-agents"])
def test_connect_delegate_read_reply_and_disconnect(
    page: Page, built_spa, linked_runtime, harness, tmp_path
) -> None:
    """Real browser/server/runner with scripted SDK tool calls; human auth is local-only."""
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
    with httpx.Client(base_url=base, trust_env=False, timeout=60) as client:
        sessions = []
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
            manager, worker = sessions
            page.goto(f"{base}/c/{manager}")
            page.get_by_role("button", name="Session links", exact=True).click()
            dialog = page.get_by_role("dialog", name="Session links", exact=True)
            dialog.get_by_role("textbox", name="Find a session you own").fill(worker_title)
            dialog.get_by_role("button", name=worker_title, exact=False).click()
            dialog.get_by_role("button", name="Connect", exact=True).click()
            expect(dialog.get_by_test_id("session-link")).to_have_count(1)
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
                    {"text": "Sent the request to the linked worker."},
                ],
            )
            send(manager_prompt)
            expect(
                page.get_by_text("Sent the request to the linked worker.", exact=True)
            ).to_be_visible(timeout=45_000)
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
            page.get_by_role("button", name="Session links", exact=True).click()
            dialog.get_by_role("button", name="Disconnect", exact=True).click()
            expect(dialog.get_by_test_id("session-link")).to_have_count(0)
            page.keyboard.press("Escape")
            configure(
                manager_prompt,
                [
                    {
                        "tool_calls": [
                            tool(
                                "sys_session_delegate",
                                {"target_id": worker, "message": "New work after disconnect"},
                                f"refused_{suffix}",
                            )
                        ]
                    },
                    {"text": "The link is disconnected; I could not send new work."},
                ],
            )
            send("Check the disconnected worker.")
            expect(
                page.get_by_text(
                    "The link is disconnected; I could not send new work.", exact=True
                )
            ).to_be_visible(timeout=45_000)
            assert "New work after disconnect" not in json.dumps(_items(client, worker))
            page.screenshot(path=str(tmp_path / "journey-disconnected.png"), animations="disabled")
        finally:
            for session_id in sessions:
                client.delete(f"/v1/sessions/{session_id}").raise_for_status()


def test_stale_link_cleanup_hides_inaccessible_session(page: Page, built_spa, tmp_path) -> None:
    page.set_viewport_size({"width": 1440, "height": 960})
    server_root = tmp_path / "server"
    alice = {"X-Forwarded-Email": "alice"}
    bob = {"X-Forwarded-Email": "bob"}
    with (
        isolated_local_server(
            server_root,
            env={"OMNIGENT_FEATURES": "session_links", "OMNIGENT_LOCAL_SINGLE_USER": None},
        ) as base,
        httpx.Client(base_url=base, headers=alice, trust_env=False, timeout=30) as client,
    ):
        sessions = []
        transferred = False
        bundle = _build_hello_world_bundle()
        try:
            for title in ("Manager", "Private worker"):
                response = post_session_bundle(
                    client.post, "/v1/sessions", bundle, metadata={"title": title}
                )
                response.raise_for_status()
                sessions.append(response.json()["session_id"])
            manager, worker = sessions
            page.set_extra_http_headers(alice)
            page.goto(f"{base}/c/{manager}")
            page.get_by_role("button", name="Session links", exact=True).click()
            dialog = page.get_by_role("dialog", name="Session links", exact=True)
            dialog.get_by_role("textbox", name="Find a session you own").fill("Private worker")
            dialog.get_by_role("button", name="Private worker", exact=False).click()
            dialog.get_by_role("button", name="Connect", exact=True).click()
            expect(dialog.get_by_test_id("session-link")).to_have_count(1)

            # Ownership transfer has no public UI; install it in this test's database.
            # Revoke the former owner's remaining access through the API to evict caches.
            permissions = SqlAlchemyPermissionStore(f"sqlite:///{server_root / 'chat.db'}")
            permissions.ensure_user("bob")
            permissions.grant("bob", worker, 4)
            permissions.grant("alice", worker, 2)
            transferred = True
            client.delete(
                f"/v1/sessions/{worker}/permissions/alice", headers=bob
            ).raise_for_status()
            page.reload()
            page.get_by_role("button", name="Session links", exact=True).click()
            link = dialog.get_by_test_id("session-link")
            expect(link.get_by_text("Restricted session", exact=True)).to_be_visible()
            expect(link.get_by_text("Private worker", exact=True)).to_have_count(0)
            expect(link.get_by_role("button", name="Disconnect", exact=True)).to_be_visible()
            page.screenshot(path=str(tmp_path / "stale-outgoing-link.png"), animations="disabled")
            assert client.get(f"/v1/sessions/{worker}").status_code in (403, 404)

            page.set_extra_http_headers(bob)
            page.goto(f"{base}/c/{worker}")
            page.get_by_role("button", name="Session links", exact=True).click()
            expect(link.get_by_text("Restricted session", exact=True)).to_be_visible()
            expect(link.get_by_text("Manager", exact=True)).to_have_count(0)
            page.screenshot(path=str(tmp_path / "stale-incoming-link.png"), animations="disabled")
            link.get_by_role("button", name="Disconnect", exact=True).click()
            expect(dialog.get_by_test_id("session-link")).to_have_count(0)
            assert client.get(f"/v1/sessions/{manager}/links").json()["data"] == []
            assert client.get(f"/v1/sessions/{manager}", headers=bob).status_code in (403, 404)
            page.screenshot(path=str(tmp_path / "stale-link-removed.png"), animations="disabled")
        finally:
            for index, session_id in enumerate(sessions):
                client.delete(
                    f"/v1/sessions/{session_id}",
                    headers=bob if index == 1 and transferred else alice,
                ).raise_for_status()
