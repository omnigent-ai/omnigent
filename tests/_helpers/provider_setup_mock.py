"""Loopback OpenAI-compatible provider for provider-settings end-to-end tests.

The fixture starts this server twice with distinct ports, labels, and request
ledgers. A test can save endpoint A as the initial provider, save endpoint B as
its replacement, and prove which provider a real runner used without opening
an external network connection.

Both OpenAI protocols used by Omnigent are implemented:

* native Codex and Responses-mode openai-agents append ``/responses``;
* Chat-mode openai-agents and the generic adapter append
  ``/chat/completions``.

Only request method, path, and JSON are recorded.  Authorization headers are
intentionally excluded from the ledger.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


class _SingleProviderServer(ThreadingHTTPServer):
    """Server state for :func:`serve`."""

    def __init__(self, port: int, log_path: Path, label: str) -> None:
        self.log_path = log_path
        self.label = label
        self._log_lock = threading.Lock()
        self._request_count = 0
        self.release_response = threading.Event()
        self.release_response.set()
        self.waiting = threading.Event()
        super().__init__(("127.0.0.1", port), _single_provider_handler_class())

    def record(self, method: str, path: str, body: object | None) -> int:
        """Append one sanitized JSONL request and return its sequence number."""
        record = {"label": self.label, "method": method, "path": path, "json": body}
        line = json.dumps(record, separators=(",", ":")) + "\n"
        with self._log_lock:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as log:
                log.write(line)
            self._request_count += 1
            return self._request_count


def serve(port: int, log_path: Path, label: str) -> None:
    """Serve one blocking loopback provider for a subprocess-backed test.

    The configured provider base URL is ``http://127.0.0.1:<port>/v1``.  The
    server records request JSON, but never headers, in *log_path* as JSONL.

    :param port: Explicit loopback port selected by the parent fixture.
    :param log_path: Dedicated request ledger for this provider process.
    :param label: Stable provider marker, usually ``"A"`` or ``"B"``.
    """
    if not 0 <= port <= 65535:
        raise ValueError(f"invalid TCP port: {port}")
    if not label:
        raise ValueError("label must be non-empty")
    server = _SingleProviderServer(port, Path(log_path), label)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _single_provider_handler_class() -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server: _SingleProviderServer

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            self.server.record("GET", self.path, None)
            if path == "/control/status":
                self._send_json(200, {"waiting": self.server.waiting.is_set()})
                return
            if path == "/health":
                self._send_json(200, {"ok": True, "label": self.server.label})
                return
            if path == "/v1/models":
                self._send_json(
                    200,
                    {
                        "object": "list",
                        "data": [
                            {
                                "id": f"mock-provider-{self.server.label.lower()}",
                                "object": "model",
                                "created": 0,
                                "owned_by": "omnigent-test",
                            }
                        ],
                    },
                )
                return
            self._send_json(404, {"error": {"message": "unknown mock provider path"}})

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            parsed = _parse_json(body)
            sequence = self.server.record("POST", self.path, parsed)
            path = self.path.split("?", 1)[0]
            if path == "/control/pause":
                self.server.release_response.clear()
                self._send_json(200, {"paused": True})
                return
            if path == "/control/release":
                self.server.release_response.set()
                self._send_json(200, {"paused": False})
                return
            if (
                path in {"/v1/responses", "/v1/chat/completions"}
                and not self.server.release_response.is_set()
            ):
                self.server.waiting.set()
                if not self.server.release_response.wait(120):
                    self._send_json(504, {"error": {"message": "fixture response gate timed out"}})
                    return
                self.server.waiting.clear()
            model = _request_model(parsed, self.server.label.lower())
            text = _response_marker(self.server.label)
            if path == "/v1/responses":
                response = _responses_body(text, model=model, sequence=sequence)
                if isinstance(parsed, dict) and parsed.get("stream") is True:
                    self._send_sse(_responses_sse(response))
                else:
                    self._send_json(200, response)
                return
            if path == "/v1/chat/completions":
                if isinstance(parsed, dict) and parsed.get("stream") is True:
                    self._send_sse(_chat_sse(text, model=model, sequence=sequence))
                else:
                    self._send_json(200, _chat_body(text, model=model, sequence=sequence))
                return
            self._send_json(404, {"error": {"message": "unknown mock provider path"}})

        def _send_json(self, status: int, payload: object) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def _send_sse(self, body: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()

        def log_message(self, _format: str, *args: object) -> None:
            del args

    return Handler


def _parse_json(body: bytes) -> object:
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _request_model(request: object, endpoint: str) -> str:
    if isinstance(request, dict):
        model = request.get("model")
        if isinstance(model, str) and model:
            return model
    return f"mock-provider-{endpoint}"


def _response_marker(label: str) -> str:
    """Return a stable A/B marker from labels such as ``A`` or ``mock-a``."""
    suffix = label.rsplit("-", 1)[-1]
    normalized = "".join(character if character.isalnum() else "_" for character in suffix)
    return f"MOCK_PROVIDER_{normalized.upper()}_RESPONSE"


def _responses_body(text: str, *, model: str, sequence: int) -> dict[str, Any]:
    response_id = f"resp_mock_{sequence}"
    message = {
        "id": f"msg_mock_{sequence}",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [
            {
                "type": "output_text",
                "text": text,
                "annotations": [],
            }
        ],
    }
    return {
        "id": response_id,
        "object": "response",
        "created_at": 0,
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "max_output_tokens": None,
        "model": model,
        "output": [message],
        "parallel_tool_calls": True,
        "previous_response_id": None,
        "reasoning": {"effort": None, "summary": None},
        "store": False,
        "temperature": None,
        "text": {"format": {"type": "text"}},
        "tool_choice": "auto",
        "tools": [],
        "top_p": None,
        "truncation": "disabled",
        "usage": {
            "input_tokens": 1,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 1,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 2,
        },
        "user": None,
        "metadata": {},
    }


def _responses_sse(response: dict[str, Any]) -> bytes:
    completed_message = response["output"][0]
    partial_message = {**completed_message, "status": "in_progress", "content": []}
    in_progress = {**response, "status": "in_progress", "output": [], "usage": None}
    text = completed_message["content"][0]["text"]
    events = [
        ("response.created", {"response": in_progress}),
        (
            "response.output_item.added",
            {"output_index": 0, "item": partial_message},
        ),
        (
            "response.content_part.added",
            {
                "item_id": completed_message["id"],
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            },
        ),
        (
            "response.output_text.delta",
            {
                "item_id": completed_message["id"],
                "output_index": 0,
                "content_index": 0,
                "delta": text,
                "logprobs": [],
            },
        ),
        (
            "response.output_text.done",
            {
                "item_id": completed_message["id"],
                "output_index": 0,
                "content_index": 0,
                "text": text,
                "logprobs": [],
            },
        ),
        (
            "response.content_part.done",
            {
                "item_id": completed_message["id"],
                "output_index": 0,
                "content_index": 0,
                "part": completed_message["content"][0],
            },
        ),
        (
            "response.output_item.done",
            {"output_index": 0, "item": completed_message},
        ),
        ("response.completed", {"response": response}),
    ]
    return _sse_events(events)


def _chat_body(text: str, *, model: str, sequence: int) -> dict[str, Any]:
    return {
        "id": f"chatcmpl_mock_{sequence}",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _chat_sse(text: str, *, model: str, sequence: int) -> bytes:
    chunk_id = f"chatcmpl_mock_{sequence}"
    chunks = [
        {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": 0,
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        },
        {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": 0,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
        },
        {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": 0,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
        {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": 0,
            "model": model,
            "choices": [],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    ]
    body = b"".join(
        f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n".encode() for chunk in chunks
    )
    return body + b"data: [DONE]\n\n"


def _sse_events(events: list[tuple[str, dict[str, Any]]]) -> bytes:
    frames: list[bytes] = []
    for sequence, (event_type, payload) in enumerate(events):
        data = {"type": event_type, "sequence_number": sequence, **payload}
        frames.append(
            f"event: {event_type}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()
        )
    return b"".join(frames)
