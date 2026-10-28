"""E2E: a Pi session that Pi compacted must resume compacted after a server/runner restart.

A loopback proxy enforces a 32k window with an OpenAI-style context-length 400, so the
real Pi CLI runs its own overflow compaction against the scripted mock model.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import math
import shutil
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from playwright.sync_api import Browser, Page, expect

from omnigent.testing.process_reaper import reap_leaked_omnigent_processes
from tests._helpers.live_server import terminate_process
from tests._helpers.server_runner import ServerRunner, server_runner
from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e.omnigent._pi_mock_gateway import write_pi_gateway_config
from tests.e2e_ui.conftest import configure_mock_llm, reset_mock_llm
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _WORKING,
    _ensure_chat_view,
)

_MODEL = "local-vllm-32k"
_CONTEXT_LIMIT = 32_768
_TURN_TIMEOUT_MS = 180_000
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


def _send(page: Page, text: str) -> None:
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_enabled(timeout=60_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _send_turn(page: Page, text: str, reply: str) -> None:
    _send(page, text)
    expect(page.locator(_ASSISTANT, has_text=reply).first).to_be_visible(timeout=_TURN_TIMEOUT_MS)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_TURN_TIMEOUT_MS)


def _is_summarization(entry: dict[str, Any]) -> bool:
    text = _user_text(entry["body"])
    return any(prompt in text for prompt in _SUMMARY_PROMPTS)


def _wait_for_turn_outcome(page: Page, replies_before: int) -> str:
    replies = page.locator(_ASSISTANT)
    error_pill = page.get_by_test_id("error-pill")
    deadline = time.monotonic() + _TURN_TIMEOUT_MS / 1000
    while time.monotonic() < deadline:
        if replies.count() > replies_before and page.locator(_WORKING).count() == 0:
            replies.last.scroll_into_view_if_needed()
            return f"reply: {replies.last.inner_text()[:300]}"
        if error_pill.count():
            pill = error_pill.last
            pill.scroll_into_view_if_needed()
            pill.get_by_role("button", expanded=False).first.click()
            message = pill.get_by_test_id("error-message-content")
            expect(message).to_be_visible(timeout=10_000)
            message.scroll_into_view_if_needed()
            return f"error: {message.inner_text()[:300]}"
        page.wait_for_timeout(500)
    return "no outcome"


def _restart_stack(stack: ServerRunner, runner_env: dict[str, str | None]) -> None:
    terminate_process(stack.runner)
    stack.runner = None
    reap_leaked_omnigent_processes(stack.runner_home / ".omnigent")
    stack.restart_server()
    stack.start_runner(env=runner_env)


@pytest.mark.nightly
@pytest.mark.timeout(900)
def test_pi_compacted_session_resumes_compacted_after_restart(
    browser: Browser,
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """The first post-restart request carries Pi's summary and fits the window."""
    if shutil.which("pi") is None:
        pytest.skip("requires the real pi executable")
    nonce = uuid.uuid4().hex[:8]
    post_marker, resume_marker = f"POST-01-{nonce}", f"RESUME-01-{nonce}"
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
    diagnostics: dict[str, Any] = {"nonce": nonce, "timeline": []}
    started = time.monotonic()

    def mark(label: str) -> None:
        diagnostics["timeline"].append((label, round(time.monotonic() - started, 2)))

    with (
        proxy.running(),
        httpx.Client(trust_env=False) as client,
        server_runner(tmp_path / "stack") as stack,
    ):
        runner_env = _runner_env(stack, f"{proxy.url}")
        stack.start_runner(env=runner_env)
        session_id = _create_pi_session(client, stack)
        diagnostics["session_id"] = session_id
        context = browser.new_context()
        outcome = "not reached"
        restart_seq = 0
        pre_markers: list[str] = []
        compacting_markers: list[str] = []
        try:
            page = context.new_page()
            page.goto(f"{stack.base_url}/c/{session_id}")
            _ensure_chat_view(page)
            mark("session open")
            first_compaction_turn: int | None = None
            for index in range(1, _MAX_FILL_TURNS + 1):
                marker = f"PRE-{index:02d}-{nonce}"
                pre_markers.append(marker)
                script_reply(marker)
                sent_at = len(proxy.requests)
                _send_turn(
                    page,
                    f"Turn {marker}: here is part {index} of the build log I am debugging. "
                    f"Keep it in mind.\n{_log_part(index)}",
                    f"ack-{marker}",
                )
                mark(f"turn {marker} answered")
                if any(_is_summarization(entry) for entry in proxy.requests[sent_at:]):
                    compacting_markers.append(marker)
                if first_compaction_turn is None and any(
                    _is_summarization(entry) for entry in proxy.requests
                ):
                    first_compaction_turn = index
                if (
                    first_compaction_turn is not None
                    and index >= first_compaction_turn + _TURNS_AFTER_FIRST_COMPACTION
                ):
                    break
            diagnostics["fill_turns"] = len(pre_markers)
            diagnostics["first_compaction_turn"] = first_compaction_turn
            script_reply(post_marker)
            sent_at = len(proxy.requests)
            _send_turn(
                page, f"Turn {post_marker}: does anything stand out so far?", f"ack-{post_marker}"
            )
            mark("post-compaction turn answered")
            if any(_is_summarization(entry) for entry in proxy.requests[sent_at:]):
                compacting_markers.append(post_marker)
            diagnostics["compacting_turns"] = compacting_markers
            deadline = time.monotonic() + 30.0
            compactions: list[dict[str, Any]] = []
            while time.monotonic() < deadline and not compactions:
                compactions = [
                    item
                    for item in _items(client, stack.base_url, session_id)
                    if item.get("type") == "compaction"
                ]
                time.sleep(1.0)
            diagnostics["compaction_items_before_restart"] = len(compactions)
            restart_seq = len(proxy.requests)
            _restart_stack(stack, runner_env)
            mark("server and runner restarted")
            page.goto(f"{stack.base_url}/c/{session_id}")
            _ensure_chat_view(page)
            expect(page.locator(_ASSISTANT, has_text=f"ack-{post_marker}").first).to_be_visible(
                timeout=60_000
            )
            mark("session reopened")
            script_reply(resume_marker)
            replies_before = page.locator(_ASSISTANT).count()
            _send(page, f"Turn {resume_marker}: which worker was slowest?")
            outcome = _wait_for_turn_outcome(page, replies_before)
            mark(f"resume turn outcome: {outcome}")
            page.wait_for_timeout(4_000)
        finally:
            context.close()
            diagnostics["outcome"] = outcome
            (tmp_path / "diagnostics.json").write_text(json.dumps(diagnostics, indent=2))

    summarization = [entry for entry in proxy.requests[:restart_seq] if _is_summarization(entry)]
    assert summarization, (
        "Pi never compacted before the restart; the setup did not fill the window"
    )
    resumed = [
        entry
        for entry in proxy.requests[restart_seq:]
        if resume_marker in _user_text(entry["body"])
    ]
    assert resumed, "no model request carried the post-restart message"
    first = resumed[0]
    (tmp_path / "first-resumed-request.json").write_text(json.dumps(first, indent=2))
    resumed_text = _user_text(first["body"])
    summarized_text = "\n".join(_user_text(entry["body"]) for entry in summarization)
    summarized_markers = [
        marker for marker in pre_markers if marker in summarized_text and marker in resumed_text
    ]
    assert first["status"] == 200 and f"ack-{resume_marker}" in outcome, (
        f"the resumed Pi session overflowed the model window: first request estimated "
        f"{first['estimated_tokens']} tokens (limit {_CONTEXT_LIMIT}), HTTP {first['status']}; "
        f"chat outcome {outcome!r}; Omnigent persisted "
        f"{diagnostics['compaction_items_before_restart']} compaction items; summarized turns "
        f"replayed: {summarized_markers}; summary present: {summary.split()[0] in resumed_text}"
    )
    assert not summarized_markers, (
        f"turns Pi had already summarized were replayed after restart: {summarized_markers}"
    )
    assert summary.split()[0] in resumed_text, "the resumed context lost Pi's compaction summary"
    # The last compacting turn's prompt is stored before its compaction item, so only
    # the kept context can replay it.
    assert f"Turn {compacting_markers[-1]}:" in resumed_text, (
        "the resumed context lost the prompt Pi kept when it last compacted"
    )
