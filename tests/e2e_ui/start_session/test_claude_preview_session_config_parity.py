"""E2E: Claude's pre-session picker and the created session resolve one config.

With a global ``auth: {type: databricks, profile: ...}`` block (written by
``omnigent setup``), the New Chat preview resolves the profile's ucode/gateway
launch config, so the picker offers the Databricks workspace's Claude catalog.
The generated claude-native session wrapper carries no profile of its own, so
the created session must resolve the same global auth: its catalog, model rows,
and default must match what the preview showed. A second scenario exports an
ambient ``DATABRICKS_HOST`` pointing at a different workspace: the named
profile must keep model discovery pinned to its own workspace.

The rig boots a real ``omnigent server`` + ``omnigent host`` with an isolated
``$HOME`` carrying a ``.databrickscfg`` profile pinned to a local fake
Databricks workspace, ucode state for that workspace's Claude gateway, the
global databricks ``auth:`` block, and a scripted ``claude`` CLI double whose
``/model`` picker follows the launch environment exactly like the real CLI:
gateway model pins when ``ANTHROPIC_BASE_URL`` is set, the subscription
aliases otherwise. The browser drives the real SPA.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
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

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="boots POSIX server/host daemons with a scripted claude CLI"
)

_SERVER_HEALTH_TIMEOUT_S = 90.0
_HOST_READY_TIMEOUT_S = 120.0
_PICKER_WARMUP_TIMEOUT_S = 120.0
_SESSION_PICKER_TIMEOUT_S = 120.0

#: The profile workspace's gateway catalog (what ucode pins into the launch env).
_WS_PROFILE_MODELS = ("claude-opus-5", "claude-sonnet-5")
#: A different workspace an ambient DATABRICKS_HOST must not leak into discovery.
_WS_AMBIENT_MODELS = ("claude-haiku-5",)

_GATEWAY_ROWS = {"opus": "Opus 5", "sonnet": "Sonnet 5"}
_GATEWAY_DEFAULT_ID = "opus"

# A scripted Claude Code double. Its /model picker follows the launch env the
# way the real CLI does: with ANTHROPIC_BASE_URL set it offers the gateway
# model ids pinned via ANTHROPIC_DEFAULT_*_MODEL, otherwise the subscription
# aliases of the CLI's own login.
_CLAUDE_STUB = '''#!/usr/bin/env python3
"""Scripted Claude Code double: the /model picker follows the launch env."""

import json
import os
import sys
import time

ARGS = sys.argv[1:]

SUBSCRIPTION = {
    "sonnet": ("claude-sonnet-4-5-20250929", "Sonnet 4.5"),
    "opus": ("claude-opus-4-1-20250805", "Opus 4.1"),
    "haiku": ("claude-haiku-4-5-20251001", "Haiku 4.5"),
}
TIER_ENV = {
    "opus": "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "haiku": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
}
GATEWAY_LABELS = {
    "databricks-claude-opus-5": "Opus 5",
    "databricks-claude-sonnet-5": "Sonnet 5",
    "databricks-claude-haiku-5": "Haiku 5",
}

if os.environ.get("ANTHROPIC_BASE_URL"):
    RESOLUTIONS = {}
    for tier in ("opus", "sonnet", "haiku"):
        pinned = os.environ.get(TIER_ENV[tier])
        if pinned:
            RESOLUTIONS[tier] = (pinned, GATEWAY_LABELS.get(pinned, pinned))
else:
    RESOLUTIONS = dict(SUBSCRIPTION)
DEFAULT_ALIAS = next(iter(RESOLUTIONS))

PICKER_MODELS = [
    {
        "value": "default",
        "resolvedModel": RESOLUTIONS[DEFAULT_ALIAS][0],
        "displayName": "Default (recommended)",
    }
] + [
    {"value": alias, "resolvedModel": model, "displayName": label}
    for alias, (model, label) in RESOLUTIONS.items()
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
    model, label = RESOLUTIONS.get(alias or DEFAULT_ALIAS, (alias or "?", alias or "?"))
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
            "Usage: /model <name>. Available: "
            + ", ".join(RESOLUTIONS)
            + ", default, or a full model ID."
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
        model, _ = RESOLUTIONS.get(alias or DEFAULT_ALIAS, (alias or "?", alias or "?"))
        emit({
            "type": "system",
            "subtype": "init",
            "session_id": "stub-session",
            "model": model,
            "tools": [],
        })
        emit({"type": "result", "subtype": "success", "is_error": False, "result": "ok"})
    raise SystemExit(0)

print("stub claude TUI ready")
time.sleep(3600)
'''


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


def _subprocess_pythonpath() -> str:
    return os.pathsep.join(
        [
            str(_REPO_ROOT),
            str(_REPO_ROOT / "sdks" / "python-client"),
            str(_REPO_ROOT / "sdks" / "ui"),
        ]
    )


def _sanitized_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(
            ("OMNIGENT_RUNNER", "OMNIGENT_PROCESS", "ANTHROPIC_", "OPENAI_")
        ) or key.startswith(("DATABRICKS_", "CLAUDE_")):
            env.pop(key)
    for key in ("CLAUDECODE", "RUNNER_SERVER_URL", "OMNIGENT", "CODEX_HOME"):
        env.pop(key, None)
    env["PYTHONPATH"] = _subprocess_pythonpath()
    return env


def _start_fake_workspace(bare_model_ids: tuple[str, ...]) -> tuple[str, Callable[[], None]]:
    """Serve the two Databricks discovery listings for a fake workspace."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
            path = self.path.split("?", 1)[0]
            if path == "/api/2.1/unity-catalog/model-services":
                body: dict[str, object] = {
                    "model_services": [
                        {"name": f"model-services/system.ai.{model_id}"}
                        for model_id in bare_model_ids
                    ]
                }
            elif path == "/ai-gateway/anthropic/v1/models":
                body = {"data": [{"id": f"databricks-{model_id}"} for model_id in bare_model_ids]}
            else:
                self.send_response(404)
                self.end_headers()
                return
            payload = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}", server.shutdown


