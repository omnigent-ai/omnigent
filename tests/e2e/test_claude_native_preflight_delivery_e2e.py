"""End-to-end regression for OMNI-2530 pre-flight forwarder failures."""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

import omnigent.claude_native_forwarder as forwarder


class _FailFirstAuth(httpx.Auth):
    """Fail once before yielding a request, then allow retries through."""

    def __init__(self) -> None:
        self.failed = False

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        if not self.failed:
            self.failed = True
            raise httpx.RequestError("token refresh returned no token")
        yield request


class _RecordingServer(ThreadingHTTPServer):
    requests: queue.Queue[dict[str, Any]]


class _RecordingHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        cast(_RecordingServer, self.server).requests.put(payload)
        self.send_response(202)
        self.end_headers()
        self.wfile.write(b"{}")


@contextmanager
def _recording_server() -> Iterator[tuple[_RecordingServer, str]]:
    server = _RecordingServer(("127.0.0.1", 0), _RecordingHandler)
    server.requests = queue.Queue()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = server.server_address
    host = str(address[0])
    port = int(address[1])
    try:
        yield server, f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_preflight_auth_failure_retries_claude_tool_call_after_recovery(
    tmp_path: Path,
) -> None:
    """A request that failed before send must not burn the transcript item id."""
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "assistant-tool-call",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_omni_2530",
                            "name": "Read",
                            "input": {"file_path": "/tmp/example.txt"},
                        }
                    ],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=0,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript_path, 0),
    )
    retry_tracker = forwarder._PostRetryTracker(base_delay_s=0.0, max_delay_s=0.0)
    dedupe = forwarder._ForwardDedupeState()

    with _recording_server() as (server, base_url):
        async with httpx.AsyncClient(base_url=base_url, auth=_FailFirstAuth()) as client:
            after_failure = await forwarder._forward_available_items(
                client=client,
                session_id="conv_omni_2530",
                bridge_dir=bridge_dir,
                agent_name="claude-native-ui",
                state=state,
                retry_tracker=retry_tracker,
                dedupe=dedupe,
            )
            after_recovery = await forwarder._forward_available_items(
                client=client,
                session_id="conv_omni_2530",
                bridge_dir=bridge_dir,
                agent_name="claude-native-ui",
                state=after_failure,
                retry_tracker=retry_tracker,
                dedupe=dedupe,
            )

    assert after_failure.byte_offset == 0
    assert after_failure.seen_source_ids == ()
    assert after_recovery.byte_offset == transcript_path.stat().st_size
    posted = server.requests.get_nowait()
    assert posted["type"] == "external_conversation_item"
    assert posted["data"]["item_type"] == "function_call"
    assert posted["data"]["item_data"]["name"] == "Read"
    assert server.requests.empty()
