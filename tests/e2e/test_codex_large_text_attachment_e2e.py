"""Upload a large text file through the real server, runner, and Codex CLI."""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

import httpx
import pytest
import yaml

from tests._helpers.server_runner import server_runner
from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e.conftest import configure_mock_llm, get_mock_requests


@pytest.mark.skipif(
    shutil.which("codex") is None or shutil.which("tmux") is None,
    reason="requires the native Codex CLI and tmux",
)
@pytest.mark.timeout(150)
def test_uploaded_large_text_file_reaches_native_codex_as_a_complete_reference(
    tmp_path: Path, isolated_mock_llm_server_url: str
) -> None:
    model = "gpt-5.4"
    answer = "LARGE_ATTACHMENT_DELIVERED"
    configure_mock_llm(isolated_mock_llm_server_url, [{"text": answer}], key=model)
    source_home = tmp_path / "source-home"
    source_home.mkdir()
    original = "BEGIN_LARGE_ATTACHMENT\n" + "report row\n" * 140_000 + "END_LARGE_ATTACHMENT\n"
    with server_runner(tmp_path / "stack") as stack:
        stack.start_runner(env={"CODEX_HOME": str(source_home)})
        with httpx.Client(base_url=stack.base_url, trust_env=False, timeout=30) as client:
            created = post_session_bundle(
                client.post,
                "/v1/sessions",
                bundle_files(
                    {
                        "native.yaml": yaml.safe_dump(
                            {
                                "name": "attachment-test",
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
            upload = client.post(
                f"/v1/sessions/{session}/resources/files",
                files={"file": ("report.txt", original.encode(), "text/plain")},
            )
            upload.raise_for_status()
            file_id = upload.json()["id"]
            sent = client.post(
                f"/v1/sessions/{session}/events",
                json={
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [
                            {"type": "input_file", "file_id": file_id, "filename": "report.txt"},
                            {"type": "input_text", "text": "Acknowledge the attached report."},
                        ],
                    },
                },
            )
            assert sent.is_success, (sent.status_code, sent.text, stack.log_tail())
            deadline = time.monotonic() + 90
            while True:
                response = client.get(f"/v1/sessions/{session}")
                response.raise_for_status()
                snapshot = response.json()
                assert snapshot["status"] != "failed", (
                    snapshot.get("last_task_error"),
                    stack.log_tail(),
                )
                if answer in json.dumps(snapshot["items"]) and snapshot["status"] == "idle":
                    break
                assert time.monotonic() < deadline, stack.log_tail()
                time.sleep(0.1)
            assert snapshot["last_task_error"] is None
            requests = get_mock_requests(isolated_mock_llm_server_url, key=model)
            assert requests, stack.log_tail()
            wire = json.dumps(requests)
            assert "BEGIN_LARGE_ATTACHMENT" not in wire
            paths = re.findall(r"\[Attached(?: file)?: ([^\]]+)\]", wire)
            assert paths, wire[:500]
            assert any(Path(path).read_text() == original for path in paths)
            assert snapshot["pending_inputs"] == [], snapshot["pending_inputs"]
