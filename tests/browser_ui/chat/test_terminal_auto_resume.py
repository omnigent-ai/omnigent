"""Terminal navigation recovers once through the session retry API."""

from __future__ import annotations

import json
import re
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path

import pytest
from playwright.sync_api import Error, Page, Route, WebSocketRoute, expect

from tests.browser_ui.chat.session_contract import (
    ChatSessionContract,
    chat_session_handle,
    list_payload,
)


@pytest.mark.parametrize(
    ("entry", "fail_first", "cached_terminal"),
    [
        ("switch", False, False),
        ("shortcut", False, False),
        ("mobile", False, False),
        ("grid", False, False),
        ("grid", True, False),
        ("switch", True, False),
        pytest.param("switch", True, True, id="cached-terminal"),
    ],
)
def test_terminal_view_recovers_on_open(
    page: Page,
    chat_session_contract: ChatSessionContract,
    output_path: str,
    request: pytest.FixtureRequest,
    entry: str,
    fail_first: bool,
    cached_terminal: bool,
) -> None:
    chat = chat_session_contract
    contract = chat.contract
    artifacts = Path(output_path)
    chat.harness = "codex-native"
    chat.update_session(
        labels={"omnigent.ui": "terminal", "omnigent.wrapper": "codex-native-ui"},
        runner_online=not cached_terminal,
        host_online=True,
    )
    chat.set_health(runner_online=not cached_terminal, host_online=True)
    contract.json(f"/v1/sessions/{chat.session_id}/codex_goal", {"goal": None})
    resource = {
        "id": "terminal_codex_main",
        "object": "session.resource",
        "type": "terminal",
        "session_id": chat.session_id,
        "name": "codex:main",
        "metadata": {"terminal_name": "codex", "session_key": "main", "running": True},
    }
    terminals: list[dict[str, object]] = [resource] if cached_terminal else []
    pending: list[Route] = []
    attachments: list[WebSocketRoute] = []

    def release_retries() -> None:
        for route in pending:
            with suppress(Error):
                route.abort()

    request.addfinalizer(release_retries)
    contract.json(
        f"/v1/sessions/{chat.session_id}/resources/terminals", lambda _: list_payload(terminals)
    )

    def retry(route: Route) -> None:
        assert route.request.method == "POST"
        assert route.request.post_data_json == {"type": "retry_session", "data": {}}
        pending.append(route)

    contract.route(
        re.compile(rf"{re.escape(chat.url.replace('/c/', '/v1/sessions/'))}/events$"), retry
    )

    def attach(socket: WebSocketRoute) -> None:
        attachments.append(socket)
        if cached_terminal and len(attachments) == 1:
            socket.send(b"Previous terminal output.\r\n$ ")
        else:
            socket.send(b"Terminal reconnected.\r\n$ ")

    contract.websocket("**/resources/terminals/terminal_codex_main/attach*", attach)
    page.add_init_script("localStorage.setItem('omnigent:default-transcript-view', 'chat')")
    page.set_viewport_size({"width": 390 if entry == "mobile" else 1440, "height": 900})

    with ExitStack() as stack:
        if entry == "grid":
            other = stack.enter_context(
                contextmanager(chat_session_handle)(page, contract, session_id="other-session")
            )
            contract.json(
                "/v1/info", {"single_user": True, "features": {"grid_sessions_display_ui": True}}
            )
            contract.json(
                "/v1/sessions", list_payload([chat.session_snapshot(), other.session_snapshot()])
            )
            contract.json(
                "/health",
                {
                    "sessions": {
                        s.session_id: {"host_online": True, "runner_online": True}
                        for s in [chat, other]
                    }
                },
            )
            page.goto(chat.url)
            page.evaluate(
                """ids => localStorage.setItem(
                  `omnigent:session-grids:${location.origin}:local`,
                  JSON.stringify([{id: 'recovery', layout: {axis: 'columns', children: ids}}]))""",
                [chat.session_id, other.session_id],
            )
            page.goto(f"{chat.base_url}/grid/recovery")
            surface = page.locator(f'[data-grid-session="{chat.session_id}"]')
        else:
            page.goto(chat.url)
            surface = page.locator("body")

        chat.wait_for_stream()
        if cached_terminal:
            previous_terminal = page.get_by_test_id("main-terminal-view").get_by_test_id(
                "terminal-view"
            )
            expect(previous_terminal).to_have_attribute("data-state", "connected")
            attachments[0].close(code=4404, reason="Terminal not found")
            expect(previous_terminal).to_have_attribute("data-state", "closed")
            previous_terminal.evaluate("element => element.dataset.recoveryOriginal = 'true'")
        assert pending == []
        page.screenshot(path=str(artifacts / "before-opening-terminal.png"))
        with page.expect_request(
            lambda request: (
                request.url.endswith(f"/v1/sessions/{chat.session_id}/events")
                and request.method == "POST"
            )
        ):
            if entry == "shortcut":
                page.keyboard.press("ControlOrMeta+Alt+Backslash")
            elif entry == "mobile":
                page.get_by_role("button", name="Conversation actions", exact=True).click()
                page.get_by_role("menuitem", name="Terminal", exact=True).click()
            else:
                surface.get_by_role("button", name="Terminal view", exact=True).click()

        terminal = page.locator('[data-testid="main-terminal-view"][data-visible="true"]')
        expect(terminal.get_by_role("status")).to_be_visible()
        expect(terminal.get_by_role("button", name="Resume session")).to_have_count(0)
        assert len(pending) == 1
        if cached_terminal:
            expect(terminal.get_by_test_id("terminal-view")).to_have_attribute(
                "data-recovery-original", "true"
            )
            assert len(attachments) == 1
        page.screenshot(path=str(artifacts / "terminal-restarting.png"))

        if fail_first:
            pending[0].fulfill(
                status=503,
                content_type="application/json",
                body=json.dumps({"error": {"code": "unavailable", "message": "Restart failed"}}),
            )
            resume = terminal.get_by_role("button", name="Resume session", exact=True)
            expect(resume).to_be_enabled()
            expect(terminal.get_by_role("alert")).to_be_visible()
            assert len(pending) == 1
            if entry == "grid":
                surface.get_by_role("button", name="Chat view", exact=True).click()
                surface.get_by_role("button", name="Terminal view", exact=True).click()
                expect(terminal.get_by_role("alert")).to_be_visible()
                expect(resume).to_be_enabled()
                assert len(pending) == 1
            if cached_terminal:
                expect(terminal.get_by_test_id("terminal-view")).to_have_attribute(
                    "data-recovery-original", "true"
                )
                assert len(attachments) == 1
            page.screenshot(path=str(artifacts / "terminal-restart-failed.png"))
            with page.expect_request(
                lambda request: (
                    request.url.endswith(f"/v1/sessions/{chat.session_id}/events")
                    and request.method == "POST"
                )
            ):
                resume.click()
            expect(terminal.get_by_role("status")).to_be_visible()
            expect(resume).to_have_count(0)
            assert len(pending) == 2

        if not cached_terminal:
            terminals.append(resource)
        pending[-1].fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {"queued": False, "recovered": True, "recovery": "native_terminal_ready"}
            ),
        )
        chat.emit({"event": "session.resource.created", "data": {"resource": resource}})
        attached = terminal.get_by_test_id("terminal-view")
        expect(attached).to_have_attribute("data-state", "connected", timeout=15_000)
        assert len(attachments) == (2 if cached_terminal else 1)
        expect(terminal.get_by_role("button", name="Resume session")).to_have_count(0)
        assert len(pending) == (2 if fail_first else 1)
        page.screenshot(path=str(artifacts / "terminal-reconnected.png"))
