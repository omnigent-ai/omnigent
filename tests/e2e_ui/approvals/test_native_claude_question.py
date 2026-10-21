"""Real Claude questions round-trip through chat, including hook timeouts.

Only model replies and the timed-out permission HTTP response are scripted.
Claude's tool, command hook, transcript forwarding, and the browser run live.
"""

from __future__ import annotations

import contextlib
import json
import threading
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent.harnesses.claude_native import bridge
from tests._helpers.polling import poll_until
from tests.e2e._native_resume_helpers import wait_for_terminal_ready
from tests.e2e.conftest import get_mock_requests
from tests.e2e_ui.conftest import configure_mock_llm, reset_mock_llm
from tests.e2e_ui.messages.test_native_claude_render_parity import (
    _ensure_chat_view,
    _open_terminal_view,
    _type_into_tui,
    _wait_terminal_connected,
)


@contextlib.contextmanager
def _permission_timeout(session_id: str) -> Iterator[list[dict]]:
    """Give this session's real hook the server's empty-200 timeout response."""
    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    config_path = bridge.bridge_dir_for_conversation_id(session_id) / "permission_hook.json"
    original = config_path.read_bytes()
    config = json.loads(original)
    config["ap_server_url"] = f"http://127.0.0.1:{server.server_port}"
    thread.start()
    try:
        config_path.write_text(json.dumps(config))
        yield received
    finally:
        config_path.write_bytes(original)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.nightly
@pytest.mark.timeout(180)
@pytest.mark.parametrize("input_surface", ["chat", "terminal"])
@pytest.mark.parametrize("hook_result", ["answer", "timeout"])
def test_native_question_reaches_chat_and_returns_to_claude(
    page: Page,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
    input_surface: str,
    hook_result: str,
) -> None:
    """A web answer or timeout hint reaches the real CLI's next model request."""
    base_url, session_id = native_claude_mock_session
    marker = uuid.uuid4().hex[:12]
    call_id = f"toolu_question_{marker}"
    question = f"Which option do you prefer ({marker})?"
    fallback_question = f"{question} Alpha or Bravo?"
    completion = f"Answer received ({marker})."
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": call_id,
                        "name": "AskUserQuestion",
                        "arguments": json.dumps(
                            {
                                "questions": [
                                    {
                                        "header": "Choice",
                                        "question": question,
                                        "multiSelect": False,
                                        "options": [
                                            {"label": "Alpha", "description": "First option"},
                                            {"label": "Bravo", "description": "Second option"},
                                        ],
                                    }
                                ]
                            }
                        ),
                    }
                ]
            },
            {"text": completion if hook_result == "answer" else fallback_question},
        ],
        match=marker,
        required_tools=["AskUserQuestion"],
    )
    with httpx.Client(base_url=base_url, timeout=10) as client:
        wait_for_terminal_ready(client, conversation_id=session_id, harness="claude", timeout=90)
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    timeout_scope = (
        _permission_timeout(session_id) if hook_result == "timeout" else contextlib.nullcontext([])
    )
    with timeout_scope as received:
        prompt = f"Use AskUserQuestion to ask me to choose Alpha or Bravo ({marker})."
        if input_surface == "terminal":
            _type_into_tui(page, prompt)
            _ensure_chat_view(page)
        else:
            _ensure_chat_view(page)
            composer = page.get_by_role("textbox", name="Message the agent")
            composer.fill(prompt)
            page.get_by_role("button", name="Send", exact=True).click()

        if hook_result == "answer":
            form = page.get_by_test_id("ask-user-question-form")
            expect(form).to_be_visible(timeout=60_000)
            expect(form).to_contain_text(question)
            pending = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10).json()
            assert pending["pending_elicitations"]
            form.get_by_role("radio", name="Alpha").check()
            form.get_by_test_id("ask-user-question-submit").click()

        expected = completion if hook_result == "answer" else fallback_question
        expect(page.get_by_text(expected, exact=True)).to_be_visible(timeout=60_000)

        def tool_results() -> list[dict]:
            return [
                block
                for request in get_mock_requests(mock_llm_server_url)
                for message in request.get("messages", [])
                if isinstance(message.get("content"), list)
                for block in message["content"]
                if block.get("type") == "tool_result" and block.get("tool_use_id") == call_id
            ]

        poll_until(tool_results)
        results = tool_results()
        if hook_result == "answer":
            assert all(not result.get("is_error") for result in results)
            assert "Alpha" in json.dumps(results)
        else:
            assert received and received[0]["tool_name"] == "AskUserQuestion"
            assert all(result.get("is_error") for result in results)
            assert "normal chat message" in json.dumps(results)
        snapshot = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10).json()
        assert not snapshot.get("pending_elicitations")
