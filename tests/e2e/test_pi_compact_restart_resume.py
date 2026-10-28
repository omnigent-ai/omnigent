"""Real Pi compaction survives server/runner restart and fork under a scripted 32k limit."""

from __future__ import annotations

import json
import math
import threading
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from omnigent.testing.process_reaper import reap_leaked_omnigent_processes
from tests._helpers.live_server import terminate_process
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e._harness_probes import skip_if_harness_cli_missing
from tests.e2e.conftest import (
    configure_mock_llm,
    poll_session_until_terminal,
    reset_mock_llm,
    send_user_message_to_session,
)
from tests.e2e.omnigent._pi_mock_gateway import write_pi_gateway_config

_MODEL = "local-vllm-32k"
_CONTEXT_LIMIT = 32_768
_SUMMARY_PROMPTS = (
    "Create a structured context checkpoint summary",
    "Create a concise checkpoint of the user's request",
    "incorporate into the existing summary",
)


def _user_text(body: dict[str, Any]) -> str:
    texts: list[str] = []
    for message in body.get("messages", []):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            texts.extend(block.get("text", "") for block in content if isinstance(block, dict))
    return "\n".join(texts)


def _is_summarization(entry: dict[str, Any]) -> bool:
    return any(prompt in _user_text(entry["body"]) for prompt in _SUMMARY_PROMPTS)


@pytest.fixture
def context_limited_gateway(
    mock_llm_server_url: str,
) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """Reject over-window requests before forwarding to the shared scripted provider."""
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            body = json.loads(raw)
            tokens = math.ceil(
                len(json.dumps(body.get("messages", [])) + json.dumps(body.get("tools", []))) / 4
            )
            if tokens > _CONTEXT_LIMIT:
                status, content_type = 400, "application/json"
                payload = json.dumps(
                    {
                        "error": {
                            "message": (
                                f"This model's maximum context length is {_CONTEXT_LIMIT} tokens, "
                                f"however you requested {tokens} tokens"
                            )
                        }
                    }
                ).encode()
            else:
                try:
                    response = httpx.post(
                        mock_llm_server_url + self.path,
                        content=raw,
                        headers={"Content-Type": "application/json"},
                        timeout=120,
                        trust_env=False,
                    )
                    status, payload = response.status_code, response.content
                    content_type = response.headers["Content-Type"]
                except httpx.HTTPError as exc:
                    status, content_type = 502, "application/json"
                    payload = json.dumps({"error": {"message": repr(exc)}}).encode()
            requests.append({"body": body, "status": status, "estimated_tokens": tokens})
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _items(client: httpx.Client, session_id: str) -> list[dict[str, Any]]:
    response = client.get(
        f"/v1/sessions/{session_id}/items", params={"limit": 500, "order": "asc"}
    )
    response.raise_for_status()
    return response.json()["data"]


