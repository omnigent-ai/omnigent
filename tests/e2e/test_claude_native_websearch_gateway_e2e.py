"""E2E: WebSearch under a gateway-backed launch must not surface a US-only error.

Claude Code's WebSearch client tool executes by issuing a *nested*
``/v1/messages`` request that carries the server-side ``web_search`` tool.
That nested request follows ``ANTHROPIC_BASE_URL`` too, so when Omnigent
launches a native Claude terminal against a gateway (the Databricks AI
Gateway path), it lands on the gateway, which rejects the server tool with a
region-restriction error. Claude Code surfaces that rejection to the user as
the WebSearch outcome ("API Error: 400 ... only available in the US ...").
Against api.anthropic.com the identical nested request succeeds.

Claude Code disables WebSearch itself for the provider paths it can detect
(Bedrock, Vertex, its own enterprise-gateway mode), but a launch that only
pins ``ANTHROPIC_BASE_URL`` reads as first-party to it, so the tool stays
enabled and fails at use time. Omnigent knows the endpoint is a gateway, so
the launch composition must withhold the tool.

The journey is the user's own, driven end to end through the REAL ``claude``
CLI launched with the REAL product composition
(:func:`omnigent.claude_native._claude_terminal_request` — the same terminal
spec the claude-native launch paths build):

1. Omnigent composes the native-Claude terminal spec for a gateway-backed
   provider (``ANTHROPIC_BASE_URL`` pointed at the gateway).
2. The user asks Claude to search the web.
3. The mock endpoint plays both the model and the gateway leg: any request
   carrying the server ``web_search`` tool gets the gateway's real-world
   US-only rejection; everything else behaves normally, and the model calls
   WebSearch whenever it is offered.

Expected (post-fix): the turn completes without surfacing the region
restriction to the user — WebSearch is not offered under a gateway that
cannot serve it, so the model answers without it. Before the fix the final
output contains the API error text and this test FAILS.

Self-contained: mocks only the gateway HTTP endpoint; the claude CLI and the
launch composition are real. Requires no server, no credentials, no network.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any

import pytest

from omnigent.claude_native import ClaudeNativeUcodeConfig, _claude_terminal_request
from omnigent.claude_native_bridge import prepare_bridge_dir
from tests.e2e._harness_probes import cli_unavailable_reason

RESTRICTION_MESSAGE = (
    "Web search is only available in the US: the web_search tool "
    "is not supported in this region (mock gateway US-only restriction)."
)


class _MockGateway(http.server.ThreadingHTTPServer):
    """Loopback stand-in for a Databricks AI Gateway Anthropic endpoint.

    Plays both roles the journey needs:

    - the *model*: calls the ``WebSearch`` client tool whenever it is offered
      (expanding it through ``ToolSearch`` first when deferred), echoes a
      WebSearch outcome so it is user-visible, and answers in plain text when
      no search tool is available;
    - the *gateway leg*: any request whose ``tools`` carry the server-side
      ``web_search`` tool is rejected with the US-only region restriction —
      the exact behavior that breaks WebSearch on gateway-backed launches.
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        super().__init__(("127.0.0.1", 0), _MockGatewayHandler)

    @property
    def host(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


def _sse_event(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _sse_message(blocks: list[dict[str, Any]], stop_reason: str) -> bytes:
    """Minimal Anthropic Messages SSE stream carrying *blocks*."""
    out = [
        _sse_event(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_mock",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": "mock",
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 10, "output_tokens": 1},
                },
            },
        )
    ]
    for i, blk in enumerate(blocks):
        if blk["type"] == "text":
            out.append(
                _sse_event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": i,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
            )
            out.append(
                _sse_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": i,
                        "delta": {"type": "text_delta", "text": blk["text"]},
                    },
                )
            )
        else:  # tool_use
            out.append(
                _sse_event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": i,
                        "content_block": {
                            "type": "tool_use",
                            "id": blk["id"],
                            "name": blk["name"],
                            "input": {},
                        },
                    },
                )
            )
            out.append(
                _sse_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": i,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": json.dumps(blk["input"]),
                        },
                    },
                )
            )
        out.append(_sse_event("content_block_stop", {"type": "content_block_stop", "index": i}))
    out.append(
        _sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": 5},
            },
        )
    )
    out.append(_sse_event("message_stop", {"type": "message_stop"}))
    return "".join(out).encode()


def _tool_uses(parsed: dict[str, Any]) -> set[str]:
    """Names of client tools already called earlier in the conversation."""
    names: set[str] = set()
    for message in parsed.get("messages", []):
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = block.get("name")
                    if isinstance(name, str):
                        names.add(name)
    return names


