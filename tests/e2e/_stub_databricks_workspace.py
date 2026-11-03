"""Loopback stand-in for a Databricks workspace's two Anthropic routes.

``/serving-endpoints/anthropic`` serves the model (200) while ``/ai-gateway/anthropic``
answers the AI Gateway's 403. Every request is journaled so a test can assert which
route the Claude CLI reached. Host-metadata and OIDC discovery are served so the
Databricks CLI treats the host as OAuth-capable and reads its token cache; anything
else answers 404. Standalone: ``python -m tests.e2e._stub_databricks_workspace --port 48123``.
"""

from __future__ import annotations

import argparse
import json
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from tests.server.integration.mock_llm_server import anthropic_sse_text_response

SERVING_ENDPOINTS_PATH = "/serving-endpoints/anthropic"
AI_GATEWAY_PATH = "/ai-gateway/anthropic"
OIDC_DISCOVERY_PATH = "/oidc/.well-known/oauth-authorization-server"
HOST_METADATA_PATH = "/.well-known/databricks-config"
REPLY_TEXT = "STUB_SERVING_ENDPOINT_REPLY"


def unavailable_message(model: str) -> str:
    """The AI Gateway's rejection for a *model* it does not serve."""
    return f"'{model}' is no longer available. Use Unity Catalog model services."


@dataclass(frozen=True)
class SeenRequest:
    method: str
    path: str
    authorization: str | None
    model: str | None

    def journal_entry(self) -> dict[str, object]:
        """The on-disk journal form; the bearer value itself is never written."""
        return {
            "method": self.method,
            "path": self.path,
            "has_authorization": self.authorization is not None,
            "model": self.model,
        }


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen: list[SeenRequest]
    log_path: Path | None
    _lock = threading.Lock()

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            parsed = {}
        return parsed if isinstance(parsed, dict) else {}

    def _record(self, body: dict) -> None:
        model = body.get("model") if isinstance(body.get("model"), str) else None
        entry = SeenRequest(
            method=self.command,
            path=self.path,
            authorization=self.headers.get("Authorization"),
            model=model,
        )
        with self._lock:
            type(self).seen.append(entry)
            if self.log_path is not None:
                with self.log_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry.journal_entry()) + "\n")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _answer(self) -> None:
        body = self._read_json()
        self._record(body)
        model = body.get("model") if isinstance(body.get("model"), str) else "unknown-model"
        host = self.headers.get("Host") or "{}:{}".format(*self.server.server_address)
        origin = f"http://{host}"
        # Claude Code appends ``?beta=true``; route on the path alone.
        path = urlsplit(self.path).path
        if self.command == "GET" and path == HOST_METADATA_PATH:
            payload = {
                "account_id": "00000000-0000-0000-0000-000000000000",
                "workspace_id": "1234567890123456",
                "hostname": self.headers.get("Host"),
                "oidc_endpoint": f"{origin}/oidc",
            }
            self._send(200, json.dumps(payload).encode(), "application/json")
            return
        if self.command == "GET" and path == OIDC_DISCOVERY_PATH:
            payload = {
                "issuer": origin,
                "authorization_endpoint": f"{origin}/oidc/v1/authorize",
                "token_endpoint": f"{origin}/oidc/v1/token",
            }
            self._send(200, json.dumps(payload).encode(), "application/json")
            return
        if self.command == "POST" and path == f"{AI_GATEWAY_PATH}/v1/messages":
            payload = {
                "type": "error",
                "error": {"type": "permission_error", "message": unavailable_message(model)},
            }
            self._send(403, json.dumps(payload).encode(), "application/json")
            return
        if self.command == "POST" and path == f"{SERVING_ENDPOINTS_PATH}/v1/messages":
            if body.get("stream"):
                self._send(
                    200,
                    anthropic_sse_text_response(REPLY_TEXT, model=model).encode(),
                    "text/event-stream",
                )
            else:
                payload = {
                    "id": f"msg_{uuid.uuid4().hex[:12]}",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [{"type": "text", "text": REPLY_TEXT}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                }
                self._send(200, json.dumps(payload).encode(), "application/json")
            return
        self._send(404, b'{"error":"not found"}', "application/json")

    do_GET = _answer
    do_POST = _answer

    def log_message(self, fmt: str, *args: object) -> None:
        pass


@dataclass
class StubWorkspace:
    base_url: str
    seen: list[SeenRequest]

    @property
    def serving_endpoints_url(self) -> str:
        return f"{self.base_url}{SERVING_ENDPOINTS_PATH}"


@contextmanager
def stub_workspace(port: int = 0, log_path: Path | None = None) -> Iterator[StubWorkspace]:
    """Serve the stub on ``127.0.0.1:<port>`` (0 picks a free port)."""
    handler = type("_StubHandler", (_Handler,), {"seen": [], "log_path": log_path})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield StubWorkspace(
            base_url=f"http://127.0.0.1:{server.server_address[1]}", seen=handler.seen
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--log", type=Path, default=None)
    args = parser.parse_args(argv)
    with stub_workspace(args.port, args.log) as stub:
        print(stub.base_url, flush=True)
        with suppress(KeyboardInterrupt):
            threading.Event().wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