@pytest.mark.nightly
@pytest.mark.timeout(900)
def test_pi_compacted_session_resumes_compacted_after_restart(
    mock_llm_server_url: str,
    context_limited_gateway: tuple[str, list[dict[str, Any]]],
    tmp_path: Path,
) -> None:
    """Both continuations use the saved summary/tail; the fork leaves its parent alone."""
    skip_if_harness_cli_missing("pi")
    gateway_url, requests = context_limited_gateway
    nonce = uuid.uuid4().hex[:8]
    post_marker, resume_marker, fork_marker = (
        f"{stage}-01-{nonce}" for stage in ("POST", "NEXT", "FORK")
    )
    summary = f"SUMMARY-{nonce} the user shared build log parts; nothing failed yet."
    reset_mock_llm(mock_llm_server_url)
    for prompt in _SUMMARY_PROMPTS:
        configure_mock_llm(mock_llm_server_url, [{"text": summary}] * 60, match=prompt)

    pre_markers: list[str] = []
    compacting_markers: list[str] = []
    with (
        server_runner(tmp_path / "stack") as stack,
        httpx.Client(base_url=stack.base_url, timeout=30, trust_env=False) as client,
    ):
        config_home = stack.runner_home / ".omnigent-config"
        write_pi_gateway_config(config_home, mock_url=gateway_url, model=_MODEL)
        runner_env = {
            "OMNIGENT_CONFIG_HOME": str(config_home),
            "OPENAI_API_KEY": None,
            "OPENAI_BASE_URL": None,
        }
        stack.start_runner(env=runner_env)
        spec = {
            "spec_version": 1,
            "name": "pi-long-session",
            "prompt": "You are a concise assistant.",
            "executor": {"model": _MODEL, "config": {"harness": "pi"}},
        }
        response = post_session_bundle(
            client.post,
            "/v1/sessions",
            bundle_files({"config.yaml": yaml.safe_dump(spec).encode()}),
        )
        response.raise_for_status()
        session_id = response.json()["session_id"]
        bind_session_runner(client.patch, stack.base_url, session_id, stack.runner_id, timeout=180)

        def send_turn(session: str, marker: str, prompt: str) -> None:
            configure_mock_llm(
                mock_llm_server_url,
                [{"text": f"ack-{marker}: noted."}],
                match=marker,
                required_tools=["read"],
            )
            response_id = send_user_message_to_session(
                client, session_id=session, content=f"Turn {marker}: {prompt}"
            )
            result = poll_session_until_terminal(
                client, session_id=session, response_id=response_id, timeout=180
            )
            assert result["status"] == "completed", result
            assert f"ack-{marker}" in json.dumps(result["output"]), result

        first_compaction_turn = None
        for index in range(1, 41):
            marker = f"PRE-{index:02d}-{nonce}"
            pre_markers.append(marker)
            sent_at = len(requests)
            # ~1k tokens per turn leaves room for Pi's ~20k retained tail after overflow.
            log = (f"Build log part {index}: worker compiled module; cache miss.\n" * 80)[:4_000]
            send_turn(session_id, marker, f"Keep this build log in mind.\n{log}")
            if any(_is_summarization(entry) for entry in requests[sent_at:]):
                compacting_markers.append(marker)
                if first_compaction_turn is None:
                    first_compaction_turn = index
            # Grow raw history well beyond 32k so an uncompacted restart must fail.
            if first_compaction_turn is not None and index >= first_compaction_turn + 15:
                break
        assert first_compaction_turn is not None
        assert len(pre_markers) >= first_compaction_turn + 15
        sent_at = len(requests)
        send_turn(session_id, post_marker, "Does anything stand out so far?")
        if any(_is_summarization(entry) for entry in requests[sent_at:]):
            compacting_markers.append(post_marker)
        restart_seq = len(requests)
        terminate_process(stack.runner)
        stack.runner = None
        reap_leaked_omnigent_processes(stack.runner_home / ".omnigent")
        stack.restart_server()
        stack.start_runner(env=runner_env)
        assert f"ack-{post_marker}" in json.dumps(_items(client, session_id))
        send_turn(session_id, resume_marker, "Which worker was slowest?")
        response = client.post(f"/v1/sessions/{session_id}/fork", json={})
        response.raise_for_status()
        fork_id = response.json()["id"]
        assert fork_id != session_id
        bind_session_runner(client.patch, stack.base_url, fork_id, stack.runner_id)
        send_turn(fork_id, fork_marker, "Continue investigating the build.")
        assert fork_marker not in json.dumps(_items(client, session_id))

    summarized_text = "\n".join(
        _user_text(entry["body"]) for entry in requests[:restart_seq] if _is_summarization(entry)
    )
    for marker in (resume_marker, fork_marker):
        resumed = [
            entry
            for entry in requests[restart_seq:]
            if f"Turn {marker}:" in _user_text(entry["body"])
        ]
        assert resumed, "no model request carried the continuation"
        first = resumed[0]
        (tmp_path / f"{marker}-request.json").write_text(json.dumps(first, indent=2))
        resumed_text = _user_text(first["body"])
        assert first["status"] == 200, first
        assert summary.split()[0] in resumed_text, "lost Pi's compaction summary"
        assert not [
            previous
            for previous in pre_markers
            if previous in summarized_text and previous in resumed_text
        ], "replayed turns Pi already summarized"
        # This prompt predates the stored boundary; only kept context can replay it.
        assert f"Turn {compacting_markers[-1]}:" in resumed_text, "lost Pi's kept context"