class _MockGatewayHandler(http.server.BaseHTTPRequestHandler):
    server: _MockGateway

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._send(200, "application/json", b"{}")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length)
        try:
            parsed: dict[str, Any] = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            parsed = {}
        self.server.requests.append({"path": self.path, "body": parsed})

        if "/messages" not in self.path or "count_tokens" in self.path:
            self._send(200, "application/json", json.dumps({"input_tokens": 10}).encode())
            return

        tools = parsed.get("tools") or []
        # The nested request Claude Code issues to *execute* WebSearch carries
        # the server-side web_search tool. The gateway leg rejects it: this is
        # the US-only restriction users hit on gateway-backed launches.
        if any("web_search" in json.dumps(t) for t in tools):
            rejection = {
                "type": "error",
                "error": {"type": "invalid_request_error", "message": RESTRICTION_MESSAGE},
            }
            self._send(400, "application/json", json.dumps(rejection).encode())
            return

        tool_names = {t.get("name") for t in tools if isinstance(t, dict)}
        already_ran = _tool_uses(parsed)
        if "WebSearch" in already_ran and '"tool_result"' in json.dumps(parsed):
            # Follow-up after WebSearch executed: echo the tool result so the
            # outcome (search result or surfaced API error) is user-visible.
            results: list[str] = []
            for message in parsed.get("messages", []):
                content = message.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            results.append(json.dumps(block.get("content"))[:600])
            text = "FINAL: " + (" | ".join(results) or "(no tool result content)")
            stream = _sse_message([{"type": "text", "text": text}], "end_turn")
            self._send(200, "text/event-stream", stream)
            return

        # Call WebSearch as soon as it is offered. Under Omnigent's env,
        # Claude Code defers client tools behind MCP Tool Search, so expand
        # them with a single ToolSearch call first; after that, answer in
        # plain text when WebSearch was never offered (the fixed behavior).
        if "WebSearch" in tool_names:
            self._send(
                200,
                "text/event-stream",
                _sse_message(
                    [
                        {
                            "type": "tool_use",
                            "id": "toolu_ws_1",
                            "name": "WebSearch",
                            "input": {"query": "Paris weather today"},
                        }
                    ],
                    "tool_use",
                ),
            )
            return
        if "ToolSearch" in tool_names and "ToolSearch" not in already_ran:
            self._send(
                200,
                "text/event-stream",
                _sse_message(
                    [
                        {
                            "type": "tool_use",
                            "id": "toolu_ts_1",
                            "name": "ToolSearch",
                            "input": {"query": "web search"},
                        }
                    ],
                    "tool_use",
                ),
            )
            return
        self._send(
            200,
            "text/event-stream",
            _sse_message(
                [{"type": "text", "text": "ANSWER-WITHOUT-WEBSEARCH: no search available."}],
                "end_turn",
            ),
        )

    def log_message(self, *args: object) -> None:  # keep pytest output clean
        pass


@pytest.mark.posix_only
@pytest.mark.timeout(300)
def test_websearch_under_gateway_launch_does_not_surface_us_only_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A web-search turn on a gateway-backed launch must not error US-only.

    The launch is composed by the real
    :func:`omnigent.claude_native._claude_terminal_request` (the claude-native
    terminal spec) against a gateway that — like the Databricks AI Gateway —
    rejects the server ``web_search`` tool with a region restriction. The
    user-observable outcome must not be that rejection: before the fix the
    final output contains ``API Error: 400 Web search is only available in
    the US ...`` and this test fails.
    """
    reason = cli_unavailable_reason("claude")
    if reason is not None:
        pytest.skip(f"requires a runnable 'claude' CLI; {reason}")

    gateway = _MockGateway()
    threading.Thread(target=gateway.serve_forever, daemon=True).start()
    bridge_dir: Path | None = None
    try:
        # The real product composition used to point the native Claude
        # terminal at a gateway: base URL override + apiKeyHelper credential.
        claude_config = ClaudeNativeUcodeConfig(
            env={"ANTHROPIC_BASE_URL": gateway.host},
            api_key_helper="echo test-key",
        )

        config_dir = tmp_path / "claude-config"
        config_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        monkeypatch.chdir(workspace)

        bridge_dir = prepare_bridge_dir(f"conv_e2e_{uuid.uuid4().hex[:12]}", workspace=workspace)
        body = _claude_terminal_request(
            (
                "-p",
                "Search the web for today's weather in Paris.",
                "--allowedTools",
                "WebSearch",
                "--model",
                "claude-sonnet-4-5",
                "--output-format",
                "text",
            ),
            command="claude",
            bridge_dir=bridge_dir,
            claude_config=claude_config,
        )
        spec = body["spec"]

        env = {
            k: v
            for k, v in os.environ.items()
            # Keep any corporate proxy out of the loopback gateway path.
            if k.lower() not in {"http_proxy", "https_proxy", "all_proxy"}
        }
        env.update(spec["env"])
        env.update(
            {
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
                "CLAUDE_CONFIG_DIR": str(config_dir),
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "DISABLE_TELEMETRY": "1",
            }
        )

        proc = subprocess.run(
            [spec["command"], *spec["args"]],
            cwd=spec["cwd"],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=240,
        )
    finally:
        gateway.shutdown()
        gateway.server_close()
        if bridge_dir is not None:
            shutil.rmtree(bridge_dir, ignore_errors=True)

    output = f"{proc.stdout}\n{proc.stderr}"
    model_turns = [r for r in gateway.requests if "/messages" in r["path"]]
    assert model_turns, "the claude CLI never reached the gateway endpoint"
    assert proc.returncode == 0, f"claude CLI exited {proc.returncode}: {output[-2000:]}"
    assert proc.stdout.strip(), f"claude CLI produced no answer: {output[-2000:]}"

    # The bug: the gateway's region rejection of the nested web_search request
    # is surfaced to the user as the WebSearch outcome.
    assert "only available in the US" not in output and "API Error" not in output, (
        "WebSearch under the gateway launch surfaced the US-only region "
        f"restriction to the user: {proc.stdout.strip()[-1500:]!r}"
    )
    # And the launch must never have sent the gateway a request carrying the
    # server-side web_search tool — the nested leg the gateway cannot serve.
    nested_search_requests = [
        r["path"]
        for r in gateway.requests
        if any("web_search" in json.dumps(t) for t in (r["body"].get("tools") or []))
    ]
    assert not nested_search_requests, (
        "the launch still routed a nested server-side web_search request to "
        f"the gateway: {nested_search_requests}"
    )
