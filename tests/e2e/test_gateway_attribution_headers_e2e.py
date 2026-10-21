"""Verify real CLIs consume Omnigent's gateway attribution configuration.

Generate configuration for a genuine gateway URL, then change only the network
destination to a local recorder backed by the existing mock model server.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import tomlkit

from omnigent.harnesses.claude_native.main import (
    _provider_config_for_native_claude,
    build_native_claude_terminal_env,
)
from omnigent.harnesses.opencode_native.provider import (
    OpenCodeGatewayResolution,
    build_opencode_provider_config,
    write_opencode_provider_config,
)
from omnigent.harnesses.pi_native.credentials import PiProviderConfig
from omnigent.inner.codex_executor import (
    _databricks_codex_config_overrides,
    materialize_codex_provider_config,
)
from omnigent.inner.pi_executor import _build_models_json
from omnigent.onboarding.ambient import CLAUDE_CODE_MANAGED_SETTINGS_PATHS
from omnigent.onboarding.provider_config import load_providers
from tests.e2e.conftest import set_fallback_mock_llm

_ORIGIN = "https://workspace.ai-gateway.cloud.databricks.com"
_MODEL = "gpt-5.4"
_PROMPT = "ATTRIBUTION_WIRE_PROMPT"
_REPLY = "ATTRIBUTION_WIRE_OK"
_AGENT_HEADER = "Databricks-Custom-Coding-Agent"
_SURFACES_HEADER = "Databricks-Custom-Client-Surfaces"


@pytest.fixture
def recording_gateway(
    isolated_mock_llm_server_url: str,
) -> Iterator[tuple[str, list[tuple[dict[str, list[str]], bytes]]]]:
    requests: list[tuple[dict[str, list[str]], bytes]] = []
    set_fallback_mock_llm(isolated_mock_llm_server_url, key="default", text=_REPLY)

    class Recorder(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.respond(200, "application/json", b'{"models":[]}')

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append(
                (
                    {
                        name: self.headers.get_all(name) or []
                        for name in (_AGENT_HEADER, _SURFACES_HEADER)
                    },
                    body,
                )
            )
            response = httpx.post(
                isolated_mock_llm_server_url + self.path,
                content=body,
                headers={"Content-Type": "application/json"},
                trust_env=False,
                timeout=15,
            )
            self.respond(
                response.status_code,
                response.headers.get("Content-Type", "application/json"),
                response.content,
            )

        def respond(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Recorder) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}/v1", requests
        finally:
            server.shutdown()
            thread.join(timeout=5)


@pytest.mark.posix_only
@pytest.mark.timeout(90)
@pytest.mark.parametrize(
    ("coding_cli", "config_source"),
    [
        ("codex", "codex"),
        ("claude", "claude-native"),
        ("pi", "pi-native"),
        ("pi", "pi"),
        ("opencode", "opencode"),
    ],
    ids=["codex", "claude-native", "pi-native", "pi-sdk", "opencode"],
)
def test_gateway_headers_reach_real_cli_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recording_gateway: tuple[str, list[tuple[dict[str, list[str]], bytes]]],
    coding_cli: str,
    config_source: str,
) -> None:
    executable = shutil.which(coding_cli)
    if executable is None:
        pytest.skip(f"requires the {coding_cli} CLI")
    policy_paths = (
        (Path("/etc/codex/managed_config.toml"),)
        if coding_cli == "codex"
        else CLAUDE_CODE_MANAGED_SETTINGS_PATHS
        if coding_cli == "claude"
        else ()
    )
    if any(path.is_file() and path.read_text().strip() for path in policy_paths):
        pytest.skip("requires clean system config so machine policy cannot reroute test calls")
    destination, requests = recording_gateway
    state_path = tmp_path / "ug-state.json"
    state_path.write_text(json.dumps({"workspaces": {_ORIGIN: {"workspace": _ORIGIN}}}))
    monkeypatch.setattr("omnigent.onboarding.ucode_state._STATE_PATH", state_path)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "CODEX_HOME": str(tmp_path / "codex"),
        "PI_CODING_AGENT_DIR": str(tmp_path / "pi"),
        "PI_OFFLINE": "1",
        "PI_TELEMETRY": "0",
        "OPENCODE_DISABLE_MODELS_FETCH": "true",
        "OPENCODE_DISABLE_AUTOUPDATE": "true",
    }
    if coding_cli == "codex":
        codex_home = Path(env["CODEX_HOME"])
        overrides = materialize_codex_provider_config(
            codex_home,
            _databricks_codex_config_overrides(
                model=_MODEL, base_url=_ORIGIN + "/v1", auth_command="printf synthetic-key"
            ),
            include_managed_config=False,
        )
        path = codex_home / "config.toml"
        config = tomlkit.parse(path.read_text())
        config["model_providers"]["omnigent_databricks"]["base_url"] = destination
        path.write_text(tomlkit.dumps(config))
        args = ["exec", "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only"]
        for override in overrides:
            args.extend(("-c", override))
    elif coding_cli == "claude":
        entry = load_providers(
            {
                "providers": {
                    "test": {
                        "kind": "gateway",
                        "anthropic": {
                            "base_url": _ORIGIN,
                            "auth_command": "printf synthetic-key",
                            "models": {"default": "claude-sonnet-4-6"},
                        },
                    }
                }
            }
        )["test"]
        launch = _provider_config_for_native_claude(entry)
        assert launch is not None
        env.update(build_native_claude_terminal_env(launch))
        env["ANTHROPIC_BASE_URL"] = destination.removesuffix("/v1")
        args = [
            "--print",
            "--model",
            "claude-sonnet-4-6",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--settings",
            json.dumps({"apiKeyHelper": launch.api_key_helper}),
        ]
    elif coding_cli == "pi":
        provider_id = "omnigent" if config_source == "pi-native" else "databricks-openai"
        models = (
            PiProviderConfig(
                provider_id=provider_id,
                base_url=_ORIGIN + "/v1",
                api="openai-responses",
                model=_MODEL,
                api_key="synthetic-key",
                auth_header=True,
            ).to_models_config()
            if config_source == "pi-native"
            else _build_models_json(
                _ORIGIN, "synthetic-key", base_urls={"openai": _ORIGIN + "/v1"}, model=_MODEL
            )
        )
        for provider in models["providers"].values():
            provider["baseUrl"] = destination
        pi_home = Path(env["PI_CODING_AGENT_DIR"])
        pi_home.mkdir()
        (pi_home / "models.json").write_text(json.dumps(models))
        args = [
            "--print",
            "--no-session",
            "--no-extensions",
            "--no-skills",
            "--no-context-files",
            "--no-tools",
            "--provider",
            provider_id,
            "--model",
            _MODEL,
        ]
    else:
        resolution = OpenCodeGatewayResolution(
            base_url=_ORIGIN + "/v1", api_key="synthetic-key", model_id=_MODEL
        )
        path = write_opencode_provider_config(
            Path(env["XDG_CONFIG_HOME"]), build_opencode_provider_config(resolution)
        )
        config = json.loads(path.read_text())
        config["provider"][resolution.provider_id]["options"]["baseURL"] = destination
        path.write_text(json.dumps(config))
        args = ["run", "--format", "json", "--model", resolution.qualified_model]
    completed = subprocess.run(
        [executable, *args, _PROMPT],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert _REPLY in completed.stdout, completed.stdout[-2000:]
    prompt_requests = [headers for headers, body in requests if _PROMPT.encode() in body]
    assert prompt_requests, "the user prompt never reached the recording gateway"
    for headers in prompt_requests:
        assert headers[_AGENT_HEADER] == ["claude-code" if coding_cli == "claude" else coding_cli]
        assert headers[_SURFACES_HEADER] == ["omnigent, ug"]
