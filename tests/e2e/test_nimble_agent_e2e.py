"""E2E test: the ``nimble_agent`` builtin (Nimble WSA → structured JSON).

A real-LLM agent with the ``nimble_agent`` builtin runs a Web Search Agent, and
the runner dispatches to Nimble's ``/v1/agent`` endpoint (stubbed locally via
``OMNIGENT_NIMBLE_AGENT_URL``, set at import time so the session-scoped
server/runner subprocesses inherit it — no live Nimble key needed).

Like the repo's other spec-level builtin e2e tests (see
``tests/e2e/test_file_tools.py``), this **requires a real LLM** and skips under
the mock LLM.

Usage::

    pytest tests/e2e/test_nimble_agent_e2e.py -v
"""

from __future__ import annotations

import json
import os
import socket
import threading
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from tests.e2e.conftest import (
    create_runner_bound_session,
    poll_session_until_terminal,
    register_inline_agent,
    send_user_message_to_session,
)


def _reserve_port() -> int:
    """Reserve a free localhost port for the WSA stub."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


_STUB_PORT = _reserve_port()
os.environ["OMNIGENT_NIMBLE_AGENT_URL"] = f"http://127.0.0.1:{_STUB_PORT}/v1/agent"

_received_requests: list[dict[str, object]] = []


class _AgentStubHandler(BaseHTTPRequestHandler):
    """Minimal stand-in for Nimble ``POST /v1/agent``."""

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {}
        _received_requests.append({"headers": dict(self.headers), "body": body})

        payload = json.dumps(
            {
                "data": {
                    "parsing": {
                        "entities": {
                            "OrganicResult": [
                                {"title": "Nimble", "url": "https://nimbleway.com", "position": 1}
                            ]
                        }
                    }
                },
                "status": "success",
                "task_id": "stub",
                "url": "https://www.google.com/search",
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:  # keep test output quiet
        del args


@pytest.fixture
def agent_stub() -> Iterator[list[dict[str, object]]]:
    """Run the local WSA stub for the duration of the test."""
    _received_requests.clear()
    server = HTTPServer(("127.0.0.1", _STUB_PORT), _AgentStubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _received_requests
    finally:
        server.shutdown()
        server.server_close()


def test_nimble_agent_happy_path(
    http_client: httpx.Client,
    live_runner_id: str,
    using_mock_llm: bool,
    agent_stub: list[dict[str, object]],
) -> None:
    """
    A real-LLM agent with ``nimble_agent`` runs a WSA query and completes.

    Verifies agent → nimble_agent → runner dispatch → Nimble WSA (stub) → result
    → completion, and that the request carried the ``X-Client-Source`` header.
    """
    if using_mock_llm:
        pytest.skip(
            "requires real LLM (spec-level builtin tools don't resolve under the mock LLM)"
        )

    agent_name = register_inline_agent(
        http_client,
        name=f"nimble-agent-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model="gpt-4.1-mini",
        profile="",
        prompt=(
            "You get structured search results. When asked to search, call the "
            "nimble_agent tool with the query and report the results."
        ),
        extra_config={
            "tools": {
                "builtins": [
                    {
                        "name": "nimble_agent",
                        "api_key": "test-key",
                    },
                ],
            },
        },
    )

    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Use nimble_agent to search for 'nimbleway' and report the top result.",
    )
    body = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=response_id, timeout=180
    )

    assert body["status"] == "completed", (
        f"Response status is {body['status']!r}, expected 'completed'. "
        f"Output: {body.get('output', [])}"
    )

    assert agent_stub, "Nimble WSA stub was never called — nimble_agent not dispatched."
    last = agent_stub[-1]
    headers = last["headers"]
    assert isinstance(headers, dict)
    assert headers.get("X-Client-Source") == "omnigent", (
        f"Expected X-Client-Source 'omnigent', got {headers.get('X-Client-Source')!r}"
    )
    request_body = last["body"]
    assert isinstance(request_body, dict)
    assert request_body.get("agent") == "google_search"
