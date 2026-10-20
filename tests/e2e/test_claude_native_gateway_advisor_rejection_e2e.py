"""The Databricks native-Claude launch must survive a gateway that rejects the advisor tool."""

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

from omnigent.harnesses.claude_native import main as claude_native
from omnigent.runner.native.orchestration import _claude_terminal_env_unset
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


def _ucode_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gateway_url: str
) -> tuple[list[str], dict[str, str]]:
    """Return the claude argv and ucode terminal env for a user with the advisor enabled."""
    from omnigent.onboarding.ucode_state import UcodeAgentState, UcodeWorkspaceState

    workspace_state = UcodeWorkspaceState(
        workspace_url="https://example.databricks.com",
        agents={
            "claude": UcodeAgentState(
                model="databricks-claude-sonnet-test",
                base_url=gateway_url,
                auth_command="printf token",
                auth_refresh_interval_ms=3_600_000,
            )
        },
    )
    monkeypatch.setattr(
        "omnigent.onboarding.databricks_config.get_workspace_url_for_profile",
        lambda profile: "https://example.databricks.com",
    )
    monkeypatch.setattr(
        "omnigent.onboarding.ucode_state.read_ucode_state",
        lambda workspace_url: workspace_state,
    )
    config = claude_native._ucode_config_for_profile("test-profile", refresh_models=False)
    assert config is not None and config.api_key_helper and config.model

    # advisorModel is what /advisor persists; the env flag stands in for the rollout flag.
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
        **claude_native.build_native_claude_terminal_env(config),
    }
    for name in _claude_terminal_env_unset(config):
        env.pop(name, None)
    argv = [
        str(_CLAUDE_BINARY),
        "-p",
        "Reply with one short sentence.",
        "--model",
        config.model,
        "--settings",
        json.dumps({"apiKeyHelper": config.api_key_helper}),
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


def test_ucode_launch_answers_when_gateway_rejects_advisor_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user with the advisor enabled still gets replies through the Databricks gateway."""
    requests: list[dict[str, Any]] = []
    with _advisor_rejecting_gateway(requests) as gateway_url:
        argv, env = _ucode_launch(tmp_path, monkeypatch, gateway_url)
        control_env = {k: v for k, v in env.items() if k != "CLAUDE_CODE_DISABLE_ADVISOR_TOOL"}
        control = _run_claude(argv, control_env, tmp_path)
        control_requests = list(requests)
        requests.clear()
        result = _run_claude(argv, env, tmp_path)

    if not _advisor_tools(control_requests):
        pytest.skip("this Claude Code CLI does not send the advisor tool even without the flag")
    assert control.returncode != 0 and "advisor_" in control.stdout + control.stderr, (
        "Without the flag the CLI sent the advisor tool but did not surface the gateway's 400, "
        f"so Claude Code's behavior changed: {_describe(control, control_requests)}"
    )
    assert not _advisor_tools(requests), _describe(result, requests)
    assert result.returncode == 0 and _REPLY in result.stdout, _describe(result, requests)
