"""Hosted side chats share the live parent's real runner and stop with it."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bundle_files, post_session_bundle
from tests.e2e_ui.chat.test_side_chat_entrypoints import _items, _send_parent, _start_side_chat
from tests.e2e_ui.conftest import configure_mock_llm, reset_mock_llm, set_fallback_mock_llm

_REPO = Path(__file__).resolve().parents[3]


def _wait(check, description: str, timeout: float = 60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError(f"Timed out waiting for {description}")


@pytest.mark.timeout(240)
@pytest.mark.parametrize("entrypoint", ["menu", "context", "mobile"])
def test_stop_session_stops_hosted_side_chats(
    page: Page,
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path: Path,
    entrypoint: str,
) -> None:
    """Close one chat, stop the parent, then restart it through a fresh chat."""
    if os.environ.get("OMNIGENT_REPRO_SERVER_URL"):
        pytest.fail("This test owns its host and runners; run it outside verify-env run.")
    base_env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    }
    env = {
        "PYTHONPATH": str(_REPO),
        "OMNIGENT_CONFIG_HOME": str(tmp_path / "config"),
        "OMNIGENT_SKIP_ONBOARD": "1",
        "OMNIGENT_NO_UPDATE_CHECK": "1",
    }
    spec = {
        "name": "hosted-side-chat",
        "prompt": "Answer briefly.",
        "executor": {
            "harness": "openai-agents",
            "model": "gpt-4o-mini",
            "auth": {
                "type": "api_key",
                "api_key": "mock-key",
                "base_url": f"{mock_llm_server_url}/v1",
            },
        },
        "os_env": {"type": "caller_process", "cwd": ".", "sandbox": {"type": "none"}},
    }
    bundle = bundle_files({"agent.yaml": yaml.safe_dump(spec).encode()})
    set_fallback_mock_llm(mock_llm_server_url, "_policy_llm_", '{"action":"allow","reason":""}')
    with (
        server_runner(
            tmp_path,
            server_cwd=_REPO,
            base_env=base_env,
            server_env={
                **env,
                "OMNIGENT_RUNNER_TUNNEL_TOKEN": None,
                "OMNIGENT_WEB_UI_DIST": os.environ.get("OMNIGENT_WEB_UI_DIST")
                or str(_REPO / "omnigent/server/static/web-ui"),
            },
            poll_interval=0.2,
        ) as stack,
        httpx.Client(
            base_url=stack.base_url,
            timeout=60,
            trust_env=False,
            headers={
                "Origin": OMNIGENT_INTERNAL_WS_ORIGIN,
                "x-omnigent-background-session-titles": "off",
            },
        ) as client,
    ):
        stack.start_host(env=env, cwd=_REPO)

        def get(path: str):
            response = client.get(path)
            response.raise_for_status()
            return response.json()

        host = _wait(
            lambda: next((h for h in get("/v1/hosts")["hosts"] if h["status"] == "online"), None),
            "isolated host registration",
        )

        def create(title: str) -> str:
            response = post_session_bundle(
                client.post,
                "/v1/sessions",
                bundle,
                metadata={
                    "host_id": host["host_id"],
                    "workspace": str(stack.workspace),
                    "title": title,
                },
            )
            response.raise_for_status()
            return response.json()["session_id"]

        parent_id = create("Stop with side chats")
        unrelated_id = create("Unrelated session")
        runner_launches: list[str] = []
        page.on(
            "request",
            lambda request: (
                runner_launches.append(request.url)
                if request.method == "POST"
                and "/v1/hosts/" in request.url
                and request.url.endswith("/runners")
                else None
            ),
        )
        page.goto(f"{stack.base_url}/c/{parent_id}")
        parent_prompt = f"parent-{parent_id}: remember this conversation"
        reset_mock_llm(mock_llm_server_url)
        configure_mock_llm(
            mock_llm_server_url, [{"text": "Parent history is kept."}], match=parent_prompt
        )
        _send_parent(page, parent_prompt, "Parent history is kept.")
        parent_items = _items(stack.base_url, parent_id)

        def side_chat(question: str, reply: str) -> str:
            reset_mock_llm(mock_llm_server_url)
            configure_mock_llm(mock_llm_server_url, [{"text": reply}], match=question)
            with page.expect_response(f"**/v1/sessions/{parent_id}/fork") as fork:
                _start_side_chat(page, "panel", question)
            assert fork.value.ok
            child_id = fork.value.json()["id"]
            expect(
                page.locator(".side-chat-backdrop").get_by_text(reply, exact=True)
            ).to_be_visible(timeout=60_000)
            return child_id

        closed_id = side_chat(f"closed-{parent_id}: first question", "Only this chat will close.")
        child_id = side_chat(f"active-{parent_id}: second question", "Side chat history is kept.")
        snapshots = {
            session_id: get(f"/v1/sessions/{session_id}")
            for session_id in (parent_id, closed_id, child_id, unrelated_id)
        }
        runners = {session_id: snapshot["runner_id"] for session_id, snapshot in snapshots.items()}
        assert runners[parent_id] == runners[closed_id] == runners[child_id], snapshots
        assert runners[unrelated_id] != runners[parent_id], snapshots
        assert not runner_launches, runner_launches

        def online(session_id: str) -> bool:
            return get(f"/v1/runners/{runners[session_id]}/status")["online"]

        pane = page.locator(".side-chat-backdrop")

        def block_side_chat(prompt: str) -> None:
            reset_mock_llm(mock_llm_server_url)
            configure_mock_llm(
                mock_llm_server_url,
                [{"text": "This unfinished answer must never arrive.", "block": True}],
                match=prompt,
            )
            page.get_by_test_id("side-chat-input").fill(prompt)
            page.get_by_test_id("side-chat-send").click()
            expect(pane.get_by_test_id("working-indicator")).to_be_visible()
            _wait(
                lambda: httpx.get(f"{mock_llm_server_url}/gate/pending", timeout=5).json()[
                    "pending"
                ],
                "side chat's model response to block",
            )

        page.get_by_role("tab", name="Side chat 1", exact=True).click()
        block_side_chat(f"closing-{parent_id}: keep working until this tab closes")
        with page.expect_response(
            lambda response: (
                response.url.endswith(f"/v1/sessions/{closed_id}/events")
                and response.request.method == "POST"
                and response.request.post_data_json.get("type") == "stop_session"
            )
        ) as closed:
            page.get_by_role("button", name="Close Side chat 1", exact=True).click()
        assert closed.value.ok
        assert closed.value.request.post_data_json["type"] == "stop_session"
        assert all(online(sid) for sid in runners)
        _wait(lambda: get(f"/v1/sessions/{closed_id}")["status"] == "idle", "closed chat to stop")
        assert "Only this chat will close." in str(_items(stack.base_url, closed_id))
        reset_mock_llm(mock_llm_server_url)
        followup = f"followup-{parent_id}: keep working after closing one chat"
        configure_mock_llm(
            mock_llm_server_url, [{"text": "The parent still works."}], match=followup
        )
        _send_parent(page, followup, "The parent still works.")
        parent_items = _items(stack.base_url, parent_id)

        block_side_chat(f"blocked-{parent_id}: keep working until stopped")

        desktop_viewport = page.viewport_size
        row = page.locator(f'li[data-sidebar-session-id="{parent_id}"]')
        if entrypoint == "mobile":
            page.set_viewport_size({"width": 390, "height": 844})
            if not row.is_visible():
                page.get_by_role("button", name="Open sidebar", exact=True).click()
        if entrypoint == "mobile":
            box = row.bounding_box()
            assert box is not None
            cdp = page.context.new_cdp_session(page)
            point = {"x": box["x"] + box["width"] / 2, "y": box["y"] + box["height"] / 2}
            cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [point]})
            page.wait_for_timeout(750)
            cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
            cdp.detach()
        elif entrypoint == "context":
            row.click(button="right")
        else:
            row.hover()
            row.get_by_test_id("conversation-actions").click()
        page.get_by_test_id("stop-conversation").click()
        page.screenshot(path=str(tmp_path / "before-stop.png"))
        with page.expect_response(
            lambda response: (
                response.url.endswith(f"/v1/sessions/{parent_id}/events")
                and response.request.method == "POST"
                and response.request.post_data_json.get("type") == "stop_session"
            )
        ) as stopped:
            page.get_by_test_id("stop-session-confirm").click()
        assert stopped.value.ok
        assert stopped.value.request.post_data_json["type"] == "stop_session"
        _wait(
            lambda: not online(parent_id) and not online(child_id),
            "the shared parent and side-chat runner to stop",
        )
        stopped_runners = {session_id: online(session_id) for session_id in runners}
        assert stopped_runners == {
            parent_id: False,
            closed_id: False,
            child_id: False,
            unrelated_id: True,
        }
        assert _items(stack.base_url, parent_id) == parent_items
        assert "Side chat history is kept." in str(_items(stack.base_url, child_id))
        if entrypoint == "mobile":
            page.screenshot(path=str(tmp_path / "mobile-stopped.png"))
            assert desktop_viewport is not None
            page.set_viewport_size(desktop_viewport)
        expect(pane.get_by_test_id("working-indicator")).to_have_count(0, timeout=120_000)
        expect(pane.get_by_text("Side chat history is kept.", exact=True)).to_be_visible()
        expect(pane.get_by_test_id("error-pill")).to_have_count(0)
        page.screenshot(path=str(tmp_path / "after-stop.png"))

        fresh_id = side_chat(f"fresh-{parent_id}: start a new chat", "A new side chat works.")
        fresh = get(f"/v1/sessions/{fresh_id}")
        restarted_parent = get(f"/v1/sessions/{parent_id}")
        assert restarted_parent["runner_id"] != runners[parent_id]
        assert fresh["runner_id"] == restarted_parent["runner_id"]
        assert fresh["host_id"] is None
        assert not runner_launches, runner_launches
        assert get(f"/v1/runners/{fresh['runner_id']}/status")["online"]
        assert get(f"/v1/runners/{restarted_parent['runner_id']}/status")["online"]
        assert not online(parent_id) and not online(child_id)
        assert online(unrelated_id)
        restarted_items = _items(stack.base_url, parent_id)
        assert [
            item
            for item in restarted_items
            if not str(item.get("event_type", "")).startswith("session.resource.")
        ] == [
            item
            for item in parent_items
            if not str(item.get("event_type", "")).startswith("session.resource.")
        ]
        page.screenshot(path=str(tmp_path / "fresh-side-chat.png"))
        assert stack.server is not None and stack.host is not None
        (tmp_path / "evidence.json").write_text(
            json.dumps(
                {
                    "entrypoint": entrypoint,
                    "server_version": get("/api/version"),
                    "server_process": stack.server.args,
                    "host_process": stack.host.args,
                    "runner_launches": runner_launches,
                    "before": snapshots,
                    "runner_online_after_stop": stopped_runners,
                    "restarted_parent": restarted_parent,
                    "fresh": fresh,
                },
                indent=2,
            )
        )
