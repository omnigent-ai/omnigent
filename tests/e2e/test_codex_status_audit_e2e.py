"""A real native Codex turn ships session-specific lifecycle diagnostics."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import time
from pathlib import Path

import httpx
import pytest
import yaml

from tests._helpers.server_runner import server_runner
from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e.conftest import configure_mock_llm
from tests.e2e.test_native_terminal_start_error_log_correlation_e2e import _ZeroBusCapture


@pytest.mark.skipif(
    shutil.which("codex") is None or shutil.which("tmux") is None,
    reason="requires native Codex and tmux",
)
@pytest.mark.timeout(150)
def test_native_cli_turn_status_reaches_the_debug_sink_with_its_session(
    tmp_path: Path,
    isolated_mock_llm_server_url: str,
) -> None:
    model, answer = "gpt-5.4", "STATUS_AUDIT_COMPLETE"
    configure_mock_llm(isolated_mock_llm_server_url, [{"text": answer}], key=model)
    source = tmp_path / "source-home"
    source.mkdir()
    runtime = {"OMNIGENT_HARNESS_TMP_PARENT": os.environ.get("OMNIGENT_HARNESS_TMP_PARENT")}
    with (
        contextlib.closing(_ZeroBusCapture()) as capture,
        server_runner(
            tmp_path / "stack",
            server_env=runtime,
        ) as stack,
    ):
        stack.start_runner(
            env={
                **runtime,
                "CODEX_HOME": str(source),
                "OMNIGENT_DEBUG_LOG_CLIENT_ID": "e2e-client",
                "OMNIGENT_DEBUG_LOG_CLIENT_SECRET": "e2e-secret",
                "OMNIGENT_DEBUG_LOG_WORKSPACE_URL": capture.base_url,
                "OMNIGENT_DEBUG_LOG_ENDPOINT": capture.insert_url,
            }
        )
        with httpx.Client(base_url=stack.base_url, trust_env=False, timeout=30) as client:
            created = post_session_bundle(
                client.post,
                "/v1/sessions",
                bundle_files(
                    {
                        "native.yaml": yaml.safe_dump(
                            {
                                "name": "audit-test",
                                "prompt": "Reply briefly.",
                                "executor": {
                                    "harness": "codex-native",
                                    "model": model,
                                    "auth": {
                                        "type": "api_key",
                                        "api_key": "mock-key",
                                        "base_url": f"{isolated_mock_llm_server_url}/v1",
                                    },
                                },
                            }
                        ).encode(),
                    }
                ),
            )
            created.raise_for_status()
            session = created.json()["session_id"]
            bind_session_runner(client.patch, stack.base_url, session, stack.runner_id)
            sent = client.post(
                f"/v1/sessions/{session}/events",
                json={
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "Reply once."},
                        ],
                    },
                },
            )
            sent.raise_for_status()
            deadline = time.monotonic() + 90
            while True:
                response = client.get(f"/v1/sessions/{session}")
                response.raise_for_status()
                snapshot = response.json()
                assert snapshot["status"] != "failed", (
                    snapshot["last_task_error"],
                    stack.log_tail(),
                )
                if snapshot["status"] == "idle" and answer in json.dumps(snapshot["items"]):
                    break
                assert time.monotonic() < deadline, stack.log_tail()
                time.sleep(0.1)
            deadline = time.monotonic() + 15
            while True:
                rows = [
                    row
                    for row in capture.rows()
                    if row.get("event_name") == "codex_turn_status"
                    and row.get("session_id") == session
                    and row.get("attributes", {}).get("protocol_source") == "turn/completed"
                ]
                if any(row["attributes"]["status"] == "idle" for row in rows):
                    break
                assert time.monotonic() < deadline, stack.log_tail()
                time.sleep(0.05)
            assert all(row.get("turn_id") for row in rows)
            assert all(row["attributes"]["has_turn_id"] == "True" for row in rows)
            assert all(row["attributes"]["has_error"] == "False" for row in rows)
            assert all(row["attributes"]["protocol_source"] for row in rows)
            assert snapshot["last_task_error"] is None
            assert snapshot["pending_inputs"] == []
