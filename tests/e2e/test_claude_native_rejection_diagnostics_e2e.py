"""Rejected uploads retain evidence through real forwarding and log-sink processes."""

from __future__ import annotations

import contextlib
import http.server
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from tests._helpers.live_server import (
    isolated_local_server,
    isolated_process_env,
    terminate_process,
)
from tests._helpers.native_session import create_native_session
from tests.e2e.test_native_terminal_start_error_log_correlation_e2e import _ZeroBusCapture


@pytest.mark.parametrize(
    "status_upload_lost", [False, True], ids=["status-accepted", "status-lost"]
)
def test_rejection_metadata_survives_forwarder_and_log_sink(
    tmp_path: Path, status_upload_lost: bool
) -> None:
    private = "synthetic-private-validation-value"
    rejection_id = "12345678-1234-1234-1234-123456789abc"
    posts: list[dict] = []
    with (
        contextlib.closing(_ZeroBusCapture()) as capture,
        isolated_local_server(
            tmp_path / "server",
            env={
                "OMNIGENT_DEBUG_LOG_CLIENT_ID": "e2e-client",
                "OMNIGENT_DEBUG_LOG_CLIENT_SECRET": "e2e-secret",
                "OMNIGENT_DEBUG_LOG_WORKSPACE_URL": capture.base_url,
                "OMNIGENT_DEBUG_LOG_ENDPOINT": capture.insert_url,
            },
        ) as base_url,
        httpx.Client(trust_env=False, timeout=15) as client,
    ):
        session = create_native_session(client, base_url, harness="claude")
        session_id = session["session_id"]

        # This external edge rejects the upload; all other RPCs reach the real server.
        class RejectionEdge(http.server.BaseHTTPRequestHandler):
            def handle_request(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                payload = json.loads(raw) if raw else {}
                status, response, headers = 200, b"{}", {}
                if payload.get("type") == "external_conversation_item" and (
                    payload["data"].get("source_id") == "assistant-alpha:0:message"
                ):
                    posts.append(payload)
                    status = 422
                    response = json.dumps(
                        {
                            "detail": [
                                {
                                    "type": "dict_type",
                                    "loc": ["body", "data"],
                                    "input": private,
                                    "msg": private,
                                },
                                {
                                    "type": "missing",
                                    "loc": ["body", private],
                                    "ctx": {"error": private},
                                },
                            ]
                        }
                    ).encode()
                    headers["x-request-id"] = rejection_id
                elif (
                    status_upload_lost
                    and payload.get("type") == "external_session_status"
                    and payload["data"].get("status") == "failed"
                ):
                    status = 503
                else:
                    forwarded = client.request(
                        self.command,
                        base_url + self.path,
                        content=raw,
                        headers={"content-type": "application/json"},
                    )
                    status, response = forwarded.status_code, forwarded.content
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(response)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(response)

            do_POST = handle_request
            do_PATCH = handle_request
            do_GET = handle_request

            def log_message(self, *_args: object) -> None:
                pass

        edge = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RejectionEdge)
        thread = threading.Thread(target=edge.serve_forever, daemon=True)
        thread.start()
        native_tmp = tmp_path / "native-tmp"
        native_tmp.mkdir(mode=0o700)
        code = """\
import asyncio,sys
from pathlib import Path
from omnigent.process_logging import configure_process_logging
from tests.e2e.test_claude_native_forwarder_stall_replay_dedup_e2e import _seed_transcript
from omnigent.harnesses.claude_native.forwarder import forward_claude_transcript_to_session
from omnigent.harnesses.claude_native.bridge import _BRIDGE_ROOT
configure_process_logging('runner')
bridge=_BRIDGE_ROOT/'case'
bridge.mkdir(parents=True,exist_ok=True)
_seed_transcript(bridge)
asyncio.run(forward_claude_transcript_to_session(base_url=sys.argv[1],headers={},session_id=sys.argv[2],bridge_dir=bridge,agent_name='claude-native-ui',start_at_end=False,poll_interval_s=0.05))
"""
        log = (tmp_path / "forwarder.log").open("w")
        proc = None
        try:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    code,
                    f"http://127.0.0.1:{edge.server_address[1]}",
                    session_id,
                ],
                env=isolated_process_env(
                    tmp_path / "forwarder-home",
                    {
                        "TMPDIR": str(native_tmp),
                        "OMNIGENT_DEBUG_LOG_CLIENT_ID": "e2e-client",
                        "OMNIGENT_DEBUG_LOG_CLIENT_SECRET": "e2e-secret",
                        "OMNIGENT_DEBUG_LOG_WORKSPACE_URL": capture.base_url,
                        "OMNIGENT_DEBUG_LOG_ENDPOINT": capture.insert_url,
                    },
                    None,
                ),
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            deadline = time.monotonic() + 45
            rows = []
            while time.monotonic() < deadline:
                assert proc.poll() is None, (tmp_path / "forwarder.log").read_text()[-2500:]
                rows = [
                    row
                    for row in capture.rows()
                    if row.get("event_name") == "claude_transcript_item_rejected"
                ]
                if rows:
                    break
                time.sleep(0.1)
            assert len(rows) == 1, (tmp_path / "forwarder.log").read_text()[-2500:]
            assert len(posts) == 3
            row = rows[0]
            assert row["session_id"] == session_id
            attrs = row["attributes"]
            assert attrs["http_status"] == "422"
            assert attrs["forwarder_validation_summary"] == "dict_type@body.data;missing@body.*"
            assert attrs["forwarder_validation_count"] == "2"
            assert attrs["forwarder_request_id"] == rejection_id
            assert private not in json.dumps(capture.rows())
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                items = client.get(
                    f"{base_url}/v1/sessions/{session_id}/items", params={"limit": 100}
                ).json()["data"]
                if all(
                    any(marker in json.dumps(item) for item in items)
                    for marker in (
                        "stall-replay-bravo-the-stalled-post",
                        "stall-replay-charlie-after-the-stall",
                    )
                ):
                    break
                time.sleep(0.1)
            for marker in (
                "stall-replay-bravo-the-stalled-post",
                "stall-replay-charlie-after-the-stall",
            ):
                assert sum(marker in json.dumps(item) for item in items) == 1
            if not status_upload_lost:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    failures = [
                        r for r in capture.rows() if r.get("event_name") == "session_turn_failed"
                    ]
                    if any(
                        r.get("attributes", {}).get("forwarder_request_id") == rejection_id
                        for r in failures
                    ):
                        break
                    time.sleep(0.1)
                assert any(
                    r.get("attributes", {}).get("forwarder_request_id") == rejection_id
                    for r in failures
                )
            (tmp_path / "captured-rows.json").write_text(json.dumps(capture.rows(), indent=2))
        finally:
            terminate_process(proc)
            log.close()
            edge.shutdown()
            edge.server_close()
            thread.join(timeout=5)
