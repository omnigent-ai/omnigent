"""UI journey: messages an agent sends into another session must name their sender."""

from __future__ import annotations

import contextlib
import json
import re
import time
import uuid

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from tests._helpers.session import bundle_files
from tests.e2e_ui.conftest import (
    configure_mock_llm,
    open_right_rail,
    reset_mock_llm,
    set_fallback_mock_llm,
)
from tests.e2e_ui.messages.test_message_render_parity import _ensure_chat_view, _send
from tests.e2e_ui.messages.test_native_claude_render_parity import (
    _CLAUDE_MOCK_MODEL,
    _open_terminal_view,
    _wait_terminal_connected,
)

_CREATE_TOOL = "mcp__omnigent__sys_session_create"
_SEND_TOOL = "mcp__omnigent__sys_session_send"
_MESSAGE = '[data-testid="message-bubble"]'
_SUBAGENT_ROW = '[data-testid="subagent-row"]'
_APPROVAL_CARD = '[data-testid="approval-card"][data-state="pending"]'


def _install_receiving_agent(base_url: str, name: str) -> str:
    spec = (
        f"spec_version: 1\nname: {name}\nprompt: Reply in one short sentence.\n"
        "executor:\n  model: gpt-4o-mini\n  config:\n    harness: openai-agents\n"
    )
    installed = httpx.post(
        f"{base_url}/v1/agents",
        files={"bundle": ("agent.tar.gz", bundle_files({"config.yaml": spec.encode()}))},
        timeout=20.0,
    )
    installed.raise_for_status()
    return str(installed.json()["id"])


def _text(item: dict) -> str:
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    return " ".join(str(block.get("text", "")) for block in content if isinstance(block, dict))


def _approve_pending(page: Page) -> None:
    cards = page.locator(_APPROVAL_CARD)
    if cards.count():
        # The card may resolve after count(); the next poll retries any still pending.
        with contextlib.suppress(PlaywrightTimeoutError):
            cards.first.get_by_role("button", name="Approve", exact=True).click(timeout=2_000)


def _wait_reply(page: Page, base_url: str, session_id: str, marker: str, reply: str) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        _approve_pending(page)
        snapshot = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0).json()
        assert not snapshot.get("last_task_error"), snapshot["last_task_error"]
        items = httpx.get(
            f"{base_url}/v1/sessions/{session_id}/items",
            params={"limit": 200, "order": "asc"},
            timeout=10.0,
        ).json()["data"]
        messages = [item for item in items if item.get("type") == "message"]
        asked = next((i for i, m in enumerate(messages) if marker in _text(m)), None)
        replied = asked is not None and any(
            m.get("role") == "assistant" and reply in _text(m) for m in messages[asked + 1 :]
        )
        if replied and snapshot["status"] == "idle" and not snapshot.get("pending_inputs"):
            return
        page.wait_for_timeout(500)
    raise AssertionError(f"no {reply!r} reply to {marker!r} in session {session_id}")


def _advertised_tools(mock_url: str, marker: str) -> set[str]:
    response = httpx.get(f"{mock_url}/mock/requests", timeout=10.0, trust_env=False)
    response.raise_for_status()
    return {
        str(tool.get("name"))
        for captured in response.json()["requests"]
        if marker in json.dumps(captured)
        for tool in captured.get("tools", [])
    }


def _send_until_tools_ready(
    page: Page, base_url: str, session_id: str, mock_url: str, reply: str
) -> None:
    # Claude Code can answer before its Omnigent MCP tools finish connecting.
    for _attempt in range(4):
        marker = f"ready{uuid.uuid4().hex[:8]}"
        _send(page, f"Hello, are you there? ({marker})")
        _wait_reply(page, base_url, session_id, marker, reply)
        if {_CREATE_TOOL, _SEND_TOOL} <= _advertised_tools(mock_url, marker):
            return
    raise AssertionError(f"Claude never advertised {_CREATE_TOOL} and {_SEND_TOOL}")


