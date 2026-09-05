"""E2E test: the ``nimble_research`` builtin (Nimble Agent API v2 lifecycle).

A real-LLM agent with the ``nimble_research`` builtin runs a research task, and
the runner dispatches the full v2 lifecycle — start run, poll to terminal,
fetch result — against a local Agent API stub (pointed at via
``OMNIGENT_NIMBLE_RESEARCH_BASE_URL``, set at import time so the session-scoped
server/runner subprocesses inherit it — no live Nimble key needed).

Like the repo's other spec-level builtin e2e tests (see
``tests/e2e/test_file_tools.py``), this **requires a real LLM** and skips under
the mock LLM.

Usage::

    pytest tests/e2e/test_nimble_research_e2e.py -v
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
    """Reserve a free localhost port for the Agent API stub.

    There is a small close-to-rebind window; acceptable for a
    single-process opt-in e2e run.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


_STUB_PORT = _reserve_port()
os.environ["OMNIGENT_NIMBLE_RESEARCH_BASE_URL"] = f"http://127.0.0.1:{_STUB_PORT}"

_E2E_AGENT_ID = "wsa_e2e00000-0000-4000-8000-000000000000"
_E2E_RUN_ID = "task_run_e2e00000-0000-4000-8000-000000000000"
_SENTINEL = "[e2e-v2-answer] Nimble's Agent API v2 lifecycle works."

_received_requests: list[dict[str, object]] = []
_poll_count = {"n": 0}


def _task_run(status: str) -> dict[str, object]:
    """A TaskRun body for the stubbed run."""
    return {
        "id": _E2E_RUN_ID,
        "interaction_id": "int_e2e",
        "status": status,
        "is_active": status in ("queued", "running"),
        "effort": "low",
        "created_at": "2026-07-22T00:00:00Z",
        "web_search_agent_id": _E2E_AGENT_ID,
    }


class _AgentV2StubHandler(BaseHTTPRequestHandler):
    """Minimal stand-in for the Agent API v2 start/poll/result endpoints."""

    def _record(self, kind: str, body: dict[str, object]) -> None:
        _received_requests.append(
            {"kind": kind, "path": self.path, "headers": dict(self.headers), "body": body}
        )

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {}
        self._record("create", body)
        self._send_json(202, _task_run("queued"))

    def do_GET(self) -> None:
        if self.path.endswith("/result"):
            self._record("result", {})
            self._send_json(
                200,
                {
                    "run": _task_run("completed"),
                    "output": {
                        "type": "text",
                        "content": _SENTINEL,
                        "trust": {
                            "confidence": "high",
                            "reasoning": "Primary source confirmed.",
                            "sources": [
                                {
                                    "url": "https://nimbleway.com",
                                    "type": "primary",
                                    "title": "Nimble",
                                }
                            ],
                            "claims": [
                                {
                                    "callout": 1,
                                    "confidence": "high",
                                    "reasoning": "Stated directly.",
                                    "citations": [{"url": "https://nimbleway.com"}],
                                }
                            ],
                        },
                    },
                },
            )
            return
        self._record("poll", {})
        _poll_count["n"] += 1
        status = "running" if _poll_count["n"] == 1 else "completed"
        self._send_json(200, _task_run(status))

    def log_message(self, *args: object) -> None:  # keep test output quiet
        del args


@pytest.fixture
def agent_v2_stub() -> Iterator[list[dict[str, object]]]:
    """Run the local Agent API v2 stub for the duration of the test."""
    _received_requests.clear()
    _poll_count["n"] = 0
    server = HTTPServer(("127.0.0.1", _STUB_PORT), _AgentV2StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _received_requests
    finally:
        server.shutdown()
        server.server_close()


def test_nimble_research_v2_happy_path(
    http_client: httpx.Client,
    live_runner_id: str,
    using_mock_llm: bool,
    agent_v2_stub: list[dict[str, object]],
) -> None:
    """
    A real-LLM agent with ``nimble_research`` runs the full v2 lifecycle and
    answers from the stubbed, cited result.

    Verifies agent → nimble_research → runner dispatch → start → poll → result
    (all against the stub) → completion, and that every request carried the
    ``X-Client-Source`` header.
    """
    if using_mock_llm:
        pytest.skip(
            "requires real LLM (spec-level builtin tools don't resolve under the mock LLM)"
        )

    agent_name = register_inline_agent(
        http_client,
        name=f"nimble-research-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model="gpt-4.1-mini",
        profile="",
        prompt=(
            "You have a nimble_research research tool. When asked to research "
            "something, call nimble_research with the task and report the answer "
            "it returns."
        ),
        extra_config={
            "tools": {
                "builtins": [
                    {
                        "name": "nimble_research",
                        "api_key": "test-key",
                        "agent_id": _E2E_AGENT_ID,
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
        content="Use nimble_research to research Nimbleway and report the answer.",
    )
    body = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=response_id, timeout=180
    )

    assert body["status"] == "completed", (
        f"Response status is {body['status']!r}, expected 'completed'. "
        f"Output: {body.get('output', [])}"
    )

    kinds = [entry["kind"] for entry in agent_v2_stub]
    assert "create" in kinds, "the run was never started against the stub"
    assert "poll" in kinds, "the run was never polled to a terminal status"
    assert "result" in kinds, "the completed run's result was never fetched"

    for entry in agent_v2_stub:
        headers = entry["headers"]
        assert isinstance(headers, dict)
        assert headers.get("X-Client-Source") == "omnigent", (
            f"{entry['kind']} request missing X-Client-Source: {headers.get('X-Client-Source')!r}"
        )
        assert headers.get("Authorization") == "Bearer test-key"

    create_body = next(e["body"] for e in agent_v2_stub if e["kind"] == "create")
    assert isinstance(create_body, dict)
    assert "nimbleway" in str(create_body.get("input", "")).lower(), (
        f"run input did not carry the task: {create_body!r}"
    )

    assert _SENTINEL.split()[0] in json.dumps(body.get("output", [])), (
        "the stubbed answer never reached the model's reply"
    )
