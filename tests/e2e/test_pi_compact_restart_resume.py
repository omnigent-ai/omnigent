"""E2E: a Pi session that Pi compacted must resume compacted after a server/runner restart.

A loopback proxy enforces a 32k window with an OpenAI-style context-length 400, so the
real Pi CLI runs its own overflow compaction against the scripted mock model.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import math
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from omnigent.testing.process_reaper import reap_leaked_omnigent_processes
from tests._helpers.live_server import terminate_process
from tests._helpers.server_runner import ServerRunner, server_runner
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
_BIND_TIMEOUT_S = 180.0
# Pi keeps ~20k recent tokens on overflow; small parts let the retry fit the window.
_PART_CHARS = 4_000
_MAX_FILL_TURNS = 40
# Keep going after Pi's first compaction until raw history is ~48k tokens, well over the window.
_TURNS_AFTER_FIRST_COMPACTION = 15
_SUMMARY_PROMPTS = (
    "Create a structured context checkpoint summary",
    "Create a concise checkpoint of the user's request",
    "incorporate into the existing summary",
)


def _estimated_tokens(body: dict[str, Any]) -> int:
    text = json.dumps(body.get("messages", [])) + json.dumps(body.get("tools", []))
    return math.ceil(len(text) / 4)


class _ContextLimitProxy:
    """Forward to the mock model, rejecting prompts over the window like vLLM."""

    def __init__(self, upstream: str, log_path: Path) -> None:
        self.requests: list[dict[str, Any]] = []
        self._upstream = upstream
        self._log_path = log_path
        self._lock = threading.Lock()
        proxy = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                proxy._handle(self)

            def log_message(self, *args: object) -> None:
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}"

    def _handle(self, handler: http.server.BaseHTTPRequestHandler) -> None:
        raw = handler.rfile.read(int(handler.headers.get("content-length") or 0))
        body = json.loads(raw or b"{}")
        tokens = _estimated_tokens(body)
        if tokens > _CONTEXT_LIMIT:
            status = 400
            content_type = "application/json"
            payload = json.dumps(
                {
                    "error": {
                        "message": (
                            f"This model's maximum context length is {_CONTEXT_LIMIT} tokens, "
                            f"however you requested {tokens} tokens"
                        ),
                        "type": "BadRequestError",
                        "param": None,
                        "code": 400,
                    }
                }
            ).encode()
        else:
            try:
                upstream = httpx.post(
                    f"{self._upstream}{handler.path}",
                    content=raw,
                    headers={"content-type": "application/json"},
                    timeout=120.0,
                    trust_env=False,
                )
                status = upstream.status_code
                content_type = upstream.headers.get("content-type", "application/json")
                payload = upstream.content
            except httpx.HTTPError as exc:
                status, content_type = 502, "application/json"
                payload = json.dumps(
                    {"error": {"message": f"proxy upstream error: {exc!r}"}}
                ).encode()
        with self._lock:
            entry = {
                "seq": len(self.requests),
                "at": round(time.time(), 3),
                "path": handler.path,
                "estimated_tokens": tokens,
                "status": status,
                "body": body,
            }
            self.requests.append(entry)
            with self._log_path.open("a", encoding="utf-8") as log:
                summary = {key: value for key, value in entry.items() if key != "body"}
                summary["user_text_head"] = _user_text(body)[:160]
                log.write(json.dumps(summary) + "\n")
        handler.send_response(status)
        handler.send_header("content-type", content_type)
        handler.send_header("content-length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    @contextlib.contextmanager
    def running(self) -> Iterator[_ContextLimitProxy]:
        thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        thread.start()
        try:
            yield self
        finally:
            self._server.shutdown()
            self._server.server_close()


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


def _log_part(index: int) -> str:
    lines: list[str] = []
    size = 0
    line_number = 0
    while size < _PART_CHARS:
        line = (
            f"[part{index:02d}:{line_number:03d}] worker-{line_number % 7} compiled "
            f"src/pkg_{index}/module_{line_number}.py in {(line_number * 37) % 997}ms; "
            f"cache {'hit' if line_number % 3 else 'miss'}, rss {(line_number * 53) % 900}MB"
        )
        lines.append(line)
        size += len(line) + 1
        line_number += 1
    return "\n".join(lines)


def _runner_env(stack: ServerRunner, model_url: str) -> dict[str, str | None]:
    config_home = stack.runner_home / ".omnigent-config"
    write_pi_gateway_config(config_home, mock_url=model_url, model=_MODEL)
    return {
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "OPENAI_API_KEY": None,
        "OPENAI_BASE_URL": None,
    }


def _create_pi_session(client: httpx.Client, stack: ServerRunner) -> str:
    spec = {
        "spec_version": 1,
        "name": "pi-long-session",
        "prompt": "You are a concise assistant.",
        "executor": {"model": _MODEL, "config": {"harness": "pi"}},
    }
    response = post_session_bundle(
        client.post,
        f"{stack.base_url}/v1/sessions",
        bundle_files({"config.yaml": yaml.safe_dump(spec).encode()}),
        timeout=30.0,
    )
    response.raise_for_status()
    session_id = str(response.json()["session_id"])
    bind_session_runner(
        client.patch, stack.base_url, session_id, stack.runner_id, timeout=_BIND_TIMEOUT_S
    )
    return session_id


def _items(client: httpx.Client, base_url: str, session_id: str) -> list[dict[str, Any]]:
    response = client.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 500, "order": "asc"},
        timeout=30.0,
    )
    response.raise_for_status()
    return list(response.json()["data"])


def _send_turn(client: httpx.Client, session_id: str, text: str, reply: str) -> None:
    response_id = send_user_message_to_session(client, session_id=session_id, content=text)
    result = poll_session_until_terminal(
        client, session_id=session_id, response_id=response_id, timeout=180
    )
    assert result["status"] == "completed", result
    assert reply in json.dumps(result["output"]), result


def _is_summarization(entry: dict[str, Any]) -> bool:
    text = _user_text(entry["body"])
    return any(prompt in text for prompt in _SUMMARY_PROMPTS)


def _restart_stack(stack: ServerRunner, runner_env: dict[str, str | None]) -> None:
    terminate_process(stack.runner)
    stack.runner = None
    reap_leaked_omnigent_processes(stack.runner_home / ".omnigent")
    stack.restart_server()
    stack.start_runner(env=runner_env)


@pytest.mark.nightly
@pytest.mark.timeout(900)
def test_pi_compacted_session_resumes_compacted_after_restart(
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """Restart and fork preserve Pi's summary and the recent context it kept."""
    skip_if_harness_cli_missing("pi")
    nonce = uuid.uuid4().hex[:8]
    post_marker, resume_marker, fork_marker = (
        f"{stage}-01-{nonce}" for stage in ("POST", "NEXT", "FORK")
    )
    summary = f"SUMMARY-{nonce} the user shared build log parts; nothing failed yet."
    reset_mock_llm(mock_llm_server_url)

    def script_reply(marker: str) -> None:
        configure_mock_llm(
            mock_llm_server_url,
            [{"text": f"ack-{marker}: noted."}],
            match=marker,
            required_tools=["read"],
        )

    for prompt in _SUMMARY_PROMPTS:
        configure_mock_llm(mock_llm_server_url, [{"text": summary}] * 60, match=prompt)

    proxy = _ContextLimitProxy(mock_llm_server_url, tmp_path / "proxy-requests.jsonl")
    pre_markers: list[str] = []
    compacting_markers: list[str] = []
    with (
        proxy.running(),
        server_runner(tmp_path / "stack") as stack,
        httpx.Client(base_url=stack.base_url, timeout=30, trust_env=False) as client,
    ):
        runner_env = _runner_env(stack, proxy.url)
        stack.start_runner(env=runner_env)
        session_id = _create_pi_session(client, stack)
        first_compaction_turn: int | None = None
        for index in range(1, _MAX_FILL_TURNS + 1):
            marker = f"PRE-{index:02d}-{nonce}"
            pre_markers.append(marker)
            script_reply(marker)
            sent_at = len(proxy.requests)
            _send_turn(
                client,
                session_id,
                f"Turn {marker}: here is part {index} of the build log I am debugging. "
                f"Keep it in mind.\n{_log_part(index)}",
                f"ack-{marker}",
            )
            if any(_is_summarization(entry) for entry in proxy.requests[sent_at:]):
                compacting_markers.append(marker)
                if first_compaction_turn is None:
                    first_compaction_turn = index
            if (
                first_compaction_turn is not None
                and index >= first_compaction_turn + _TURNS_AFTER_FIRST_COMPACTION
            ):
                break
        assert first_compaction_turn is not None
        assert len(pre_markers) >= first_compaction_turn + _TURNS_AFTER_FIRST_COMPACTION
        script_reply(post_marker)
        sent_at = len(proxy.requests)
        _send_turn(
            client,
            session_id,
            f"Turn {post_marker}: does anything stand out so far?",
            f"ack-{post_marker}",
        )
        if any(_is_summarization(entry) for entry in proxy.requests[sent_at:]):
            compacting_markers.append(post_marker)
        restart_seq = len(proxy.requests)
        _restart_stack(stack, runner_env)
        # Read from the restarted server, rather than an in-memory session cache.
        assert f"ack-{post_marker}" in json.dumps(_items(client, stack.base_url, session_id))
        script_reply(resume_marker)
        _send_turn(
            client,
            session_id,
            f"Turn {resume_marker}: which worker was slowest?",
            f"ack-{resume_marker}",
        )
        response = client.post(f"/v1/sessions/{session_id}/fork", json={})
        response.raise_for_status()
        fork_id = response.json()["id"]
        assert fork_id != session_id
        bind_session_runner(client.patch, stack.base_url, fork_id, stack.runner_id)
        script_reply(fork_marker)
        _send_turn(
            client,
            fork_id,
            f"Turn {fork_marker}: continue investigating the build.",
            f"ack-{fork_marker}",
        )
        assert fork_marker not in json.dumps(_items(client, stack.base_url, session_id))

    summarization = [entry for entry in proxy.requests[:restart_seq] if _is_summarization(entry)]
    summarized_text = "\n".join(_user_text(entry["body"]) for entry in summarization)
    assert compacting_markers, "Pi never compacted before the restart"
    for marker in (resume_marker, fork_marker):
        resumed = [
            entry
            for entry in proxy.requests[restart_seq:]
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
