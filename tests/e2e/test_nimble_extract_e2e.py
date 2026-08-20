"""E2E test: the ``nimble_extract`` builtin (Nimble Extract Templates).

A real-LLM agent with the ``nimble_extract`` builtin runs an extract template,
and the runner dispatches to ``POST /v2/extract/templates/run`` (stubbed
locally via ``OMNIGENT_NIMBLE_EXTRACT_BASE_URL``, set at import time so the
session-scoped server/runner subprocesses inherit it — no live Nimble key
needed).

Like the repo's other spec-level builtin e2e tests (see
``tests/e2e/test_file_tools.py``), this **requires a real LLM** and skips under
the mock LLM.

Usage::

    pytest tests/e2e/test_nimble_extract_e2e.py -v
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
    """Reserve a free localhost port for the Extract Templates stub.

    There is a small close-to-rebind window; acceptable for a
    single-process opt-in e2e run.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


_STUB_PORT = _reserve_port()
os.environ["OMNIGENT_NIMBLE_EXTRACT_BASE_URL"] = f"http://127.0.0.1:{_STUB_PORT}"

_SENTINEL_URL = "https://e2e-extract.nimbleway.com/result-1"

_received_requests: list[dict[str, object]] = []


class _ExtractStubHandler(BaseHTTPRequestHandler):
    """Minimal stand-in for ``POST /v2/extract/templates/run``."""

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {}
        _received_requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
        payload = json.dumps(
            {
                "task_id": "task_e2e_extract",
                "url": "https://www.google.com/search",
                "status": "success",
                "data": {
                    "parsing": {
                        "status": "success",
                        "entities": {
                            "OrganicResult": [
                                {"title": "Nimble", "url": _SENTINEL_URL, "position": 1}
                            ]
                        },
                    }
                },
                "metadata": {"agent": "e2e_serp_snapshot"},
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
    """Run the local Extract Templates stub for the duration of the test."""
    _received_requests.clear()
    server = HTTPServer(("127.0.0.1", _STUB_PORT), _ExtractStubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _received_requests
    finally:
        server.shutdown()
        server.server_close()


def test_nimble_extract_happy_path(
    http_client: httpx.Client,
    live_runner_id: str,
    using_mock_llm: bool,
    extract_stub: list[dict[str, object]],
) -> None:
    """
    A real-LLM agent with ``nimble_extract`` runs a template and completes.

    Verifies agent → nimble_extract → runner dispatch → extract run (stub) →
    structured result → completion, and that the request carried the
    ``X-Client-Source`` header, the template, and the params.
    """
    if using_mock_llm:
        pytest.skip(
            "requires real LLM (spec-level builtin tools don't resolve under the mock LLM)"
        )

    agent_name = register_inline_agent(
        http_client,
        name=f"nimble-extract-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model="gpt-4.1-mini",
        profile="",
        prompt=(
            "You get structured search results. When asked to search, call the "
            'nimble_extract tool with params {"query": "<the query>"} and '
            "report the top result's URL."
        ),
        extra_config={
            "tools": {
                "builtins": [
                    {
                        "name": "nimble_extract",
                        "api_key": "test-key",
                        "template": "e2e_serp_snapshot",
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
        content="Use nimble_extract to search for 'nimbleway' and report the top result URL.",
    )
    body = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=response_id, timeout=180
    )

    assert body["status"] == "completed", (
        f"Response status is {body['status']!r}, expected 'completed'. "
        f"Output: {body.get('output', [])}"
    )

    assert extract_stub, "Extract stub was never called — nimble_extract not dispatched."
    last = extract_stub[-1]
    assert last["path"] == "/v2/extract/templates/run"
    headers = last["headers"]
    assert isinstance(headers, dict)
    assert headers.get("X-Client-Source") == "omnigent", (
        f"Expected X-Client-Source 'omnigent', got {headers.get('X-Client-Source')!r}"
    )
    assert headers.get("Authorization") == "Bearer test-key"
    request_body = last["body"]
    assert isinstance(request_body, dict)
    assert request_body.get("template") == "e2e_serp_snapshot"
    assert "nimbleway" in json.dumps(request_body.get("params", {})).lower()

    assert _SENTINEL_URL in json.dumps(body.get("output", [])), (
        "the stubbed structured result never reached the model's reply"
    )
