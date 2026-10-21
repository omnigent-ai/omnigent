"""Shared forwarder test support."""

from __future__ import annotations

import asyncio
import json
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest


class _RecordingHTTPServer(ThreadingHTTPServer):
    """
    HTTP server that records JSON POST bodies.

    :param server_address: Host/port tuple for
        :class:`ThreadingHTTPServer`.
    :param RequestHandlerClass: Handler class used for requests.
    """

    requests: queue.Queue[dict[str, Any]]


def _handler_factory(
    requests: queue.Queue[dict[str, Any]],
) -> type[BaseHTTPRequestHandler]:
    """
    Create a request handler that records POST JSON.

    :param requests: Queue receiving decoded request records.
    :returns: A concrete :class:`BaseHTTPRequestHandler` subclass.
    """

    class _Handler(BaseHTTPRequestHandler):
        """Request handler for the test Omnigent endpoint."""

        def log_message(self, format: str, *args: Any) -> None:
            """
            Suppress test HTTP server logging.

            :param format: Log format string.
            :param args: Log format arguments.
            :returns: None.
            """
            del format, args

        def do_POST(self) -> None:
            """
            Record a JSON POST body and return HTTP 202.

            :returns: None.
            """
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            requests.put(
                {
                    "method": "POST",
                    "path": self.path,
                    "body": json.loads(raw.decode("utf-8")),
                    "authorization": self.headers.get("Authorization"),
                }
            )
            self.send_response(202)
            self.end_headers()
            self.wfile.write(b"{}")

        def do_PATCH(self) -> None:
            """
            Record a JSON PATCH body and return HTTP 200.

            :returns: None.
            """
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            requests.put(
                {
                    "method": "PATCH",
                    "path": self.path,
                    "body": json.loads(raw.decode("utf-8")),
                    "authorization": self.headers.get("Authorization"),
                }
            )
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

    return _Handler


def _start_recording_server() -> tuple[_RecordingHTTPServer, threading.Thread, str]:
    """
    Start a local HTTP server that records POST bodies.

    :returns: ``(server, thread, base_url)``.
    """
    requests: queue.Queue[dict[str, Any]] = queue.Queue()
    server = _RecordingHTTPServer(("127.0.0.1", 0), _handler_factory(requests))
    server.requests = requests
    thread = threading.Thread(
        target=server.serve_forever,
        name="claude-forwarder-test-ap",
        daemon=True,
    )
    thread.start()
    host, port = server.server_address
    return server, thread, f"http://{host}:{port}"


async def _get_recorded_request(
    server: _RecordingHTTPServer,
    *,
    timeout_s: float = 5.0,
    method: str = "POST",
) -> dict[str, Any]:
    """
    Await one recorded request from the test server, filtered by method.

    The forwarder mirrors Claude's native session id to Omnigent via a
    one-shot ``PATCH /v1/sessions/{id}`` (see
    :func:`_maybe_mirror_external_session_id`). Most tests in this
    file assert on POSTs to ``/events``; defaulting the filter to
    ``"POST"`` lets those tests skip the mirroring PATCH that lands
    at the start of every loop in which the bridge state carries a
    Claude session id. PATCH-specific tests pass ``method="PATCH"``.

    :param server: Recording HTTP server.
    :param timeout_s: Maximum seconds to wait — applied per
        ``queue.get`` call, so the helper can spend up to
        ``timeout_s`` skipping each non-matching request before
        giving up on the next matching one.
    :param method: HTTP method to filter for, e.g. ``"POST"`` or
        ``"PATCH"``. Non-matching requests are silently discarded.
    :returns: Recorded request dict whose ``method`` matches.
    """
    while True:
        try:
            request = await asyncio.to_thread(server.requests.get, True, timeout_s)
        except queue.Empty as exc:
            raise AssertionError(
                f"forwarder did not produce a {method} request",
            ) from exc
        if request.get("method") == method:
            return request


async def _get_recorded_item_request(
    server: _RecordingHTTPServer,
    *,
    timeout_s: float = 5.0,
) -> dict[str, Any]:
    """
    Await the next ``external_conversation_item`` POST, skipping status edges.

    The forwarder now emits a turn-start ``external_session_status: running``
    (carrying the turn's response id, which drives the live tool-card spinner
    in ap-web) BEFORE a turn's items each poll. Tests that only care about the
    forwarded conversation items use this to skip that leading status edge (and
    any trailing idle) without asserting on it.

    :param server: Recording HTTP server.
    :param timeout_s: Per-``get`` timeout while skipping non-item POSTs.
    :returns: The next recorded ``external_conversation_item`` POST.
    """
    while True:
        request = await _get_recorded_request(server, timeout_s=timeout_s)
        if request["body"].get("type") == "external_conversation_item":
            return request


