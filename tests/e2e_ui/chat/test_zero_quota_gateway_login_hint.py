"""E2E: a zero-quota gateway 403 must not tell the user to run ``codex login``.

Regression test: when the AI gateway rejects a Codex turn with
``403 PERMISSION_DENIED`` because the user's (or endpoint's) rate limit is set
to zero, the codex-native forwarder classifies the failure as an *auth* error
(``_classify_codex_error`` treats every 403 — and any message containing
``"403"`` — as auth) and appends its re-auth remediation to the surfaced turn
error::

    unexpected status 403 Forbidden: {"error_code":"PERMISSION_DENIED",
    "message":"This user's rate limit is set to 0."}, url: .../responses ...

    If this looks like an auth issue, running `codex login` may help.

A rate limit of zero is a provisioning state, not an auth state, so the login
hint misdirects the user into re-running ``codex login`` against the same
wall.  This test drives the user's own journey — a codex-native chat turn on a
gateway whose quota is zero — to the SPA's error pill and asserts the surfaced
error (and the persisted ``last_task_error`` snapshot behind it) names the
quota rejection *without* directing the user to ``codex login``.  While the
bug is live the hint is appended and this test fails.

The rig mirrors ``headless_codex_session`` (dedicated server + runner so the
mock ``OMNIGENT_CONFIG_HOME`` / ``CODEX_HOME`` cannot leak into other tests),
with the provider config routing native Codex at a loopback stand-in for the
gateway that answers every Responses POST with the workspace's real-world
zero-quota rejection (and the models poll with the ``ENDPOINT_NOT_FOUND``
rejection observed alongside it in production).
"""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _codex_cli_supports_mocked_app_server,
    _create_native_codex_session,
    _write_mock_codex_provider_config,
)
from tests.e2e_ui.messages.test_message_render_parity import _ensure_chat_view, _send

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Boot budget for the spawned server + runner pair.
_HEALTH_TIMEOUT_S = 60.0
# The 403 is non-retryable, so the turn should fail fast; the budget covers
# the codex TUI launch + thread start on a cold runner.
_TURN_OUTCOME_TIMEOUT_S = 150.0
_ERROR_PILL = '[data-testid="error-pill"]'

# The gateway's real-world zero-quota rejection (verbatim from the report).
_ZERO_QUOTA_BODY = {
    "error_code": "PERMISSION_DENIED",
    "message": "This user's rate limit is set to 0.",
}
# The models-poll rejection production pairs with it.
_MODELS_404_BODY = {
    "error_code": "ENDPOINT_NOT_FOUND",
    "message": "codex/v1/models is not enabled for this workspace.",
}
# The stable fragment of the zero-quota rejection the surfaced error must name.
_ZERO_QUOTA_MARKER = "rate limit is set to 0"
# The misleading remediation: any instruction to run `codex login` (with or
# without backticks/formatting) on this provisioning failure is the bug.
_LOGIN_HINT_RE = re.compile(r"codex\s+login", re.IGNORECASE)


