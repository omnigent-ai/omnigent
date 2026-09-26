"""E2E: the Claude picker offers a generic Anthropic gateway's canonical models.

A generic Anthropic-passthrough gateway (a LiteLLM proxy, say) serves the bare
canonical ``claude-*`` ids: its ``/v1/models`` lists them and it routes
``POST /v1/messages`` for them. When such a gateway is the configured claude
provider (a ``providers:`` entry with ``kind: gateway`` serving the
``anthropic`` family), the New Chat Claude picker must offer those models.

The catalog composition instead classifies the endpoint by hostname alone —
any ``ANTHROPIC_BASE_URL`` host that is not ``anthropic.com`` is treated as a
namespaced gateway that would reject bare ``claude-*`` ids — so every probe
row resolving to a canonical id is dropped, the launch catalog persists empty,
and the picker shows "Models unavailable" for an endpoint that demonstrably
serves those exact ids.

The rig boots a real ``omnigent server`` + ``omnigent host`` with an isolated
``$HOME`` / ``OMNIGENT_CONFIG_HOME``. The gateway is a live local HTTP double
serving ``/v1/models`` (bare canonical ids) and ``/v1/messages`` (success).
The Claude CLI is a scripted double whose picker resolves the standard aliases
to the same canonical ids the real CLI prints; every product layer between the
user and the drop — provider resolution, the launch-catalog probe, the shared
catalog store, the host model-options tunnel, and the SPA's New Chat landing —
is real. The browser drives the real SPA.

While the bug is live the models flyout settles on "Models unavailable" and
this test FAILS; once the catalog trusts the gateway's own model listing, the
picker offers the canonical models and the test passes.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, expect

_REPO_ROOT = Path(__file__).resolve().parents[3]

_SERVER_HEALTH_TIMEOUT_S = 90.0
_HOST_READY_TIMEOUT_S = 120.0
_PICKER_SETTLE_TIMEOUT_S = 180.0

#: The bare canonical ids the gateway serves — what ``/v1/models`` lists and
#: what the CLI double's picker aliases resolve to.
_GATEWAY_MODELS = (
    "claude-sonnet-4-5-20250929",
    "claude-opus-4-1-20250805",
    "claude-haiku-4-5-20251001",
)
_GATEWAY_DEFAULT_MODEL = _GATEWAY_MODELS[0]
_CLAUDE_PICKER_ALIASES = ("sonnet", "opus", "haiku")

# Proxy-blind client: CI forces an egress proxy via HTTP(S)_PROXY env vars
# that must not intercept loopback requests to the spawned server.
_client = httpx.Client(trust_env=False)

# A scripted Claude Code double matching the real CLI's shape for this
# journey: the structured /model picker offers the standard aliases, each
# resolving to a bare canonical Anthropic id (exactly what the real CLI
# prints under a gateway launch config).
_CLAUDE_STUB = '''#!/usr/bin/env python3
"""Scripted Claude Code double: picker aliases resolve to canonical ids."""

import json
import sys

ARGS = sys.argv[1:]

RESOLUTIONS = {
    "sonnet": ("claude-sonnet-4-5-20250929", "Sonnet 4.5"),
    "opus": ("claude-opus-4-1-20250805", "Opus 4.1"),
    "haiku": ("claude-haiku-4-5-20251001", "Haiku 4.5"),
    "default": ("claude-sonnet-4-5-20250929", "Sonnet 4.5"),
}

PICKER_MODELS = [
    {"value": "default", "resolvedModel": RESOLUTIONS["default"][0],
     "displayName": "Default (recommended)"},
    {"value": "sonnet", "resolvedModel": RESOLUTIONS["sonnet"][0], "displayName": "Sonnet 4.5"},
    {"value": "opus", "resolvedModel": RESOLUTIONS["opus"][0], "displayName": "Opus 4.1"},
    {"value": "haiku", "resolvedModel": RESOLUTIONS["haiku"][0], "displayName": "Haiku 4.5"},
]


def opt(flag):
    if flag in ARGS:
        index = ARGS.index(flag)
        if index + 1 < len(ARGS):
            return ARGS[index + 1]
    return None


def emit(payload):
    print(json.dumps(payload))


def emit_current_model(alias):
    model, label = RESOLUTIONS.get(alias or "default", (alias or "?", alias or "?"))
    emit({
        "type": "system",
        "subtype": "init",
        "session_id": "stub-session",
        "model": model,
        "tools": [],
    })
    if alias:
        text = "Current model: `" + label + "`"
    else:
        text = (
            "Current model: `" + label + "` (default)\\n\\n"
            "Usage: /model <name>. Available: sonnet, opus, haiku, default, "
            "or a full model ID."
        )
    emit({"type": "result", "subtype": "success", "is_error": False, "result": text})


if "--version" in ARGS:
    print("2.1.236 (Claude Code)")
    raise SystemExit(0)

if ARGS[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": "claudeai"}))
    raise SystemExit(0)

if "-p" in ARGS and opt("--input-format") == "stream-json":
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        event = json.loads(line)
        if event.get("type") == "control_request":
            emit({
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": event.get("request_id"),
                    "response": {"commands": [], "models": PICKER_MODELS},
                },
            })
    emit_current_model(opt("--model"))
    raise SystemExit(0)

if "-p" in ARGS:
    prompt = opt("-p") or ""
    alias = opt("--model")
    if prompt.strip().startswith("/model") or alias:
        emit_current_model(alias)
    else:
        model, _ = RESOLUTIONS.get(alias or "default", (alias or "?", alias or "?"))
        emit({
            "type": "system",
            "subtype": "init",
            "session_id": "stub-session",
            "model": model,
            "tools": [],
        })
        emit({"type": "result", "subtype": "success", "is_error": False, "result": "ok"})
    raise SystemExit(0)

print("stub claude: unsupported invocation: " + " ".join(ARGS), file=sys.stderr)
raise SystemExit(1)
'''


class _GatewayHandler(BaseHTTPRequestHandler):
    """A LiteLLM-style Anthropic passthrough: bare canonical ids, routed turns."""

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        if self.path.split("?")[0].rstrip("/").endswith("/models"):
            self._send(
                200,
                {
                    "object": "list",
                    "data": [{"id": model, "object": "model"} for model in _GATEWAY_MODELS],
                },
            )
        else:
            self._send(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length)
        if "/messages" in self.path:
            try:
                model = json.loads(raw).get("model", _GATEWAY_DEFAULT_MODEL)
            except ValueError:
                model = _GATEWAY_DEFAULT_MODEL
            self._send(
                200,
                {
                    "id": "msg_stub_1",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            )
        else:
            self._send(404, {"error": {"message": "not found"}})

    def log_message(self, *args: object) -> None:
        pass


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for(predicate: Callable[[], object], timeout_s: float, what: str) -> object:
    deadline = time.monotonic() + timeout_s
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            result = predicate()
        except Exception as exc:  # retried until the deadline
            last_exc = exc
            result = None
        if result:
            return result
        time.sleep(1.0)
    detail = f" (last error: {last_exc})" if last_exc is not None else ""
    raise AssertionError(f"timed out after {timeout_s:.0f}s waiting for {what}{detail}")


def _sanitized_env() -> dict[str, str]:
    """Ambient env with provider/runner/host leakage stripped, loopback proxy-exempt."""
    env = os.environ.copy()
    for key in list(env):
        if key.startswith(
            (
                "OMNIGENT_RUNNER",
                "OMNIGENT_PROCESS",
                "OMNIGENT_HOST",
                "ANTHROPIC_",
                "OPENAI_",
                "CLAUDE_CODE_",
                "CLAUDE_CONFIG_DIR",
                "DATABRICKS_",
                "CODEX_",
            )
        ):
            env.pop(key)
    for key in ("CLAUDECODE", "RUNNER_SERVER_URL", "OMNIGENT_REMOTE_AUTH_TOKEN"):
        env.pop(key, None)
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(_REPO_ROOT),
            str(_REPO_ROOT / "sdks" / "python-client"),
            str(_REPO_ROOT / "sdks" / "ui"),
        ]
    )
    for var in ("NO_PROXY", "no_proxy"):
        env[var] = ",".join(filter(None, [env.get(var, ""), "127.0.0.1,localhost"]))
    return env


@dataclass
class GatewayPickerRig:
    """A booted server + host whose claude provider is a passthrough gateway."""

    base_url: str
    host_id: str
    gateway_base_url: str
    server_log: Path
    host_log: Path

    def log_tail(self) -> str:
        parts = []
        for path in (self.server_log, self.host_log):
            if path.exists():
                parts.append(f"--- {path.name} ---\n{path.read_text(errors='replace')[-3000:]}")
        return "\n".join(parts)


@pytest.fixture(scope="module")
def gateway_picker_rig(
    built_spa: None,
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> Iterator[GatewayPickerRig]:
    if request.config.getoption("--ui-base-url"):
        pytest.skip("the gateway picker e2e requires an isolated spawned server")

    root = tmp_path_factory.mktemp("claude_gateway_picker_rig")
    home = root / "home"
    home.mkdir()
    stub_bin = root / "stub-bin"
    stub_bin.mkdir()
    stub = stub_bin / "claude"
    stub.write_text(_CLAUDE_STUB)
    stub.chmod(0o755)

    gateway = ThreadingHTTPServer(("127.0.0.1", 0), _GatewayHandler)
    gateway_thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    gateway_thread.start()
    gateway_base = f"http://127.0.0.1:{gateway.server_address[1]}"

    host_config_home = root / "host-config-home"
    host_config_home.mkdir()
    (host_config_home / "config.yaml").write_text(
        f"""\
