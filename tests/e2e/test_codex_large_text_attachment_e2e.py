"""Preserve large text through the real server, runner, and native Codex CLI."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from tests._helpers.polling import poll_until
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e.conftest import get_mock_requests, set_fallback_mock_llm


@pytest.mark.skipif(
    shutil.which("codex") is None or shutil.which("tmux") is None,
    reason="requires the native Codex CLI and tmux",
)
@pytest.mark.timeout(150)
def test_large_file_and_paste_reach_native_codex_and_keep_history(
    tmp_path: Path, isolated_mock_llm_server_url: str
) -> None:
    model = "gpt-5.4"
    source_home = tmp_path / "source-home"
    source_home.mkdir()
    original = "BEGIN_LARGE_ATTACHMENT\n" + "report row\n" * 140_000 + "END_LARGE_ATTACHMENT\n"
    paste = "BEGIN_LARGE_PASTE\n" + "r\u00e9sum\u00e9 log line\n" * 90_000 + "END_LARGE_PASTE\n"
    assert len(original) > 1_048_576 and len(paste) > 1_048_576
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
            for content, answer in (
                (
                    [
                        {"type": "input_file", "file_id": file_id, "filename": "report.txt"},
                        {"type": "input_text", "text": "Acknowledge the attached report."},
                    ],
                    "LARGE_ATTACHMENT_DELIVERED",
                ),
                ([{"type": "input_text", "text": paste}], "LARGE_PASTE_DELIVERED"),
            ):
                # Auxiliary native requests can share the model's response queue.
                set_fallback_mock_llm(isolated_mock_llm_server_url, key=model, text=answer)
                sent = client.post(
                    f"/v1/sessions/{session}/events",
                    json={"type": "message", "data": {"role": "user", "content": content}},
                )
                assert sent.is_success, (sent.status_code, sent.text, stack.log_tail())
                snapshot: dict[str, Any] = {}

                def completed(expected_answer: str = answer) -> bool:
                    nonlocal snapshot
                    response = client.get(f"/v1/sessions/{session}")
                    response.raise_for_status()
                    snapshot = response.json()
                    assert snapshot["status"] != "failed", (
                        snapshot.get("last_task_error"),
                        stack.log_tail(),
                    )
                    return (
                        expected_answer in json.dumps(snapshot["items"])
                        and snapshot["status"] == "idle"
                        and not snapshot["pending_inputs"]
                    )

                poll_until(completed, timeout_s=90, interval_s=0.1)
                assert snapshot["last_task_error"] is None
                requests = get_mock_requests(isolated_mock_llm_server_url, key=model)
                assert requests, stack.log_tail()
                wire = json.dumps(requests)
                assert "BEGIN_LARGE_ATTACHMENT" not in wire
                assert "BEGIN_LARGE_PASTE" not in wire
                paths = re.findall(r"\[Attached(?: file)?: ([^\]]+)\]", wire)
                assert paths, wire[:500]
                expected = paste if answer == "LARGE_PASTE_DELIVERED" else original
                assert any(Path(path).read_text(encoding="utf-8") == expected for path in paths)
                user_content = [
                    item["data"]["content"]
                    for item in snapshot["items"]
                    if item["type"] == "message" and item["data"].get("role") == "user"
                ]
                assert any(
                    block.get("file_id") == file_id
                    for message in user_content
                    for block in message
                )
                if answer == "LARGE_PASTE_DELIVERED":
                    assert user_content[-1] == [{"type": "input_text", "text": paste}]
