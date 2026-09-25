"""Deterministic backend contract for browser-only live-chat tests."""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Page, Request, Route, WebSocketRoute

from tests.browser_ui.conftest import BrowserContract

DEFAULT_SESSION_ID = "browser-chat-session"
DEFAULT_AGENT_ID = "browser-chat-agent"
DEFAULT_HOST_ID = "browser-chat-host"


def list_payload(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Build the cursor-list envelope used by Omnigent collection routes."""
    data = [dict(row) for row in rows]
    return {
        "object": "list",
        "data": data,
        "first_id": data[0].get("id") if data else None,
        "last_id": data[-1].get("id") if data else None,
        "has_more": False,
    }


def model_option(
    model_id: str,
    *,
    model: str | None = None,
    display_name: str | None = None,
    is_default: bool = False,
) -> dict[str, Any]:
    """Build one runner-owned model-catalog entry."""
    return {
        "id": model_id,
        "model": model or model_id,
        "displayName": display_name or model_id,
        "isDefault": is_default,
    }


def message_item(
    item_id: str,
    role: str,
    text: str,
    *,
    response_id: str,
) -> dict[str, Any]:
    """Build one persisted user or assistant transcript message."""
    content_type = "input_text" if role == "user" else "output_text"
    return {
        "id": item_id,
        "response_id": response_id,
        "type": "message",
        "role": role,
        "status": "completed",
        "content": [{"type": content_type, "text": text}],
    }


def transcript_items(turns: int) -> list[dict[str, Any]]:
    """Build newest-first history with markdown and fenced code in every turn."""
    chronological: list[dict[str, Any]] = []
    for index in range(turns):
        response_id = f"browser-response-{index}"
        chronological.extend(
            [
                message_item(
                    f"browser-user-{index}",
                    "user",
                    f"## Request {index + 1}\n\nExplain browser fixture turn **{index + 1}**.",
                    response_id=response_id,
                ),
                message_item(
                    f"browser-assistant-{index}",
                    "assistant",
                    (
                        f"Turn {index + 1} response with `inline code`.\n\n"
                        "```python\n"
                        f"print('browser turn {index + 1}')\n"
                        "```"
                    ),
                    response_id=response_id,
                ),
            ]
        )
    return list(reversed(chronological))


def session_status_event(
    session_id: str,
    status: str,
    *,
    response_id: str | None = None,
) -> dict[str, Any]:
    """Build the wire representation of a ``session.status`` SSE event."""
    data: dict[str, Any] = {"conversation_id": session_id, "status": status}
    if response_id is not None:
        data["response_id"] = response_id
    return {"event": "session.status", "data": data}


class _ChatSseServer(ThreadingHTTPServer):
    daemon_threads = True


class _ChatSseStream:
    """Tiny streaming HTTP server used as the target of the mocked SSE route."""

    def __init__(self) -> None:
        self._clients: set[queue.Queue[bytes | None]] = set()
        self._lock = threading.Lock()
        self._pending: list[bytes] = []
        self._ever_connected = False
        self._connection_generation = 0
        self._connected = threading.Event()
        stream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                messages: queue.Queue[bytes | None] = queue.Queue()
                with stream._lock:
                    for event in stream._pending:
                        messages.put(event)
                    stream._pending.clear()
                    stream._ever_connected = True
                    stream._clients.add(messages)
                    stream._connection_generation += 1
                    stream._connected.set()
                try:
                    self.wfile.write(b": browser chat stream ready\n\n")
                    self.wfile.flush()
                    while True:
                        try:
                            event = messages.get(timeout=5)
                        except queue.Empty:
                            event = b": heartbeat\n\n"
                        if event is None:
                            break
                        self.wfile.write(event)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    # Browser reloads close the old SSE socket while the handler is writing.
                    pass
                finally:
                    with stream._lock:
                        stream._clients.discard(messages)
                        if not stream._clients:
                            stream._connected.clear()

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self._server = _ChatSseServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=lambda: self._server.serve_forever(poll_interval=0.05),
            daemon=True,
        )
        self._thread.start()
        host, port = self._server.server_address
        self.url = f"http://{host}:{port}/stream"

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    @property
    def connection_generation(self) -> int:
        with self._lock:
            return self._connection_generation

    def emit(self, event: Mapping[str, Any]) -> None:
        """Queue pre-connect events once, then broadcast only to live clients."""
        name = event.get("event")
        data = event.get("data")
        if not isinstance(name, str) or not isinstance(data, Mapping):
            raise ValueError("SSE events require {'event': str, 'data': mapping}")
        encoded = f"event: {name}\ndata: {json.dumps(dict(data))}\n\n".encode()
        with self._lock:
            if not self._clients and not self._ever_connected:
                self._pending.append(encoded)
            for client in self._clients:
                client.put(encoded)

    def close(self) -> None:
        with self._lock:
            for client in self._clients:
                client.put(b"data: [DONE]\n\n")
                client.put(None)
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@dataclass
class ChatSessionContract:
    """Mutable handle exposed to browser-only chat tests."""

    page: Page
    contract: BrowserContract
    stream: _ChatSseStream
    session_id: str = DEFAULT_SESSION_ID
    agent_id: str = DEFAULT_AGENT_ID
    host_id: str = DEFAULT_HOST_ID
    harness: str = "openai-agents"
    selected_model: str = "browser-model"
    models: list[dict[str, Any]] = field(
        default_factory=lambda: [model_option("browser-model", is_default=True)]
    )
    event_posts: list[dict[str, Any]] = field(default_factory=list)
    upload_requests: list[dict[str, Any]] = field(default_factory=list)
    skills: list[dict[str, Any]] = field(default_factory=list)
    skill_requests: list[dict[str, Any]] = field(default_factory=list)
    session_patches: list[dict[str, Any]] = field(default_factory=list)
    _items: list[dict[str, Any]] = field(default_factory=list)
    _session_updates: dict[str, Any] = field(default_factory=dict)
    _health_updates: dict[str, Any] = field(default_factory=dict)
    _status: str = "idle"
    _hold_skill_responses: bool = False
    _pending_skill_routes: list[Route] = field(default_factory=list)
    event_ack: dict[str, Any] = field(
        default_factory=lambda: {"queued": True, "item_id": "browser-queued-item"}
    )
    reject_uploads: bool = True
    _stream_generation: int = 0

    @property
    def url(self) -> str:
        """Client route for this live chat."""
        return f"{self.contract.base_url}/c/{self.session_id}"

    @property
    def base_url(self) -> str:
        return self.contract.base_url

    def wait_for_stream(self, timeout: float = 10) -> None:
        """Wait until the SPA has opened its live event stream."""
        deadline = time.monotonic() + timeout
        while (
            not self.stream.connected
            or self.stream.connection_generation <= self._stream_generation
        ):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for the chat event stream")
            self.page.wait_for_timeout(min(10, max(1, remaining * 1000)))
        self._stream_generation = self.stream.connection_generation

    def seed_transcript(self, turns: int) -> None:
        """Replace history with ``turns`` user/assistant markdown pairs."""
        if turns < 0:
            raise ValueError("turns must be non-negative")
        self._items = transcript_items(turns)

    def set_items(self, items: Sequence[Mapping[str, Any]]) -> None:
        """Replace history with copies of the supplied wire items."""
        self._items = [dict(item) for item in items]

    def update_session(self, **fields: Any) -> None:
        """Merge wire fields into subsequent mocked session snapshots."""
        self._session_updates.update(fields)

    def set_health(self, **fields: Any) -> None:
        """Override per-session fields returned by the health endpoint."""
        self._health_updates.update(fields)

    def set_catalog(
        self,
        *,
        harness: str,
        models: Sequence[Mapping[str, Any]],
        selected_model: str,
    ) -> None:
        """Configure the bound harness and its model options before navigation."""
        self.harness = harness
        self.models = [dict(model) for model in models]
        self.selected_model = selected_model

    def set_skills(self, skills: Sequence[Mapping[str, Any]]) -> None:
        """Replace the skills returned for this session."""
        self.skills = [dict(skill) for skill in skills]

    def hold_skills(self) -> Callable[[], None]:
        """Hold skills responses until the returned release callable runs."""
        self._hold_skill_responses = True

        def release() -> None:
            self._hold_skill_responses = False
            pending, self._pending_skill_routes = self._pending_skill_routes, []
            for route in pending:
                self._fulfill_skills(route)

        return release

    def _fulfill_skills(self, route: Route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"skills": self.skills}),
        )

    def emit(self, event: Mapping[str, Any]) -> None:
        """Broadcast an SSE wire event and keep the session snapshot consistent."""
        if event.get("event") == "session.status":
            data = event.get("data")
            if isinstance(data, Mapping) and isinstance(data.get("status"), str):
                self._status = str(data["status"])
        self.stream.emit(event)

    def emit_busy(self, response_id: str = "browser-turn") -> None:
        self.emit(session_status_event(self.session_id, "running", response_id=response_id))

    def emit_idle(self, response_id: str | None = "browser-turn") -> None:
        self.emit(session_status_event(self.session_id, "idle", response_id=response_id))

    def _session(self) -> dict[str, Any]:
        session = {
            "id": self.session_id,
            "object": "conversation",
            "title": "Browser chat session",
            "agent_id": self.agent_id,
            "agent_name": self.agent_id,
            "host_id": self.host_id,
            "workspace": "/browser-workspace",
            "status": self._status,
            "created_at": 1_704_067_200,
            "updated_at": 1_704_067_200,
            "labels": {},
            "permission_level": None,
            "harness": self.harness,
            "llm_model": self.selected_model,
            "model_options": self.models,
        }
        session.update(self._session_updates)
        return session

    def _agent(self) -> dict[str, Any]:
        return {
            "id": self.agent_id,
            "object": "agent",
            "name": self.agent_id,
            "description": "Browser-contract chat agent",
            "harness": self.harness,
            "mcp_servers": [],
            "policies": [],
            "terminals": [],
        }

    def _health(self) -> dict[str, Any]:
        status = {"runner_online": True, "host_online": True}
        status.update(self._health_updates)
        return {"sessions": {self.session_id: status}}


def install_chat_session_routes(handle: ChatSessionContract) -> None:
    """Register every backend dependency needed by an idle live-chat page."""
    contract = handle.contract
    empty = list_payload([])

    def matcher(path: str) -> re.Pattern[str]:
        return re.compile(rf"^{re.escape(contract.base_url + path)}(?:\?.*)?$")

    contract.json(
        "/v1/info",
        {
            "accounts_enabled": False,
            "single_user": True,
            "login_url": None,
            "needs_setup": False,
            "databricks_features": False,
            "managed_sandboxes_enabled": False,
            "sandbox_provider": None,
            "sandbox_providers": [],
            "sandbox_provider_capabilities": {},
            "enabled_connections": [],
            "sharing_mode": "off",
            "public_sharing_enabled": False,
            "server_version": "browser-chat-contract",
            "smart_routing_enabled": False,
            "smart_routing_sources": {"external": False, "oss": False},
            "features": {},
            "harness_install_enabled": False,
            "installable_harnesses": [],
            "dictation_available": False,
            "branding": {
                "app_name": None,
                "heading": None,
                "logos": {"main": None, "loading": None, "favicon": None},
                "powered_by": True,
            },
        },
    )
    contract.json("/v1/me", {"user_id": "local", "is_admin": True})
    contract.json("/v1/agents", lambda _request: list_payload([handle._agent()]))
    contract.json(
        "/v1/hosts",
        lambda _request: {
            "hosts": [
                {
                    "host_id": handle.host_id,
                    "name": "Browser host",
                    "owner": "local",
                    "status": "online",
                    "configured_harnesses": {handle.harness: True},
                }
            ]
        },
    )
    contract.json(f"/v1/hosts/{handle.host_id}/worktrees", empty)
    contract.json("/v1/projects", empty)
    contract.json("/v1/projects/order", {"ordered_project_ids": None, "sort_mode": "alphabetical"})
    contract.json("/v1/extensions", empty)
    contract.json(
        "/v1/harnesses",
        lambda _request: {
            "data": [{"id": handle.harness, "name": handle.harness}],
            "setup_steps": {},
        },
    )
    contract.json("/v1/sessions/projects", [])
    contract.json("/v1/sessions", lambda _request: list_payload([handle._session()]))
    session_api = f"/v1/sessions/{handle.session_id}"
    contract.json(session_api, lambda _request: handle._session())

    def patch_session(route: Route) -> None:
        if route.request.method != "PATCH":
            route.fallback()
            return
        body = _request_record(route.request)["body"]
        if not isinstance(body, dict):
            route.fulfill(
                status=400,
                content_type="application/json",
                body=json.dumps({"detail": "Session patch must be a JSON object"}),
            )
            return
        allowed_fields = {
            "approval_mode",
            "archived",
            "collaboration_mode",
            "cost_control_mode_override",
            "external_session_id",
            "labels",
            "model_override",
            "permission_mode",
            "project_id",
            "reasoning_effort",
            "runner_id",
            "share_workspace_files",
            "silent",
            "subagent_routing_override",
            "terminal_launch_args",
            "title",
        }
        unknown_fields = body.keys() - allowed_fields
        if unknown_fields:
            route.fulfill(
                status=422,
                content_type="application/json",
                body=json.dumps(
                    {
                        "detail": [
                            {
                                "type": "extra_forbidden",
                                "loc": ["body", key],
                                "msg": "Extra inputs are not permitted",
                                "input": body[key],
                            }
                            for key in sorted(unknown_fields)
                        ]
                    }
                ),
            )
            return
        handle.session_patches.append(body)
        handle._session_updates.update(
            {key: value for key, value in body.items() if key != "silent"}
        )
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(handle._session()),
        )

    contract.route(matcher(session_api), patch_session)
    contract.json(
        f"{session_api}/items",
        lambda _request: list_payload(handle._items),
    )
    contract.json(f"{session_api}/agent", lambda _request: handle._agent())
    contract.json(f"{session_api}/child_sessions", empty)
    contract.json(f"{session_api}/resources/terminals", empty)
    contract.response(f"{session_api}/read-state", method="PUT")
    for resource in ("environments/default", "github"):
        contract.json(
            f"{session_api}/resources/{resource}",
            {"error": {"message": "No browser-contract environment"}},
            status=404,
        )
    contract.json(
        "/health",
        lambda _request: handle._health(),
    )
    model_catalog = re.compile(
        rf"/v1/hosts/{re.escape(handle.host_id)}/harnesses/[^/]+/model-options(?:\?.*)?$"
    )
    contract.json(
        model_catalog,
        lambda _request: {"models": handle.models, "routable_models": []},
    )

    def skills(route: Route) -> None:
        request = route.request
        query = parse_qs(urlparse(request.url).query)
        if request.method != "GET" or query.get("session_id") != [handle.session_id]:
            route.fallback()
            return
        handle.skill_requests.append(_request_record(request))
        if handle._hold_skill_responses:
            handle._pending_skill_routes.append(route)
            return
        handle._fulfill_skills(route)

    contract.route(matcher("/v1/skills"), skills)

    def post_event(route: Route) -> None:
        if route.request.method != "POST":
            route.fallback()
            return
        handle.event_posts.append(_request_record(route.request))
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(handle.event_ack),
        )

    contract.route(matcher(f"{session_api}/events"), post_event)

    def upload(route: Route) -> None:
        if route.request.method != "POST":
            route.fallback()
            return
        request = _request_record(route.request)
        handle.upload_requests.append(request)
        if handle.reject_uploads:
            route.abort("blockedbyclient")
        else:
            filename, size = _multipart_file_metadata(request)
            upload_id = f"browser-upload-{len(handle.upload_requests)}"
            route.fulfill(
                status=201,
                content_type="application/json",
                body=json.dumps(
                    {
                        "id": upload_id,
                        "object": "session.resource",
                        "type": "file",
                        "session_id": handle.session_id,
                        "name": filename,
                        "metadata": {
                            "filename": filename,
                            "bytes": size,
                            "created_at": 1_704_067_200,
                            "source_metadata": None,
                        },
                    }
                ),
            )

    contract.route(matcher(f"{session_api}/resources/files"), upload)

    def stream(route: Route) -> None:
        if route.request.method != "GET":
            route.fallback()
            return
        route.continue_(url=handle.stream.url)

    contract.route(matcher(f"{session_api}/stream"), stream)

    def updates(socket: WebSocketRoute) -> None:
        socket.on_message(lambda _message: None)

    contract.websocket("**/v1/sessions/updates*", updates)


def _request_record(request: Request) -> dict[str, Any]:
    raw_body = request.post_data_buffer
    content_type = request.headers.get("content-type")
    body: Any = raw_body
    if raw_body and content_type and content_type.split(";", 1)[0] == "application/json":
        with suppress(json.JSONDecodeError, UnicodeDecodeError):
            body = json.loads(raw_body)
    record = {"url": request.url, "method": request.method, "body": body}
    if raw_body is not None:
        record.update(
            {
                "body_bytes": raw_body,
                "body_length": len(raw_body),
                "content_type": content_type,
            }
        )
    return record


def _multipart_file_metadata(request: Mapping[str, Any]) -> tuple[str, int]:
    raw_body = request.get("body_bytes")
    content_type = request.get("content_type")
    if not isinstance(raw_body, bytes) or not isinstance(content_type, str):
        return "upload.bin", 0
    message = BytesParser(policy=email_policy).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + raw_body
    )
    for part in message.iter_parts():
        if (
            part.get_content_disposition() != "form-data"
            or part.get_param("name", header="content-disposition") != "file"
        ):
            continue
        payload = part.get_payload(decode=True)
        return part.get_filename() or "upload.bin", len(payload) if isinstance(
            payload, bytes
        ) else 0
    return "upload.bin", len(raw_body)


def chat_session_handle(
    page: Page,
    browser_contract: BrowserContract,
) -> Iterator[ChatSessionContract]:
    """Create, install, and clean up a reusable chat contract handle."""
    stream = _ChatSseStream()
    try:
        handle = ChatSessionContract(page=page, contract=browser_contract, stream=stream)
        install_chat_session_routes(handle)
        yield handle
    finally:
        stream.close()