providers:
  litellm:
    kind: gateway
    default: [anthropic]
    anthropic:
      base_url: "{gateway_base}"
      api_key: "sk-litellm-test"
      models:
        default: {_GATEWAY_DEFAULT_MODEL}
"""
    )

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

    host_env = _sanitized_env()
    host_env["HOME"] = str(home)
    host_env["PATH"] = f"{stub_bin}{os.pathsep}{os.environ['PATH']}"
    host_env["OMNIGENT_CONFIG_HOME"] = str(host_config_home)
    host_env["OMNIGENT_DATA_DIR"] = str(root / "host-data")
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
        listing = _client.get(f"{gateway_base}/v1/models", timeout=5).json()
        assert [row["id"] for row in listing["data"]] == list(_GATEWAY_MODELS)
        turn = _client.post(
            f"{gateway_base}/v1/messages",
            json={
                "model": _GATEWAY_DEFAULT_MODEL,
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers={"x-api-key": "sk-litellm-test"},
            timeout=5,
        )
        assert turn.status_code == 200 and turn.json()["model"] == _GATEWAY_DEFAULT_MODEL

        def _healthy() -> bool:
            if server.poll() is not None:
                raise AssertionError(
                    f"rig server exited early:\n{server_log.read_text(errors='replace')[-3000:]}"
                )
            try:
                return _client.get(f"{base_url}/health", timeout=2).status_code == 200
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
            rows = _client.get(f"{base_url}/v1/hosts", timeout=5).json().get("hosts", [])
            for row in rows:
                if row.get("status") != "online":
                    continue
                readiness = row.get("configured_harnesses") or {}
                if readiness.get("claude-native") is True:
                    return str(row["host_id"])
            return None

        host_id = _wait_for(
            _host_ready, _HOST_READY_TIMEOUT_S, "the rig host to register with claude ready"
        )

        yield GatewayPickerRig(
            base_url=base_url,
            host_id=str(host_id),
            gateway_base_url=gateway_base,
            server_log=server_log,
            host_log=host_log,
        )
    finally:
        _stop(host)
        _stop(server)
        for handle in logs:
            handle.close()
        gateway.shutdown()
        gateway_thread.join(timeout=5)


_MODEL_ROW_PREFIX = "new-chat-landing-agent-model-"
# Non-catalog controls sharing the row testid prefix: the smart-routing
# toggle, the "Harness default" sentinel, the search box, and the trigger's
# current-model label (`…-agent-model-value`).
_MODEL_ROW_SENTINELS = ("smart-routing", "default", "search", "value")
_MODELS_SECTION_TESTID = "new-chat-landing-agent-models"


def _open_agent_menu(page: Page) -> None:
    select = page.get_by_test_id("new-chat-landing-agent-select")
    expect(select).to_be_visible(timeout=30_000)
    if page.get_by_role("menu").count() == 0:
        select.click()
        expect(page.get_by_role("menu").first).to_be_visible(timeout=10_000)


def _pick_agent(page: Page, label: str) -> None:
    _open_agent_menu(page)
    for item in page.get_by_role("menuitem").all():
        if label.lower() in item.inner_text().lower():
            item.click()
            page.wait_for_timeout(800)
            return
    raise AssertionError(f"agent {label!r} not offered on the landing screen")


def _expand_agent_config(page: Page, label: str) -> None:
    """Open the selected agent row's config flyout with the keyboard.

    ArrowRight is radix's canonical submenu-open key; a pointer click acts as
    row selection (closing the root menu) rather than reliably leaving the
    flyout open.
    """
    row = page.get_by_role("menuitem", name=label, exact=True).first
    row.press("ArrowRight", timeout=5_000)


def _read_models_section(page: Page) -> tuple[list[dict[str, str]], str]:
    """One read of the selected agent's models flyout: (rows, leading notice)."""
    rows: list[dict[str, str]] = []
    for option in page.locator(
        f'[role="menuitemcheckbox"][data-testid^="{_MODEL_ROW_PREFIX}"]'
    ).all():
        testid = option.get_attribute("data-testid") or ""
        row_id = testid[len(_MODEL_ROW_PREFIX) :]
        if row_id in _MODEL_ROW_SENTINELS:
            continue
        rows.append(
            {
                "id": row_id,
                "text": " ".join((option.inner_text() or "").split()),
            }
        )
    section = page.get_by_test_id(_MODELS_SECTION_TESTID)
    notice = " ".join((section.inner_text() or "").split()) if section.count() else ""
    return rows, notice


