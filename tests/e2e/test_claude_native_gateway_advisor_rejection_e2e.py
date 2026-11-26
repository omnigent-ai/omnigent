"""An SDK-resolved Claude launch must survive a gateway that rejects the advisor tool."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from omnigent.inner import claude_sdk_executor as claude_sdk
from omnigent.onboarding.ambient import CLAUDE_CODE_MANAGED_SETTINGS_PATHS
from tests.server.integration.mock_llm_server import anthropic_sse_text_response

_CLAUDE_BINARY = shutil.which("claude")

pytestmark = [
    pytest.mark.skipif(_CLAUDE_BINARY is None, reason="requires the Claude Code CLI"),
    pytest.mark.timeout(180),
]

_REJECTION = {"message": "tool type 'advisor_20260301' is not supported for this model"}
_REPLY = "gateway reply without the advisor"


def _advisor_tools(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        tool
        for body in requests
        for tool in body.get("tools") or []
        if str(tool.get("type", "")).startswith("advisor_")
    ]


@contextmanager
def _advisor_rejecting_gateway(requests: list[dict[str, Any]]) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def _send(self, status: int, payload: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            self._send(404, b'{"message":"not found"}', "application/json")

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path.split("?", 1)[0] != "/v1/messages":
                self._send(200, b'{"input_tokens":10}', "application/json")
                return
            requests.append(body)
            if _advisor_tools([body]):
                self._send(400, json.dumps(_REJECTION).encode(), "application/json")
                return
            payload = anthropic_sse_text_response(_REPLY, model=body.get("model", "mock-model"))
            self._send(200, payload.encode(), "text/event-stream")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _gateway_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gateway_url: str
) -> tuple[list[str], dict[str, str]]:
    """Build a gateway-provider launch for a user with the advisor enabled."""
    monkeypatch.setattr(claude_sdk, "is_databricks_ai_gateway_url", lambda url: url == gateway_url)
    gateway_env = claude_sdk._resolve_gateway_env(
        base_url_override=gateway_url,
        auth_command_override="printf token",
    )
    api_key_helper = gateway_env.pop(claude_sdk._CLAUDE_API_KEY_HELPER_ENV_KEY)

    # advisorModel is what /advisor persists; the env flag stands in for the rollout flag.
    if any(
        path.is_file() and path.read_text().strip() for path in CLAUDE_CODE_MANAGED_SETTINGS_PATHS
    ):
        pytest.skip("requires clean system config so machine policy cannot reroute test calls")
    config_dir = tmp_path / "claude-config"
    config_dir.mkdir()
    (config_dir / "settings.json").write_text(
        json.dumps(
            {
                "advisorModel": "opus",
                "env": {"CLAUDE_CODE_ENABLE_EXPERIMENTAL_ADVISOR_TOOL": "1"},
            }
        )
    )
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "CLAUDE_CONFIG_DIR": str(config_dir),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        **gateway_env,
    }
    argv = [
        str(_CLAUDE_BINARY),
        "-p",
        "Reply with one short sentence.",
        "--model",
        "claude-sonnet-test",
        "--settings",
        json.dumps({"apiKeyHelper": api_key_helper}),
        "--no-session-persistence",
    ]
    return argv, env


def _run_claude(
    argv: list[str], env: dict[str, str], cwd: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _describe(result: subprocess.CompletedProcess[str], requests: list[dict[str, Any]]) -> str:
    return (
        f"exit={result.returncode} stdout={result.stdout!r} stderr={result.stderr[-2000:]!r} "
        f"requests={len(requests)} advisor_tools={_advisor_tools(requests)}"
    )


def test_gateway_launch_answers_when_gateway_rejects_advisor_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user with the advisor enabled still gets replies through the Databricks gateway."""
    requests: list[dict[str, Any]] = []
    with _advisor_rejecting_gateway(requests) as gateway_url:
        argv, env = _gateway_launch(tmp_path, monkeypatch, gateway_url)
        control_env = {k: v for k, v in env.items() if k != "CLAUDE_CODE_DISABLE_ADVISOR_TOOL"}
        control = _run_claude(argv, control_env, tmp_path)
        control_requests = list(requests)
        requests.clear()
        result = _run_claude(argv, env, tmp_path)

    assert control_requests, "The control CLI did not reach the mock gateway: " + _describe(
        control, control_requests
    )
    if not _advisor_tools(control_requests):
        pytest.skip("this Claude Code CLI does not send the advisor tool even without the flag")
    assert control.returncode != 0 and "advisor_" in control.stdout + control.stderr, (
        "Without the flag the CLI sent the advisor tool but did not surface the gateway's 400, "
        f"so Claude Code's behavior changed: {_describe(control, control_requests)}"
    )
    assert not _advisor_tools(requests), _describe(result, requests)
    assert result.returncode == 0 and _REPLY in result.stdout, _describe(result, requests)