class _ZeroQuotaGateway(http.server.ThreadingHTTPServer):
    """Loopback stand-in for an AI gateway whose quota is provisioned to zero.

    Every Responses POST gets the workspace's real-world rejection shape:
    ``403 {"error_code": "PERMISSION_DENIED", "message": "This user's rate
    limit is set to 0."}``; the models poll gets the paired
    ``ENDPOINT_NOT_FOUND`` 404.
    """

    def __init__(self) -> None:
        self.post_paths_seen: list[str] = []
        super().__init__(("127.0.0.1", 0), _ZeroQuotaGatewayHandler)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _ZeroQuotaGatewayHandler(http.server.BaseHTTPRequestHandler):
    server: _ZeroQuotaGateway

    def _send_json(self, status: int, payload: dict[str, str]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        # codex polls /models on startup; production answers 404 here.
        self._send_json(404, _MODELS_404_BODY)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        self.server.post_paths_seen.append(self.path)
        self._send_json(403, _ZERO_QUOTA_BODY)

    def log_message(self, *args: object) -> None:  # keep pytest output clean
        pass


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# Proxy-blind client: CI forces an egress proxy via HTTP(S)_PROXY env vars
# that must not intercept loopback requests to the spawned server.
_client = httpx.Client(trust_env=False)

# Shared fixtures/helpers (e.g. the conftest session factory) use ambient
# ``httpx`` calls that DO trust env, so also exclude loopback from any forced
# proxy at import time.
for _var in ("NO_PROXY", "no_proxy"):
    os.environ[_var] = ",".join(filter(None, [os.environ.get(_var, ""), "127.0.0.1,localhost"]))


def _no_proxy_env() -> dict[str, str]:
    """Ambient env with loopback excluded from any forced HTTP(S) proxy."""
    env = os.environ.copy()
    for var in ("NO_PROXY", "no_proxy"):
        existing = env.get(var, "")
        env[var] = ",".join(filter(None, [existing, "127.0.0.1,localhost"]))
    return env


@pytest.fixture
def zero_quota_codex_session(
    built_spa: None,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str, _ZeroQuotaGateway]]:
    """A codex-native wrapper session routed at a zero-quota gateway.

    Spawns a dedicated server + runner whose provider config routes native
    Codex at the :class:`_ZeroQuotaGateway` loopback stand-in, then creates
    and binds the same codex-native wrapper session ``omnigent codex`` ships.
    Every turn on this rig dies with the gateway's zero-quota 403.

    :returns: ``(base_url, session_id, gateway)``.
    """
    codex_path = shutil.which("codex")
    if codex_path is None:
        pytest.skip("codex CLI is required for the zero-quota codex-native rig")
    if not _codex_cli_supports_mocked_app_server(codex_path):
        pytest.skip("codex CLI >= 0.139.0 is required for mocked-provider e2e")

    gateway = _ZeroQuotaGateway()
    threading.Thread(target=gateway.serve_forever, daemon=True).start()

    work = tmp_path_factory.mktemp("codex_zero_quota")
    config_home = work / "config-home"
    codex_home = work / "codex-home"
    home_dir = work / "home"
    state_dir = work / "codex-native-state"
    artifacts = work / "artifacts"
    for path in (config_home, codex_home, home_dir, state_dir, artifacts):
        path.mkdir(parents=True, exist_ok=True)
    _write_mock_codex_provider_config(config_home, gateway.base_url, model="mock-model")

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    binding_token = secrets.token_urlsafe(32)

    from omnigent.runner.identity import token_bound_runner_id

    runner_id = token_bound_runner_id(binding_token)

    shared_env = {
        **_no_proxy_env(),
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "OMNIGENT_CODEX_NATIVE_STATE_DIR": str(state_dir),
        "CODEX_HOME": str(codex_home),
        "HOME": str(home_dir),
    }
    server_env = {**shared_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}
    runner_env = {
        **shared_env,
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
    }

    server_log = work / "server.log"
    runner_log = work / "runner.log"
    server_handle = server_log.open("w")
    runner_handle = runner_log.open("w")
    server_proc: subprocess.Popen[bytes] | None = None
    runner_proc: subprocess.Popen[bytes] | None = None
    session_id: str | None = None
    try:
        server_proc = subprocess.Popen(
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
                f"sqlite:///{work}/test.db",
                "--artifact-location",
                str(artifacts),
            ],
            env=server_env,
            stdout=server_handle,
            stderr=subprocess.STDOUT,
            cwd=str(_REPO_ROOT),
        )
        runner_proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=runner_env,
            stdout=runner_handle,
            stderr=subprocess.STDOUT,
            cwd=str(_REPO_ROOT),
        )

        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        online = False
        while time.monotonic() < deadline:
            if server_proc.poll() is not None or runner_proc.poll() is not None:
                break
            try:
                if _client.get(f"{base_url}/health", timeout=2).status_code == 200:
                    status = _client.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                    if status.status_code == 200 and status.json().get("online"):
                        online = True
                        break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        if not online:
            raise RuntimeError(
                "zero-quota codex rig did not come online within "
                f"{_HEALTH_TIMEOUT_S:.0f}s.\nServer log:\n{server_log.read_text()[-3000:]}\n"
                f"Runner log:\n{runner_log.read_text()[-3000:]}"
            )

        session_id = _create_native_codex_session(base_url, runner_id, model="mock-model")
        yield (base_url, session_id, gateway)
    finally:
        if session_id is not None:
            with contextlib.suppress(httpx.HTTPError):
                _client.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
        for proc in (runner_proc, server_proc):
            if proc is not None and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        for proc in (runner_proc, server_proc):
            if proc is not None:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        server_handle.close()
        runner_handle.close()
        gateway.shutdown()
        gateway.server_close()