def _write_databricks_home(home: Path, workspace_url: str) -> None:
    (home / ".databrickscfg").write_text(
        f"[oss]\nhost = {workspace_url}\ntoken = dapi-fake-profile-token\n"
    )
    ucode_dir = home / ".ucode"
    ucode_dir.mkdir(parents=True, exist_ok=True)
    gateway = f"{workspace_url}/ai-gateway/anthropic"
    (ucode_dir / "state.json").write_text(
        json.dumps(
            {
                "state_version": 1,
                "current_workspace": workspace_url,
                "workspaces": {
                    workspace_url: {
                        "workspace": workspace_url,
                        "claude_models": {
                            "opus": "databricks-claude-opus-5",
                            "sonnet": "databricks-claude-sonnet-5",
                        },
                        "base_urls": {"claude": gateway},
                        "available_tools": ["claude"],
                        "agents": {
                            "claude": {
                                "model": "databricks-claude-opus-5",
                                "base_url": gateway,
                                "auth_command": "echo fake-gateway-token",
                                "auth_refresh_interval_ms": 900000,
                                "env": {"ANTHROPIC_BASE_URL": gateway},
                            }
                        },
                    }
                },
            }
        )
    )


@dataclass
class ClaudeParityRig:
    """A booted server + host whose Claude CLI carries the reported configuration."""

    base_url: str
    host_id: str
    server_log: Path
    host_log: Path

    def log_tail(self) -> str:
        parts = []
        for path in (self.server_log, self.host_log):
            if path.exists():
                parts.append(f"--- {path.name} ---\n{path.read_text(errors='replace')[-3000:]}")
        return "\n".join(parts)


@contextlib.contextmanager
def _booted_rig(root: Path, *, ambient_workspace: bool) -> Iterator[ClaudeParityRig]:
    home = root / "home"
    home.mkdir()
    stub_bin = root / "stub-bin"
    stub_bin.mkdir()
    stub = stub_bin / "claude"
    stub.write_text(_CLAUDE_STUB)
    stub.chmod(0o755)

    profile_ws, stop_profile_ws = _start_fake_workspace(_WS_PROFILE_MODELS)
    stoppers = [stop_profile_ws]
    _write_databricks_home(home, profile_ws)

    host_config_home = root / "host-config-home"
    host_config_home.mkdir()
    (host_config_home / "config.yaml").write_text("auth:\n  type: databricks\n  profile: oss\n")

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
    if ambient_workspace:
        ambient_ws, stop_ambient_ws = _start_fake_workspace(_WS_AMBIENT_MODELS)
        stoppers.append(stop_ambient_ws)
        host_env["DATABRICKS_HOST"] = ambient_ws
        host_env["DATABRICKS_TOKEN"] = "dapi-fake-ambient-token"
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
                readiness = row.get("configured_harnesses") or {}
                if readiness.get("claude-native") is True:
                    return str(row["host_id"])
            return None

        host_id = _wait_for(
            _host_ready, _HOST_READY_TIMEOUT_S, "the rig host to register with claude ready"
        )

        yield ClaudeParityRig(
            base_url=base_url,
            host_id=str(host_id),
            server_log=server_log,
            host_log=host_log,
        )
    finally:
        _stop(host)
        _stop(server)
        for stop in stoppers:
            stop()
        for handle in logs:
            handle.close()


