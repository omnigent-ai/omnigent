"""E2E: the New Chat picker for Claude Code offers what the CLI's own ``/model`` picker offers
when Claude Code reaches an Anthropic passthrough gateway only through ambient
``ANTHROPIC_BASE_URL`` (no anthropic ``providers:`` entry), instead of "Models unavailable"."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, expect

from tests.e2e_ui.start_session.test_native_picker_cli_parity import (
    _MODEL_ROW_PREFIX,
    _MODEL_ROW_SENTINELS,
    _MODELS_SECTION_TESTID,
    _expand_agent_config,
    _free_port,
    _open_agent_menu,
    _pick_agent,
    _sanitized_env,
    _wait_for,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]

_SERVER_HEALTH_TIMEOUT_S = 90.0
_HOST_READY_TIMEOUT_S = 120.0
_CATALOG_WARMUP_TIMEOUT_S = 180.0
_PICKER_SETTLE_TIMEOUT_S = 60.0

_CLAUDE_AGENT_LABEL = "Claude Code"
_GATEWAY_TOKEN = "e2e-gateway-bearer"
_LITELLM_KEY = "e2e-litellm-virtual-key"

# A scripted Claude Code double for hosts without the CLI: its interactive
# picker resolves every alias to a canonical Anthropic id, as the real CLI does
# through a passthrough gateway.
_CLAUDE_STUB = '''#!/usr/bin/env python3
"""Scripted Claude Code double: the picker resolves aliases to canonical ids."""

import json
import sys

ARGS = sys.argv[1:]
DEFAULT_MODEL = "claude-opus-5-5[1m]"
PICKER_MODELS = [
    {"value": "default", "resolvedModel": DEFAULT_MODEL, "displayName": "Default (recommended)"},
    {"value": "opus[1m]", "resolvedModel": DEFAULT_MODEL, "displayName": "Opus (1M context)"},
    {"value": "sonnet", "resolvedModel": "claude-sonnet-5-5", "displayName": "Sonnet"},
    {"value": "sonnet[1m]", "resolvedModel": "claude-sonnet-5-5[1m]",
     "displayName": "Sonnet 5.5 (1M context)"},
    {"value": "haiku", "resolvedModel": "claude-haiku-4-5-20251001", "displayName": "Haiku"},
]


def emit(payload):
    print(json.dumps(payload), flush=True)


def emit_current_model():
    emit({"type": "system", "subtype": "init", "session_id": "stub", "model": DEFAULT_MODEL,
          "tools": []})
    emit({"type": "result", "subtype": "success", "is_error": False,
          "result": "Current model: `Opus 5.5 (1M context) (default)`\\n\\n"
                    "Usage: /model <name>. Available: sonnet, opus, haiku, sonnet[1m], "
                    "opus[1m], default, or a full model ID."})


if "--version" in ARGS:
    print("2.1.284 (Claude Code)")
    raise SystemExit(0)

if ARGS[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": "api_key"}))
    raise SystemExit(0)

if "-p" in ARGS:
    if "--input-format" in ARGS:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            event = json.loads(line)
            if event.get("type") == "control_request":
                emit({"type": "control_response", "response": {
                    "subtype": "success", "request_id": event.get("request_id"),
                    "response": {"commands": [], "models": PICKER_MODELS}}})
    emit_current_model()
    raise SystemExit(0)

print("stub claude: unsupported invocation: " + " ".join(ARGS), file=sys.stderr)
raise SystemExit(1)'''


class _LiteLLMGatewayHandler(BaseHTTPRequestHandler):
    """A LiteLLM-shaped ``/v1/models``: wildcard routes hidden unless asked for."""

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/v1/models":
            self.send_error(404)
            return
        wildcard = parse_qs(parsed.query).get("return_wildcard_routes", ["false"])[0] == "true"
        rows = [{"id": "claude-*", "object": "model", "owned_by": "anthropic"}] if wildcard else []
        body = json.dumps({"object": "list", "data": rows}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return


def _claude_picker_aliases(claude: str, env: dict[str, str], home: Path) -> list[str]:
    """The enabled aliases the CLI's own picker offers under *env* (e.g. ``opus[1m]``)."""
    requests = [
        {"type": "control_request", "request_id": "e2e", "request": {"subtype": "initialize"}},
        {
            "type": "user",
            "message": {"role": "user", "content": "/model"},
            "parent_tool_use_id": None,
            "session_id": "default",
        },
    ]
    completed = subprocess.run(
        [
            claude,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--no-session-persistence",
        ],
        input="".join(json.dumps(request) + "\n" for request in requests),
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=str(home),
    )
    for line in completed.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") != "control_response":
            continue
        models = event.get("response", {}).get("response", {}).get("models") or []
        return [
            model["value"]
            for model in models
            if model.get("value") != "default" and model.get("disabled") is not True
        ]
    raise AssertionError(
        f"the claude CLI's picker probe returned no model list (exit {completed.returncode}):\n"
        f"{completed.stdout[-2000:]}\n{completed.stderr[-2000:]}"
    )


