"""A configured provider must stay authoritative over conflicting Claude Code user settings."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from omnigent.harnesses.claude_native import bridge
from omnigent.harnesses.claude_native import main as claude_native
from tests._helpers.providers import isolate_provider_resolution

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("OMNIGENT_E2E_CLAUDE_NATIVE") != "1" or shutil.which("claude") is None,
        reason="requires Claude Code and OMNIGENT_E2E_CLAUDE_NATIVE=1",
    ),
    pytest.mark.timeout(240),
]

_OTHER_WORKSPACE_MODELS = {"ws-b-opus", "ws-b-sonnet", "ws-b-haiku"}
_OTHER_WORKSPACE_HEADER = "x-workspace-b-secret"


class _RecordingServer:
    """Loopback endpoint that records what each request carries, then rejects it."""

    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        recorded = self.requests

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    model = json.loads(body).get("model")
                except (ValueError, AttributeError):
                    model = None
                credential = self.headers.get("x-api-key") or self.headers.get("Authorization")
                recorded.append(
                    {
                        "path": self.path,
                        "model": model,
                        "credential": credential,
                        "other_workspace_header": self.headers.get(_OTHER_WORKSPACE_HEADER),
                    }
                )
                payload = json.dumps(
                    {
                        "type": "error",
                        "error": {"type": "invalid_request_error", "message": "recorded"},
                    }
                ).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST

            def log_message(self, *args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> _RecordingServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def claude_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[_RecordingServer, _RecordingServer, Path, Path, Path]]:
    """Isolated Claude Code and Omnigent config with loopback workspaces A and B."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_dir = tmp_path / "claude-config"
    config_dir.mkdir()
    for key in tuple(os.environ):
        if key.startswith(("ANTHROPIC_", "AWS_", "CLAUDE_CODE_", "DATABRICKS_")) or (
            key == "CLAUDECODE"
        ):
            monkeypatch.delenv(key)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DISABLE_AUTOUPDATER", "1")
    monkeypatch.setenv("DISABLE_TELEMETRY", "1")
    monkeypatch.setenv("DISABLE_ERROR_REPORTING", "1")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "bridges")
    monkeypatch.chdir(workspace)
    (config_dir / ".claude.json").write_text(
        json.dumps(
            {
                "hasCompletedOnboarding": True,
                "projects": {str(workspace): {"hasTrustDialogAccepted": True}},
            }
        )
    )
    with _RecordingServer() as workspace_a, _RecordingServer() as workspace_b:
        config_home = isolate_provider_resolution(monkeypatch, tmp_path / "omnigent-config")
        yield workspace_a, workspace_b, workspace, config_dir, config_home


def _resolve_conflicting_config(
    config_home: Path,
    config_dir: Path,
    provider: dict[str, object],
    user_settings: dict[str, object],
) -> claude_native.ClaudeNativeUcodeConfig:
    """Configure workspace A in Omnigent and conflicting user settings in Claude Code."""
    (config_home / "config.yaml").write_text(
        yaml.safe_dump({"providers": {"workspace-a": provider}})
    )
    (config_dir / "settings.json").write_text(json.dumps(user_settings))
    claude_config = claude_native.resolve_native_claude_config(spec=None)
    assert claude_config is not None
    return claude_config