def _agents_rail(page: Page) -> Locator:
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Agents")).click()
    return rail


def _scripted_call(name: str, call_id: str, arguments: dict, reply: str) -> list[dict]:
    return [
        {"tool_calls": [{"call_id": call_id, "name": name, "arguments": json.dumps(arguments)}]},
        {"text": reply},
    ]


def _unlabeled_rendering(page: Page, text: str, sender: re.Pattern[str]) -> str | None:
    message = page.locator(_MESSAGE, has_text=text).first
    expect(message).to_be_visible(timeout=60_000)
    label = message.get_by_test_id("message-source")
    if label.count() and sender.search(label.inner_text()):
        return None
    return f"{message.get_attribute('data-role')!r} bubble {message.inner_text()!r}"


@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_agent_sent_cross_session_messages_name_their_sender(
    request: pytest.FixtureRequest,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    base_url, parent_id = native_claude_mock_session
    mock_url = mock_llm_server_url
    nonce = uuid.uuid4().hex[:8]
    agent_id = _install_receiving_agent(base_url, f"helper-{nonce}")
    created_text = f"Helper task {nonce}: list three primary colors."
    sent_text = f"Follow-up {nonce}: now list three secondary colors."
    # Equal-length markers: the mock routes a resent conversation to the newest one.
    create_marker, send_marker = f"mkcreate{nonce}", f"mksendxx{nonce}"
    created_reply, sent_reply = "I started the Color helper.", "I sent the Color helper more."

    reset_mock_llm(mock_url)
    set_fallback_mock_llm(mock_url, "default", f"Colors noted {nonce}.")
    set_fallback_mock_llm(mock_url, _CLAUDE_MOCK_MODEL, f"Okay {nonce}.")
    configure_mock_llm(
        mock_url,
        _scripted_call(
            _CREATE_TOOL,
            f"create_{nonce}",
            {"agent_id": agent_id, "title": "Color helper", "message": created_text},
            created_reply,
        ),
        match=create_marker,
        required_tools=[_CREATE_TOOL],
    )

    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{parent_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)
    _send_until_tools_ready(page, base_url, parent_id, mock_url, f"Okay {nonce}.")

    _send(page, f"Start a helper session and give it a color task. ({create_marker})")
    _wait_reply(page, base_url, parent_id, create_marker, created_reply)
    rows = _agents_rail(page).locator(_SUBAGENT_ROW)
    expect(rows).to_have_count(1, timeout=60_000)
    child_id = rows.first.get_attribute("data-child-session-id")
    assert child_id, "subagent row is missing data-child-session-id"

    configure_mock_llm(
        mock_url,
        _scripted_call(
            _SEND_TOOL, f"send_{nonce}", {"session_id": child_id, "args": sent_text}, sent_reply
        ),
        match=send_marker,
        required_tools=[_SEND_TOOL],
    )
    _send(page, f"Send the helper a follow-up color task. ({send_marker})")
    _wait_reply(page, base_url, parent_id, send_marker, sent_reply)

    parent_title = httpx.get(f"{base_url}/v1/sessions/{parent_id}", timeout=10.0).json()["title"]
    sender = re.compile(
        "|".join(re.escape(name) for name in ("Claude", parent_title) if name), re.IGNORECASE
    )

    _agents_rail(page).locator(_SUBAGENT_ROW).first.click()
    page.wait_for_url(re.compile(re.escape(f"/c/{child_id}")))
    for view in ("on first load", "after reload"):
        if view == "after reload":
            page.reload()
        expect(page.locator(_MESSAGE, has_text=sent_text).first).to_be_visible(timeout=60_000)
        unlabeled = {
            text: rendering
            for text in (created_text, sent_text)
            if (rendering := _unlabeled_rendering(page, text, sender))
        }
        assert not unlabeled, (
            f"{view}: agent-sent messages show no label naming their sender "
            f"({sender.pattern}): {unlabeled}"
        )
