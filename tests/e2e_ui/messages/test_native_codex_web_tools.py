"""E2E regression: native Codex web turns can use built-in tools."""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.codex_parity.helpers import (
    ev_completed,
    ev_function_call,
    ev_response_created,
)
from tests.e2e_ui.conftest import MockedCodexNativeSession

from .test_message_render_parity import _ASSISTANT, _select_view_mode, _send

_SHELL_OUTPUT = "E2E_NATIVE_CODEX_WEB_SHELL_OUTPUT"
_COMMAND = f"printf '{_SHELL_OUTPUT}\\n'"
_PROMPT = "Run the shell command supplied by the mocked response."
_TOOL_RESPONSES = [
    [
        ev_response_created("resp-web-shell-tool"),
        ev_function_call(
            "call_web_shell_tool",
            "exec_command",
            json.dumps({"cmd": _COMMAND}),
        ),
        ev_completed("resp-web-shell-tool"),
    ]
]
_NATIVE_CODEX_TIMEOUT_MS = 180_000


@pytest.mark.parametrize(
    "mocked_native_codex_session",
    [_TOOL_RESPONSES],
    indirect=True,
    ids=["shell-tool"],
)
@pytest.mark.timeout(300)
def test_native_codex_web_turn_runs_shell_tool(
    page: Page,
    mocked_native_codex_session: MockedCodexNativeSession,
) -> None:
    """A composer-started native Codex turn executes and renders its shell tool."""
    session = mocked_native_codex_session
    page.goto(f"{session.base_url}/c/{session.session_id}")
    _select_view_mode(page, "Chat")

    _send(page, _PROMPT)

    assistant_turn = page.locator(_ASSISTANT).last
    shell_run = assistant_turn.get_by_role("button", name="Ran 1 shell command")
    expect(shell_run).to_be_visible(timeout=_NATIVE_CODEX_TIMEOUT_MS)
    shell_run.click()
    command_row = assistant_turn.get_by_role("button", name=re.compile(r"^printf "))
    expect(command_row).to_be_visible(timeout=30_000)
    command_row.click()
    expect(assistant_turn.get_by_text(_SHELL_OUTPUT, exact=True)).to_be_visible(timeout=30_000)

    requests = session.sidecar.requests(min_count=1, timeout_ms=30_000)
    turn_request = next(
        request for request in requests if _PROMPT in str(request["body"]["input"])
    )
    advertised_tools = {
        tool.get("name")
        for tool in turn_request["body"].get("tools", [])
        if isinstance(tool, dict)
    }
    assert "exec_command" in advertised_tools
