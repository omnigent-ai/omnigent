"""E2E test: ``web_fetch`` backed by Nimble Extract (``fetch_provider: nimble``).

An agent with ``web_fetch`` + ``fetch_provider: nimble`` fetches a URL, and the
runner dispatches to the Nimble Extract path (``_execute_web_fetch_tool`` →
``web_fetch_nimble._fetch_nimble``). A local HTTP stub stands in for Nimble's
``/v1/extract`` API — the runner subprocess is pointed at it via
``OMNIGENT_NIMBLE_EXTRACT_URL`` (set at import time so the session-scoped
server/runner subprocesses inherit it), so no live Nimble key is needed.

Like the repo's other spec-level builtin e2e tests (see
``tests/e2e/test_file_tools.py``), this **requires a real LLM** and skips under
the mock LLM: the mock adapter can't resolve a builtin's callable, so the agent
must run against a real model that decides to call ``web_fetch`` itself.

Usage::

    pytest tests/e2e/test_web_fetch_nimble_e2e.py -v
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
    """Reserve a free localhost port for the Extract stub."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


# Point the (session-scoped) runner subprocess at our local Extract stub. Set at
# import time — before collection finishes and the server/runner fixtures spawn —
# so the child processes inherit the override from os.environ.
_STUB_PORT = _reserve_port()
os.environ["OMNIGENT_NIMBLE_EXTRACT_URL"] = f"http://127.0.0.1:{_STUB_PORT}/v1/extract"

_EXTRACT_MARKDOWN = "# Stubbed Nimble Extract\n\nWEB_FETCH_NIMBLE_E2E page body."
_received_requests: list[dict[str, object]] = []


class _ExtractStubHandler(BaseHTTPRequestHandler):
    """Minimal stand-in for Nimble ``POST /v1/extract``."""

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
                "data": {"markdown": _EXTRACT_MARKDOWN},
                "url": body.get("url", ""),
                "status": "success",
                "task_id": "stub",
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
def extract_stub() -> Iterator[list[dict[str, object]]]:
    """Run the local Extract stub for the duration of the test."""
    _received_requests.clear()
    server = HTTPServer(("127.0.0.1", _STUB_PORT), _ExtractStubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _received_requests
    finally:
        server.shutdown()
        server.server_close()


def test_web_fetch_nimble_extract_happy_path(
    http_client: httpx.Client,
    live_runner_id: str,
    using_mock_llm: bool,
    extract_stub: list[dict[str, object]],
) -> None:
    """
    A real-LLM agent with ``web_fetch`` + ``fetch_provider: nimble`` fetches a URL
    via Nimble Extract and completes.

    Verifies the chain agent → web_fetch → runner dispatch → Nimble Extract
    (stub) → result → completion, and that the request carried the
    ``X-Client-Source`` identity header.
    """
    if using_mock_llm:
        pytest.skip(
            "requires real LLM (spec-level builtin tools don't resolve under the mock LLM)"
        )

    agent_name = register_inline_agent(
        http_client,
        name=f"nimble-fetch-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model="gpt-4.1-mini",
        profile="",
        prompt=(
            "You read web pages. When asked to read a URL, call the web_fetch "
            "tool with that url and report what the page says."
        ),
        extra_config={
            "tools": {
                "builtins": [
                    {
                        "name": "web_fetch",
                        "fetch_provider": "nimble",
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
        content="Use web_fetch to read https://example.com and report what it says.",
    )
    body = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=response_id, timeout=180
    )

    assert body["status"] == "completed", (
        f"Response status is {body['status']!r}, expected 'completed'. "
        f"Output: {body.get('output', [])}"
    )

    # The runner dispatched web_fetch to Nimble Extract (not the sub-agent), and
    # the request carried the X-Client-Source identity header.
    assert extract_stub, "Nimble Extract stub was never called — nimble path not taken."
    last = extract_stub[-1]
    headers = last["headers"]
    assert isinstance(headers, dict)
    assert headers.get("X-Client-Source") == "omnigent", (
        f"Expected X-Client-Source 'omnigent', got {headers.get('X-Client-Source')!r}"
    )
    request_body = last["body"]
    assert isinstance(request_body, dict)
    assert request_body.get("url") == "https://example.com"
