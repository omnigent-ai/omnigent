"""Unit coverage for mock-LLM readiness and captured request inspection."""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests._helpers.mock_llm import captured_tool_result, mock_llm_answers, tool_result_text


def _messages_result(content: object, call_id: str = "call-1", is_error: object = None) -> dict:
    """One captured Messages request whose user turn answers *call_id*."""
    block: dict = {"type": "tool_result", "tool_use_id": call_id, "content": content}
    if is_error is not None:
        block["is_error"] = is_error
    return {"messages": [{"role": "user", "content": [block]}]}


def _responses_result(output: object, call_id: str = "call-1") -> dict:
    """One captured Responses request answering *call_id*."""
    return {"input": [{"type": "function_call_output", "call_id": call_id, "output": output}]}


@pytest.mark.parametrize(
    ("captured_request", "expected"),
    [
        pytest.param(
            _messages_result(
                [{"type": "text", "text": "line one"}, {"type": "text", "text": "line two"}],
                is_error=True,
            ),
            "is_error=True line one\nline two",
            id="messages-error-text-blocks",
        ),
        pytest.param(
            _messages_result('{"conversation_id": "conv_1"}'),
            '{"conversation_id": "conv_1"}',
            id="messages-string-content",
        ),
        pytest.param(
            _responses_result('{"conversation_id": "conv_2"}'),
            '{"conversation_id": "conv_2"}',
            id="responses-function-output",
        ),
    ],
)
def test_captured_tool_result_returns_the_payload_text(
    captured_request: dict, expected: str
) -> None:
    assert captured_tool_result([captured_request], "call-1") == expected


def test_captured_tool_result_newest_match_wins() -> None:
    requests = [
        _messages_result("first attempt"),
        _responses_result("second attempt"),
        _messages_result('{"conversation_id": "newest"}', is_error=False),
    ]
    assert captured_tool_result(requests, "call-1") == '{"conversation_id": "newest"}'


def test_captured_tool_result_reports_a_missing_call_id() -> None:
    assert (
        captured_tool_result([_messages_result("other", call_id="call-2")], "call-1")
        == "no captured tool result for 'call-1'"
    )


def test_captured_tool_result_keeps_a_long_payload_whole() -> None:
    payload = '{"detail": "' + "x" * 2000 + '"}'
    result = captured_tool_result([_responses_result(payload)], "call-1")
    assert result == payload
    assert len(result) > 2000


@pytest.mark.parametrize(
    ("payload", "error", "expected"),
    [
        pytest.param("text", None, "text", id="string"),
        pytest.param("text", True, "is_error=True text", id="string-error"),
        pytest.param(
            [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
            False,
            "a\nb",
            id="blocks-no-error",
        ),
        pytest.param(None, None, "", id="empty"),
    ],
)
def test_tool_result_text_formats_payload_and_error_flag(
    payload: object, error: object, expected: str
) -> None:
    assert tool_result_text(payload, error=error) == expected


class _StatsHandler(BaseHTTPRequestHandler):
    """Answers ``/stats`` 200 and other paths 404, or drops the connection while *booting*."""

    booting = False

    def do_GET(self) -> None:
        if type(self).booting:
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        if self.path != "/stats":
            self.send_error(404)
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *_args: object) -> None:
        """Keep the test output quiet."""


@pytest.fixture
def stats_server() -> Iterator[tuple[str, type[_StatsHandler]]]:
    """A local ``/stats`` endpoint whose handler class sets whether it's still booting."""
    handler = type("StatsHandler", (_StatsHandler,), {"booting": False})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", handler
    finally:
        server.shutdown()
        server.server_close()


def test_mock_llm_answers_once_stats_replies(stats_server) -> None:
    base_url, _handler = stats_server
    assert mock_llm_answers(base_url) is True


def test_mock_llm_answers_not_yet_when_the_server_drops_the_request(stats_server) -> None:
    base_url, handler = stats_server
    handler.booting = True
    assert mock_llm_answers(base_url) is False


def test_mock_llm_answers_not_yet_when_nothing_listens() -> None:
    # Held bound but not listening, so no other process can take the port meanwhile.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
        assert mock_llm_answers(f"http://127.0.0.1:{port}") is False