def _stored_model_options(rig: GatewayPickerRig) -> dict | None:
    """The host's own answer for the picker, or ``None`` while it still warms."""
    response = _client.get(
        f"{rig.base_url}/v1/hosts/{rig.host_id}/harnesses/claude-native/model-options",
        timeout=30,
    )
    if response.status_code != 200:
        return None
    return response.json()


@pytest.mark.timeout(600)
def test_claude_picker_offers_the_gateways_canonical_models(
    page: Page, gateway_picker_rig: GatewayPickerRig
) -> None:
    """A passthrough gateway's servable canonical models reach the Claude picker.

    Journey: configure a generic Anthropic-passthrough gateway as the claude
    provider, bring the machine online as a host, open the app's New Chat
    landing, pick the Claude Code agent, and open its Models flyout. The
    gateway's own ``/v1/models`` lists bare canonical ``claude-*`` ids and it
    routes ``/v1/messages`` for them, so the picker must offer those models —
    not "Models unavailable".
    """
    rig = gateway_picker_rig
    page.goto(rig.base_url)
    expect(page.get_by_test_id("new-chat-landing-input")).to_be_visible(timeout=30_000)
    _pick_agent(page, "Claude Code")

    rows: list[dict[str, str]] = []
    notice = ""
    catalog: dict | None = None
    deadline = time.monotonic() + _PICKER_SETTLE_TIMEOUT_S
    while time.monotonic() < deadline:
        _open_agent_menu(page)
        if page.get_by_test_id(_MODELS_SECTION_TESTID).count() == 0:
            # Menus re-render while queries settle; a miss here just retries.
            with contextlib.suppress(AssertionError, PlaywrightError):
                _expand_agent_config(page, "Claude Code")
        page.wait_for_timeout(500)
        with contextlib.suppress(PlaywrightError):
            rows, notice = _read_models_section(page)
        if rows:
            break
        catalog = _stored_model_options(rig)
        # An authoritative empty catalog plus the settled empty-state notice
        # IS the journey's outcome; keep polling only while either still warms.
        if (
            catalog is not None
            and catalog.get("models") == []
            and "Models unavailable" in notice
        ):
            break

    claude_rows = [
        row
        for row in rows
        if row["id"] in _CLAUDE_PICKER_ALIASES
        or row["id"].startswith("claude-")
        or "claude" in row["text"].lower()
    ]
    assert claude_rows, (
        "the New Chat Claude picker offers none of the configured gateway's servable "
        f"canonical models: the gateway at {rig.gateway_base_url} lists "
        f"{list(_GATEWAY_MODELS)} on /v1/models and routes /v1/messages for them, yet "
        f"the picker shows {notice or 'no models section'!r} (rows: {rows}) and the "
        f"host's claude-native model-options answer is {catalog}.\n{rig.log_tail()}"
    )