@pytest.mark.timeout(400)
def test_zero_quota_gateway_403_does_not_advise_codex_login(
    page: Page,
    zero_quota_codex_session: tuple[str, str, _ZeroQuotaGateway],
) -> None:
    """A zero-quota 403 turn error must not carry a ``codex login`` hint.

    Journey (the reported one): a user whose workspace gateway quota is
    provisioned to zero sends a codex-native chat message; the gateway rejects
    the turn with ``403 PERMISSION_DENIED`` / "rate limit is set to 0"; the
    SPA surfaces the failed turn as an error pill.  The surfaced error must
    name the quota rejection and must NOT direct the user to ``codex login``
    — a login cannot mint quota, so the hint only sends users retrying into
    the same wall.
    """
    base_url, session_id, gateway = zero_quota_codex_session
    page.goto(f"{base_url}/c/{session_id}")
    _ensure_chat_view(page)

    _send(page, "Summarize the repo layout for me.")

    # The turn must reach a terminal, user-visible failure: the error pill.
    pill = page.locator(_ERROR_PILL)
    expect(pill.first).to_be_visible(timeout=int(_TURN_OUTCOME_TIMEOUT_S * 1000))

    # Expand the pill so the surfaced message (what the user actually reads)
    # is on screen, and pin the journey: the error names the quota rejection.
    pill.first.click()
    message_content = page.get_by_test_id("error-message-content")
    expect(message_content.first).to_be_visible(timeout=10_000)
    expect(message_content.first).to_contain_text(_ZERO_QUOTA_MARKER, timeout=10_000)
    # Let the expanded error settle on screen before reading it (also gives a
    # recorded clip a beat on the outcome the user reads).
    page.wait_for_timeout(1_500)

    # The bug: the surfaced error directs the user to `codex login`.
    visible_error = message_content.first.inner_text()
    assert not _LOGIN_HINT_RE.search(visible_error), (
        "zero-quota gateway 403 surfaced a `codex login` remediation to the "
        "user; a rate limit of 0 is a provisioning state a login cannot fix:\n"
        f"{visible_error[:800]}"
    )

    # Durable assertion against the persisted failure, not just the pill: a
    # codex-native turn failure writes no transcript ``error`` item; it is
    # persisted as the session snapshot's ``last_task_error`` (what the SPA
    # re-renders after reload). It must name the quota rejection, must not
    # carry the login remediation, and must not be classified as re-auth.
    detail = _client.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    detail.raise_for_status()
    last_task_error = detail.json().get("last_task_error") or {}
    persisted_message = str(last_task_error.get("message", ""))
    assert _ZERO_QUOTA_MARKER in persisted_message, (
        "the turn did not persist the gateway's zero-quota rejection; "
        f"gateway saw POSTs: {gateway.post_paths_seen!r}; "
        f"last_task_error: {last_task_error!r}"
    )
    assert not _LOGIN_HINT_RE.search(persisted_message), (
        "zero-quota gateway 403 persisted a `codex login` remediation the "
        f"user cannot act on:\n{persisted_message[:800]}"
    )
    assert last_task_error.get("code") != "codex_reauth_required", (
        "zero-quota gateway 403 was persisted as a re-auth failure; a rate "
        "limit of 0 is a provisioning state, not an auth state: "
        f"{last_task_error!r}"
    )