async def _wait_for_json_state(
    path: Path,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    timeout_s: float = 5.0,
) -> dict[str, Any]:
    """
    Wait until a JSON object file satisfies ``predicate``.

    :param path: JSON file path.
    :param predicate: Function returning ``True`` for the desired
        state, e.g. ``lambda payload: "byte_offset" in payload``.
    :param timeout_s: Maximum seconds to wait.
    :returns: Parsed JSON object satisfying the predicate.
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    last_payload: dict[str, Any] | None = None
    while asyncio.get_running_loop().time() < deadline:
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            assert isinstance(payload, dict)
            last_payload = payload
            if predicate(payload):
                return payload
        await asyncio.sleep(0.01)
    raise AssertionError(f"{path} did not reach expected state; last={last_payload!r}")


# ── Sub-agent watcher (Claude Code Task tool) ────────────


def _start_recording_server_with_responses(
    response_for: Callable[[object], object] | None = None,
) -> tuple[_RecordingHTTPServer, threading.Thread, str]:
    """
    Start a local HTTP server that records POST bodies AND returns
    a customizable response body.

    Variant of :func:`_start_recording_server` for tests that need
    the Omnigent server's response (rather than just a generic 202 ``{}``)
    — used by the sub-agent watcher tests because
    ``external_subagent_start`` returns ``{"child_session_id": "..."}``
    that the forwarder reads back.

    :param response_for: Callback that takes the decoded request
        body and returns the JSON value to send back. ``None`` (the
        default) responds with ``{}`` like the standard recorder.
    :returns: ``(server, thread, base_url)``.
    """
    requests: queue.Queue[dict[str, Any]] = queue.Queue()

    class _Handler(BaseHTTPRequestHandler):
        """Recording handler with response customization."""

        def log_message(self, format: str, *args: Any) -> None:
            """Suppress test HTTP server logging.

            :param format: Log format string.
            :param args: Log format arguments.
            :returns: None.
            """
            del format, args

        def do_POST(self) -> None:
            """Record a JSON POST body and send a customizable response.

            :returns: None.
            """
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            body = json.loads(raw.decode("utf-8"))
            requests.put({"method": "POST", "path": self.path, "body": body})
            response_body = {} if response_for is None else response_for(body)
            self.send_response(202)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response_body).encode("utf-8"))

        def do_PATCH(self) -> None:
            """Record a JSON PATCH body and respond ``{}``.

            :returns: None.
            """
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            requests.put(
                {"method": "PATCH", "path": self.path, "body": json.loads(raw.decode("utf-8"))}
            )
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

    server = _RecordingHTTPServer(("127.0.0.1", 0), _Handler)
    server.requests = requests
    thread = threading.Thread(
        target=server.serve_forever,
        name="claude-forwarder-test-ap-subagent",
        daemon=True,
    )
    thread.start()
    host, port = server.server_address
    return server, thread, f"http://{host}:{port}"


def _seed_subagent_on_disk(
    *,
    transcript_path: Path,
    subagent_id: str,
    agent_type: str,
    description: str,
    tool_use_id: str,
    transcript_records: list[dict[str, Any]] | None = None,
    spawn_transcript_path: Path | None = None,
    spawn_tool_name: str = "Agent",
) -> Path:
    """
    Create the ``.meta.json`` + ``.jsonl`` pair Claude Code would
    write for a Task-tool sub-agent.

    Mirrors the on-disk layout the forwarder's watcher polls:
    ``<transcript_parent>/<transcript_stem>/subagents/agent-<id>.*``.

    :param transcript_path: Parent transcript JSONL path. The sibling
        ``<stem>/subagents/`` directory is created next to it.
    :param subagent_id: Stable Claude-side id (the ``agent-<id>``
        filename stem), e.g. ``"a5c7eff..."``.
    :param agent_type: ``agentType`` value for the meta file,
        e.g. ``"Explore"``.
    :param description: ``description`` value for the meta file.
    :param tool_use_id: ``toolUseId`` value for the meta file.
    :param transcript_records: Optional list of decoded transcript
        rows to seed into the sub-agent's ``.jsonl``. ``None`` /
        empty leaves the transcript empty (the common case when a
        sub-agent has just been spawned).
    :param spawn_transcript_path: Transcript containing the spawning
        tool call. Defaults to the top-level transcript.
    :param spawn_tool_name: Name on the spawning ``tool_use`` block.
        Defaults to ``"Agent"``; pass ``"Task"`` to exercise the alias.
    :returns: Path to the sub-agent's ``.jsonl`` (handy for tests
        that append rows after the fact).
    """
    spawn_path = spawn_transcript_path or transcript_path
    with spawn_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "isSidechain": spawn_path != transcript_path,
                    "type": "assistant",
                    "uuid": f"spawn-{subagent_id}",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": tool_use_id,
                                "name": spawn_tool_name,
                                "input": {"description": description},
                            }
                        ],
                    },
                }
            )
            + "\n"
        )
    subagents_dir = transcript_path.parent / transcript_path.stem / "subagents"
    subagents_dir.mkdir(parents=True, exist_ok=True)
    meta_path = subagents_dir / f"agent-{subagent_id}.meta.json"
    meta_path.write_text(
        json.dumps(
            {
                "agentType": agent_type,
                "description": description,
                "toolUseId": tool_use_id,
            }
        ),
        encoding="utf-8",
    )
    jsonl_path = subagents_dir / f"agent-{subagent_id}.jsonl"
    if transcript_records:
        jsonl_path.write_text(
            "\n".join(json.dumps(row) for row in transcript_records) + "\n",
            encoding="utf-8",
        )
    else:
        jsonl_path.write_text("", encoding="utf-8")
    return jsonl_path


# ---------------------------------------------------------------------------
# In-pane /effort → Omnigent session reasoning_effort mirroring
# ---------------------------------------------------------------------------


@dataclass
class _CapturedRequest:
    """
    One request seen by the effort-sync mock transport.

    :param method: HTTP method, e.g. ``"PATCH"``.
    :param path: Request path, e.g. ``"/v1/sessions/conv_x"``.
    :param body: Parsed JSON body, or ``None`` when the request had no body.
    """

    method: str
    path: str
    body: dict[str, Any] | None


def _subagent_drop_row(caplog: pytest.LogCaptureFixture) -> dict[str, Any]:
    from omnigent.debug_logging import record_to_row

    records = [
        record
        for record in caplog.records
        if getattr(record, "event_name", None) == "claude_subagent_transcript_dropped"
    ]
    assert len(records) == 1
    return record_to_row(records[0], source="runner")