@pytest.fixture(scope="module")
def parity_rig(
    built_spa: None, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[ClaudeParityRig]:
    with _booted_rig(tmp_path_factory.mktemp("claude_parity_rig"), ambient_workspace=False) as rig:
        yield rig


@pytest.fixture(scope="module")
def ambient_rig(
    built_spa: None, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[ClaudeParityRig]:
    with _booted_rig(tmp_path_factory.mktemp("claude_ambient_rig"), ambient_workspace=True) as rig:
        yield rig


_MODEL_ROW_PREFIX = "new-chat-landing-agent-model-"
# Non-catalog controls sharing the row testid prefix.
_MODEL_ROW_SENTINELS = ("smart-routing", "default", "search", "value")


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


_MODELS_SECTION_TESTID = "new-chat-landing-agent-models"


def _expand_agent_config(page: Page, label: str) -> None:
    """Open the selected agent row's config flyout with the keyboard.

    ArrowRight is radix's canonical submenu-open key; a pointer click acts as
    row selection and closes the root menu instead.
    """
    row = page.get_by_role("menuitem", name=label, exact=True).first
    row.press("ArrowRight", timeout=5_000)


def _landing_model_rows(
    page: Page, rig: ClaudeParityRig, agent_label: str
) -> list[dict[str, str]]:
    """Read the selected agent's model rows from the landing agent dropdown."""
    deadline = time.monotonic() + _PICKER_WARMUP_TIMEOUT_S
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
            rows.append(
                {
                    "id": row_id,
                    "text": " ".join((option.inner_text() or "").split()),
                    "checked": option.get_attribute("aria-checked") or "",
                }
            )
        if rows:
            return rows
    raise AssertionError(
        f"the landing agent menu showed no model rows within {_PICKER_WARMUP_TIMEOUT_S:.0f}s\n"
        f"{rig.log_tail()}"
    )


def _create_claude_session(page: Page) -> str:
    landing_input = page.get_by_test_id("new-chat-landing-input")
    expect(landing_input).to_be_visible(timeout=30_000)
    landing_input.fill("hello — which models does this session offer?")
    landing_input.press("Enter")
    # The SPA first shows an optimistic /c/temp:<draft> URL; wait until the
    # server-created session's real id replaces it.
    page.wait_for_url(re.compile(r"/c/(?!temp:)"), timeout=90_000)
    return page.url.rsplit("/c/", 1)[1].split("?", 1)[0]


def _session_model_rows(page: Page, rig: ClaudeParityRig) -> list[dict[str, str]]:
    """Read the created session's model rows from the composer config menu."""
    deadline = time.monotonic() + _SESSION_PICKER_TIMEOUT_S
    while time.monotonic() < deadline:
        gear = page.get_by_test_id("composer-config-gear")
        expect(gear).to_be_visible(timeout=30_000)
        with contextlib.suppress(AssertionError, PlaywrightError):
            if not page.get_by_test_id("composer-agent-edit").is_visible():
                gear.click()
            page.get_by_test_id("composer-agent-edit").click(timeout=5_000)
        page.wait_for_timeout(500)
        rows: list[dict[str, str]] = []
        for option in page.locator('[role="menuitemcheckbox"][data-model-id]').all():
            text = " ".join((option.inner_text() or "").split())
            if text.endswith("(current)"):
                continue
            rows.append(
                {
                    "id": option.get_attribute("data-model-id") or "",
                    "text": text,
                    "checked": option.get_attribute("aria-checked") or "",
                }
            )
        if rows:
            return rows
        page.keyboard.press("Escape")
        page.keyboard.press("Escape")
        page.wait_for_timeout(2_000)
    raise AssertionError(
        f"the session composer showed no model rows within {_SESSION_PICKER_TIMEOUT_S:.0f}s\n"
        f"{rig.log_tail()}"
    )


def _session_default_model(base_url: str, session_id: str) -> str | None:
    """The default-marked row of the session snapshot's model options."""
    deadline = time.monotonic() + _SESSION_PICKER_TIMEOUT_S
    options: list[dict[str, object]] = []
    while time.monotonic() < deadline:
        snapshot = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10).json()
        options = snapshot.get("model_options") or []
        if options:
            break
        time.sleep(2.0)
    for row in options:
        if row.get("isDefault"):
            return str(row.get("id"))
    return None


def test_created_session_offers_the_previewed_claude_catalog(
    page: Page, parity_rig: ClaudeParityRig
) -> None:
    """The session created from New Chat must offer the previewed catalog.

    Under a global databricks ``auth:`` block the New Chat preview resolves the
    profile's ucode/gateway config and offers the workspace's Claude catalog
    (Opus 5 / Sonnet 5, Opus 5 default). The generated session wrapper is
    profile-less; it must resolve the same global auth rather than falling back
    to Claude's own login, so the session's model rows and default must match
    the preview.
    """
    page.goto(parity_rig.base_url)
    expect(page.get_by_test_id("new-chat-landing-input")).to_be_visible(timeout=30_000)
    _pick_agent(page, "Claude Code")

    preview_rows = _landing_model_rows(page, parity_rig, "Claude Code")
    preview_ids = {row["id"] for row in preview_rows}
    assert preview_ids == set(_GATEWAY_ROWS), (
        f"rig self-check: the New Chat preview must offer the profile workspace's gateway "
        f"catalog {sorted(_GATEWAY_ROWS)}, got {sorted(preview_ids)}\n{parity_rig.log_tail()}"
    )
    for row in preview_rows:
        assert _GATEWAY_ROWS[row["id"]] in row["text"], (
            f"rig self-check: preview row {row['id']!r} must be labelled "
            f"{_GATEWAY_ROWS[row['id']]!r}, got {row['text']!r}"
        )
    preview_default = [row["id"] for row in preview_rows if row["checked"] == "true"]
    assert preview_default == [_GATEWAY_DEFAULT_ID], (
        f"rig self-check: the preview must mark the gateway default "
        f"{_GATEWAY_DEFAULT_ID!r}, got {preview_default}"
    )
    page.keyboard.press("Escape")

    session_id = _create_claude_session(page)
    session_rows = _session_model_rows(page, parity_rig)
    session_ids = {row["id"] for row in session_rows}

    assert session_ids == preview_ids, (
        f"the created Claude session offers {sorted(session_ids)} but the New Chat preview "
        f"offered {sorted(preview_ids)}: the profile-less session wrapper resolved a different "
        f"auth/catalog than the pre-session preview (global databricks auth was dropped at "
        f"launch)\nsession rows: {session_rows}\npreview rows: {preview_rows}"
    )
    session_labels = {row["id"]: row["text"] for row in session_rows}
    for row_id, label in _GATEWAY_ROWS.items():
        assert label in session_labels[row_id], (
            f"the session's {row_id!r} row reads {session_labels[row_id]!r}; the preview "
            f"offered {label!r} — the session resolved a different model catalog"
        )
    session_default = _session_default_model(parity_rig.base_url, session_id)
    assert session_default == _GATEWAY_DEFAULT_ID, (
        f"the created session's default model row is {session_default!r} but the preview "
        f"marked {_GATEWAY_DEFAULT_ID!r} as the default"
    )


def test_preview_discovery_stays_on_the_profile_workspace(
    page: Page, ambient_rig: ClaudeParityRig
) -> None:
    """An ambient DATABRICKS_HOST must not redirect the preview's discovery.

    The global auth names profile ``oss`` (workspace serving Opus 5 /
    Sonnet 5). The host process inherits an ambient ``DATABRICKS_HOST``
    pointing at a different workspace that serves only Haiku 5. The New Chat
    preview must keep the named profile's workspace catalog.
    """
    page.goto(ambient_rig.base_url)
    expect(page.get_by_test_id("new-chat-landing-input")).to_be_visible(timeout=30_000)
    _pick_agent(page, "Claude Code")

    rows = _landing_model_rows(page, ambient_rig, "Claude Code")
    row_ids = {row["id"] for row in rows}
    assert row_ids == set(_GATEWAY_ROWS), (
        f"the New Chat Claude picker offers {sorted(row_ids)} instead of the profile "
        f"workspace's catalog {sorted(_GATEWAY_ROWS)}: an ambient DATABRICKS_HOST redirected "
        f"model discovery to another workspace\nrows: {rows}\n{ambient_rig.log_tail()}"
    )
