"""Strict gateway double: Gemini requires MLflow and signed tool history.

Calls reuse the function name as their ID; file results drive the next read.
Requests are journaled. Error envelopes are inferred from the reported failure.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

CODEX_CHAT_ERROR = "Request doesn't match any known API type for this gateway route"
CODEX_RESPONSES_ERROR = "Responses API passthrough is not supported for model {model}"
MISSING_SIGNATURE_ERROR = "Function call is missing a thought_signature in functionCall parts"
LOOP_GUARD_ERROR = "mock loop guard: {count} consecutive identical tool calls"

_CODEX_CHAT = "/ai-gateway/codex/v1/chat/completions"
_CODEX_RESPONSES = "/ai-gateway/codex/v1/responses"
_MLFLOW_CHAT = "/ai-gateway/mlflow/v1/chat/completions"

# Identical consecutive tool calls tolerated before the mock ends the turn.
LOOP_GUARD = 40


@dataclass
class GeminiGatewayMock:
    """Read ordered path-to-marker files, requiring signed tool history."""

    files: dict[str, str]
    journal: list[dict[str, Any]] = field(default_factory=list)
    _server: ThreadingHTTPServer | None = None
    _thread: threading.Thread | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _seq: int = 0
    _decisions: list[str] = field(default_factory=list)

    @property
    def origin(self) -> str:
        assert self._server is not None, "mock not started"
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> str:
        mock = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: object) -> None:
                return

            def do_GET(self) -> None:
                self._reply(404, {"error_code": "ENDPOINT_NOT_FOUND", "message": self.path})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    body = {"raw": raw.decode(errors="replace")}
                status, payload, note, stream = mock._handle(self.path, body)
                mock._record(self.path, body, status, note)
                if stream is not None:
                    data = stream.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self._reply(status, payload)

            def _reply(self, status: int, payload: dict[str, Any]) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.origin

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def dump(self, path: str) -> None:
        # Handler threads may still be appending (loop/timeout outcomes).
        with self._lock:
            snapshot = list(self.journal)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, indent=2, default=str)

    def decisions(self) -> list[str]:
        with self._lock:
            return list(self._decisions)

    def _record(self, path: str, body: Any, status: int, note: str) -> None:
        with self._lock:
            self._seq += 1
            self.journal.append(
                {
                    "seq": self._seq,
                    "time": time.time(),
                    "path": path,
                    "status": status,
                    "note": note,
                    "body": body,
                }
            )

    def _handle(
        self, path: str, body: dict[str, Any]
    ) -> tuple[int, dict[str, Any], str, str | None]:
        route = path.split("?", 1)[0]
        model = str(body.get("model") or "databricks-gemini-3-8-flash")
        if route == _CODEX_CHAT:
            return 400, _error(CODEX_CHAT_ERROR), "codex surface rejects chat/completions", None
        if route == _CODEX_RESPONSES:
            return (
                400,
                _error(CODEX_RESPONSES_ERROR.format(model=model)),
                "codex surface rejects Responses passthrough",
                None,
            )
        if route != _MLFLOW_CHAT:
            return (
                404,
                {"error_code": "ENDPOINT_NOT_FOUND", "message": route},
                "unknown route",
                None,
            )
        return self._gemini_chat(body, model)

    def _gemini_chat(
        self, body: dict[str, Any], model: str
    ) -> tuple[int, dict[str, Any], str, str | None]:
        messages = body.get("messages") or []
        for message in messages:
            if message.get("role") != "assistant":
                continue
            for call in message.get("tool_calls") or []:
                if not call.get("thoughtSignature"):
                    return (
                        400,
                        _error(MISSING_SIGNATURE_ERROR),
                        "missing thoughtSignature",
                        None,
                    )
        tools = {(tool.get("function") or {}).get("name") for tool in body.get("tools") or []}
        if "sys_os_read" not in tools:
            return (
                500,
                _error(f"mock setup: tool 'sys_os_read' not advertised; got {sorted(tools)}"),
                "tool not advertised",
                None,
            )
        seen = " ".join(_tool_result_text(m) for m in messages if m.get("role") == "tool")
        pending = [path for path, marker in self.files.items() if marker not in seen]
        with self._lock:
            request_no = len(self._decisions) + 1
            decision = pending[0] if pending else "<answer>"
            self._decisions.append(decision)
            tail = self._decisions[-LOOP_GUARD:]
        if len(tail) == LOOP_GUARD and len(set(tail)) == 1 and decision != "<answer>":
            return 400, _error(LOOP_GUARD_ERROR.format(count=LOOP_GUARD)), "loop guard", None

        stream = bool(body.get("stream"))
        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        completion_id = f"chatcmpl-mock-{request_no}"
        if pending:
            tool_call = {
                "id": "sys_os_read",
                "type": "function",
                "function": {
                    "name": "sys_os_read",
                    "arguments": json.dumps({"path": pending[0]}),
                },
                "thoughtSignature": f"thought-signature-{request_no}",
            }
            note = f"tool call sys_os_read({pending[0]})"
            if stream:
                return (
                    200,
                    {},
                    note,
                    _sse(
                        completion_id,
                        model,
                        [
                            {"role": "assistant", "content": None},
                            {"tool_calls": [{"index": 0, **tool_call}]},
                        ],
                        "tool_calls",
                        include_usage,
                    ),
                )
            message: dict[str, Any] = {
                "role": "assistant",
                "content": None,
                "tool_calls": [tool_call],
            }
            return 200, _completion(completion_id, model, message, "tool_calls"), note, None

        answer = "; ".join(f"{path} says {marker}" for path, marker in self.files.items())
        if stream:
            return (
                200,
                {},
                "final answer",
                _sse(
                    completion_id,
                    model,
                    [{"role": "assistant", "content": answer}],
                    "stop",
                    include_usage,
                ),
            )
        return (
            200,
            _completion(completion_id, model, {"role": "assistant", "content": answer}, "stop"),
            "final answer",
            None,
        )


def write_gateway_home(home: Path, profile: str, origin: str) -> None:
    """Seed one scratch Databricks profile and ucode workspace on the codex surface."""
    home.mkdir(parents=True, exist_ok=True)
    (home / ".databrickscfg").write_text(
        f"[{profile}]\nhost = {origin}\ntoken = mock-pat-{profile}\n", encoding="utf-8"
    )
    ucode_dir = home / ".ucode"
    ucode_dir.mkdir(exist_ok=True)
    state = {
        "state_version": 1,
        "current_workspace": origin,
        "workspaces": {
            origin: {
                "workspace": origin,
                "codex_models": ["databricks-gpt-5-5"],
                "base_urls": {"codex": f"{origin}/ai-gateway/codex/v1"},
                "available_tools": ["codex"],
                "agents": {
                    "codex": {
                        "model": "databricks-gpt-5-5",
                        "base_url": f"{origin}/ai-gateway/codex/v1",
                        "auth_command": "printf omni-mock-token",
                    }
                },
            }
        },
    }
    (ucode_dir / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")


def _error(message: str) -> dict[str, Any]:
    return {"error_code": "BAD_REQUEST", "message": message}


def _tool_result_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return ""


def _usage() -> dict[str, int]:
    return {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}


def _completion(
    completion_id: str, model: str, message: dict[str, Any], finish_reason: str
) -> dict[str, Any]:
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": _usage(),
    }


def _sse(
    completion_id: str,
    model: str,
    deltas: list[dict[str, Any]],
    finish_reason: str,
    include_usage: bool,
) -> str:
    def chunk(delta: dict[str, Any], finish: str | None) -> dict[str, Any]:
        return {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    events = [chunk(delta, None) for delta in deltas]
    events.append(chunk({}, finish_reason))
    if include_usage:
        events.append(
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [],
                "usage": _usage(),
            }
        )
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