@dataclass
class AmbientGatewayRig:
    """A booted server + host whose Claude Code reaches a gateway via ambient env only."""

    base_url: str
    host_id: str
    gateway_url: str
    cli_picker_aliases: list[str]
    server_log: Path
    host_log: Path

    def model_options(self) -> dict[str, object]:
        """The host's pre-launch Claude answer, as the SPA fetches it."""
        response = httpx.get(
            f"{self.base_url}/v1/hosts/{self.host_id}/harnesses/claude-native/model-options",
            timeout=_CATALOG_WARMUP_TIMEOUT_S,
        )
        response.raise_for_status()
        return response.json()

    def log_tail(self) -> str:
        parts = []
        for path in (self.server_log, self.host_log):
            if path.exists():
                parts.append(f"--- {path.name} ---\n{path.read_text(errors='replace')[-3000:]}")
        return "\n".join(parts)


@pytest.fixture(scope="module", params=["installed-claude", "scripted-claude"])
def ambient_gateway_rig(
    built_spa: None, tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest
) -> Iterator[AmbientGatewayRig]:
    root = tmp_path_factory.mktemp("ambient_gateway_rig")
    home = root / "home"
    home.mkdir()
    cli_bin = root / "cli-bin"
    cli_bin.mkdir()
    if request.param == "installed-claude":
        claude = shutil.which("claude")
        if claude is None:
            pytest.skip("the 'claude' CLI is required to probe the real picker behaviour")
    else:
        stub = cli_bin / "claude"
        stub.write_text(_CLAUDE_STUB)
        stub.chmod(0o755)
        claude = str(stub)

    gateway = ThreadingHTTPServer(("127.0.0.1", 0), _LiteLLMGatewayHandler)
    gateway_thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    gateway_thread.start()
    gateway_url = f"http://127.0.0.1:{gateway.server_address[1]}"

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    server_log = root / "server.log"
    host_log = root / "host.log"
    logs = []

    server_env = _sanitized_env()
    server_env["OMNIGENT_CONFIG_HOME"] = str(root / "server-config-home")
    server_env["OMNIGENT_DATA_DIR"] = str(root / "server-data")
    server_handle = open(server_log, "w")  # noqa: SIM115 — subprocess lifetime
    logs.append(server_handle)
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omnigent.cli",
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{root / 'rig.db'}",
            "--artifact-location",
            str(root / "artifacts"),
        ],
        env=server_env,
        cwd=str(_REPO_ROOT),
        stdout=server_handle,
        stderr=subprocess.STDOUT,
    )

    # The reported host shape: Claude Code configured purely through the
    # ambient environment, omnigent's own config home empty (no providers:).
    host_env = _sanitized_env()
    host_env["HOME"] = str(home)
    host_env["PATH"] = f"{cli_bin}{os.pathsep}{os.environ['PATH']}"
    host_env["OMNIGENT_CONFIG_HOME"] = str(root / "host-config-home")
    host_env["OMNIGENT_DATA_DIR"] = str(root / "host-data")
    host_env["ANTHROPIC_BASE_URL"] = gateway_url
    host_env["ANTHROPIC_AUTH_TOKEN"] = _GATEWAY_TOKEN
    host_env["ANTHROPIC_CUSTOM_HEADERS"] = f"x-litellm-api-key: {_LITELLM_KEY}"
    host_env["DISABLE_AUTOUPDATER"] = "1"
    host_env["DISABLE_TELEMETRY"] = "1"
    host_env["DISABLE_ERROR_REPORTING"] = "1"
    host_handle = open(host_log, "w")  # noqa: SIM115 — subprocess lifetime
    logs.append(host_handle)
    host: subprocess.Popen[bytes] | None = None

    def _stop(proc: subprocess.Popen[bytes] | None) -> None:
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    try:
        cli_picker_aliases = _claude_picker_aliases(claude, host_env, home)
        assert cli_picker_aliases, "Claude Code's own picker offers no models through the gateway"

        def _healthy() -> bool:
            if server.poll() is not None:
                raise AssertionError(
                    f"rig server exited early:\n{server_log.read_text(errors='replace')[-3000:]}"
                )
            try:
                return httpx.get(f"{base_url}/health", timeout=2).status_code == 200
            except httpx.HTTPError:
                return False

        _wait_for(_healthy, _SERVER_HEALTH_TIMEOUT_S, "the rig server /health")

        host = subprocess.Popen(
            [sys.executable, "-m", "omnigent.host._daemon_entry", "--server", base_url],
            env=host_env,
            cwd=str(_REPO_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=host_handle,
        )

        def _host_ready() -> str | None:
            if host is not None and host.poll() is not None:
                raise AssertionError(
                    f"rig host exited early:\n{host_log.read_text(errors='replace')[-3000:]}"
                )
            rows = httpx.get(f"{base_url}/v1/hosts", timeout=5).json().get("hosts", [])
            for row in rows:
                if row.get("status") != "online":
                    continue
                if (row.get("configured_harnesses") or {}).get("claude-native") is True:
                    return str(row["host_id"])
            return None

        host_id = _wait_for(
            _host_ready, _HOST_READY_TIMEOUT_S, "the rig host to register with Claude Code ready"
        )

        yield AmbientGatewayRig(
            base_url=base_url,
            host_id=str(host_id),
            gateway_url=gateway_url,
            cli_picker_aliases=cli_picker_aliases,
            server_log=server_log,
            host_log=host_log,
        )
    finally:
        _stop(host)
        _stop(server)
        gateway.shutdown()
        gateway.server_close()
        for handle in logs:
            handle.close()


def _settled_model_rows(page: Page, agent_label: str) -> tuple[list[dict[str, str]], str]:
    """The settled model rows, plus the section's placeholder text when it offers none."""
    deadline = time.monotonic() + _PICKER_SETTLE_TIMEOUT_S
    notice = ""
    while time.monotonic() < deadline:
        _open_agent_menu(page)
        if page.get_by_test_id(_MODELS_SECTION_TESTID).count() == 0:
            with contextlib.suppress(AssertionError, PlaywrightError):
                _expand_agent_config(page, agent_label)
        page.wait_for_timeout(500)
        rows: list[dict[str, str]] = []
        for option in page.locator(
            f'[role="menuitemcheckbox"][data-testid^="{_MODEL_ROW_PREFIX}"]'
        ).all():
            testid = option.get_attribute("data-testid") or ""
            row_id = testid[len(_MODEL_ROW_PREFIX) :]
            if row_id in _MODEL_ROW_SENTINELS:
                continue
            rows.append({"id": row_id, "text": " ".join((option.inner_text() or "").split())})
        if rows:
            return rows, ""
        section = page.get_by_test_id(_MODELS_SECTION_TESTID)
        if section.count() == 0:
            continue
        notice = " ".join((section.first.inner_text() or "").split())
        if notice and "Loading models" not in notice:
            return [], notice
    return [], notice


def test_claude_picker_offers_the_clis_models_through_an_ambient_gateway(
    page: Page, ambient_gateway_rig: AmbientGatewayRig
) -> None:
    """The New Chat Claude Code picker offers what the CLI's own picker offers."""
    rig = ambient_gateway_rig
    # The host's boot probe fills the catalog in the background; wait for it so
    # the picker below reads a settled answer rather than a loading state.
    _wait_for(rig.model_options, _CATALOG_WARMUP_TIMEOUT_S, "the host's Claude model options")

    page.goto(rig.base_url)
    expect(page.get_by_test_id("new-chat-landing-input")).to_be_visible(timeout=30_000)
    _pick_agent(page, _CLAUDE_AGENT_LABEL)

    rows, notice = _settled_model_rows(page, _CLAUDE_AGENT_LABEL)
    offered = [row["id"] for row in rows]
    missing = [alias for alias in rig.cli_picker_aliases if alias not in offered]
    assert not missing, (
        f"the New Chat Claude Code picker offers {offered or 'no model rows'}"
        f"{f' and reads {notice!r}' if notice else ''}, but Claude Code's own /model picker "
        f"through the ambient gateway {rig.gateway_url} (ANTHROPIC_BASE_URL, no providers: "
        f"entry) offers {rig.cli_picker_aliases}; the host's model-options answer was "
        f"{json.dumps(rig.model_options())}\n{rig.log_tail()}"
    )
    trigger = page.get_by_test_id("new-chat-landing-agent-select")
    expect(trigger).not_to_contain_text("Models unavailable")