def _run_terminal_turn(
    claude_config: claude_native.ClaudeNativeUcodeConfig, workspace: Path
) -> str:
    """Run one print-mode turn through the native terminal launch spec."""
    bridge_dir = bridge.prepare_bridge_dir(uuid.uuid4().hex, workspace=workspace)
    request = claude_native._claude_terminal_request(
        ("-p", "Say hello.", "--max-turns", "1", "--output-format", "json"),
        command="claude",
        bridge_dir=bridge_dir,
        claude_config=claude_config,
    )
    spec = request["spec"]
    result = subprocess.run(
        [spec["command"], *spec["args"]],
        env={**os.environ, **spec["env"]},
        cwd=spec["cwd"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return f"returncode={result.returncode} stderr={result.stderr[-2000:]}"


@pytest.fixture
def workspaces(
    claude_home: tuple[_RecordingServer, _RecordingServer, Path, Path, Path],
) -> tuple[_RecordingServer, _RecordingServer, claude_native.ClaudeNativeUcodeConfig, Path]:
    """Workspace A configured in Omnigent; workspace B pinned in Claude Code's user settings."""
    workspace_a, workspace_b, workspace, config_dir, config_home = claude_home
    claude_config = _resolve_conflicting_config(
        config_home,
        config_dir,
        {
            "kind": "key",
            "default": ["anthropic"],
            "anthropic": {
                "base_url": workspace_a.url,
                "api_key": "token-workspace-a",
                "models": {"default": "claude-sonnet-4-20250514"},
            },
        },
        {
            "env": {
                "ANTHROPIC_BASE_URL": workspace_b.url,
                "ANTHROPIC_DEFAULT_OPUS_MODEL": "ws-b-opus",
                "ANTHROPIC_DEFAULT_SONNET_MODEL": "ws-b-sonnet",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": "ws-b-haiku",
                "ANTHROPIC_CUSTOM_HEADERS": f"{_OTHER_WORKSPACE_HEADER}: secret-b",
            },
            "apiKeyHelper": "printf %s token-workspace-b",
        },
    )
    assert claude_config.env["ANTHROPIC_BASE_URL"] == workspace_a.url
    return workspace_a, workspace_b, claude_config, workspace


async def test_picker_catalog_omits_models_pinned_by_another_workspace(workspaces) -> None:
    _, _, claude_config, _ = workspaces

    rows = await claude_native.claude_model_catalog(claude_config)

    assert rows is not None
    listed = {str(row.get("model")) for row in rows}
    assert "claude-sonnet-4-20250514" in listed, f"picker rows: {rows}"
    assert not listed & _OTHER_WORKSPACE_MODELS, f"picker rows: {rows}"


def test_terminal_launch_sends_turns_to_configured_provider(workspaces) -> None:
    workspace_a, workspace_b, claude_config, workspace = workspaces

    diagnostics = _run_terminal_turn(claude_config, workspace)

    assert workspace_a.requests, f"workspace B received: {workspace_b.requests}; {diagnostics}"
    assert not workspace_b.requests, f"workspace B received: {workspace_b.requests}"
    assert {request["credential"] for request in workspace_a.requests} == {"token-workspace-a"}, (
        f"workspace A received: {workspace_a.requests}"
    )
    models = {request["model"] for request in workspace_a.requests}
    assert "claude-sonnet-4-20250514" in models, f"workspace A received: {workspace_a.requests}"
    assert not models & _OTHER_WORKSPACE_MODELS, f"workspace A received: {workspace_a.requests}"
    assert not any(request["other_workspace_header"] for request in workspace_a.requests), (
        f"workspace A received: {workspace_a.requests}"
    )


def test_bedrock_terminal_launch_keeps_the_configured_bearer(
    claude_home: tuple[_RecordingServer, _RecordingServer, Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_a, workspace_b, workspace, config_dir, config_home = claude_home
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    claude_config = _resolve_conflicting_config(
        config_home,
        config_dir,
        {
            "kind": "bedrock",
            "default": ["anthropic"],
            "anthropic": {
                "base_url": workspace_a.url,
                "api_key": "bedrock-token-a",
                "models": {"default": "us.anthropic.claude-sonnet-4-20250514-v1:0"},
            },
        },
        {
            "env": {
                "CLAUDE_CODE_USE_BEDROCK": "1",
                "ANTHROPIC_BEDROCK_BASE_URL": workspace_b.url,
                "AWS_BEARER_TOKEN_BEDROCK": "bedrock-token-b",
            },
        },
    )
    assert claude_config.env["ANTHROPIC_BEDROCK_BASE_URL"] == workspace_a.url

    diagnostics = _run_terminal_turn(claude_config, workspace)

    assert workspace_a.requests, f"workspace B received: {workspace_b.requests}; {diagnostics}"
    assert not workspace_b.requests, f"workspace B received: {workspace_b.requests}"
    assert {request["credential"] for request in workspace_a.requests} == {
        "Bearer bedrock-token-a"
    }, f"workspace A received: {workspace_a.requests}"
