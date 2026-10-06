r"""UI journey: a Claude Code prompt on a proxied host must not be fail-closed by its policy hook.

A runner on a host with an ambient HTTP proxy (``HTTP_PROXY`` / ``HTTPS_PROXY``
exported, no loopback ``NO_PROXY`` exemption) launches Claude Code. A prompt
typed into the terminal fires the ``UserPromptSubmit`` policy hook, whose
``POST .../policies/evaluate`` targets the runner-local relay on ``127.0.0.1``.
The hook's ``httpx.Client`` honours the proxy environment, so that loopback
callback is sent to the proxy instead of the runner. A remote proxy cannot reach
the runner's loopback, the callback fails, and the hook fails closed: Claude Code
drops the prompt with "UserPromptSubmit operation blocked by hook: Omnigent
policy evaluation unavailable ...".

Stand-in for the reported environment: an in-process forward proxy that answers
loopback destinations with ``502`` (a remote proxy's ``127.0.0.1`` is itself) and
forwards the model endpoint, which is addressed by a non-loopback name only the
proxy can reach -- the shape of a corporate egress proxy. The mock LLM serves the
model traffic, so the journey completes once the hook connects to loopback
directly.

The test asserts the desired behaviour: the reply lands in the session. It fails
on a build whose hook routes loopback callbacks through the proxy.

Run::

    pytest tests/e2e_ui/approvals/test_policy_hook_ambient_proxy.py \
        --ui-skip-build --video on
"""

from __future__ import annotations

import ipaddress
import json
import os
import secrets
import select
import shutil
import socket
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _CLAUDE_MOCK_MODEL,
    _REPO_ROOT,
    _create_native_claude_session,
    reset_mock_llm,
    set_fallback_mock_llm,
)
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _ensure_chat_view,
    _item_text,
    _ordered_message_items,
    _turn_prompt,
)
from tests.e2e_ui.messages.test_native_claude_render_parity import (
    _open_terminal_view,
    _pane_text,
    _tmux_advert,
    _type_into_tui,
    _wait_terminal_connected,
)

pytestmark = pytest.mark.skipif(
    shutil.which("claude") is None,
    reason="native Claude Code journey needs the `claude` CLI on PATH.",
)

# The model endpoint's name on the corporate network: not loopback, resolvable
# only by the proxy.
_MODEL_HOST = "anthropic-gateway.corp.test"

_RUNNER_ONLINE_TIMEOUT_S = 120.0
_POLL_S = 0.5
# Claude boot + prompt injection + the hook's 30 s fail-closed retry budget.
_TURN_OUTCOME_TIMEOUT_S = 180.0
_TUI_READY_TIMEOUT_S = 120.0
# The SessionStart hook prints this only after Claude Code has loaded its hook
# config, so it proves the UserPromptSubmit gate is registered before we type.
_TUI_READY_MARKER = "open this session in omnigent"

_HOP_HEADERS = frozenset(
    {
        "host",
        "connection",
        "keep-alive",
        "proxy-connection",
        "proxy-authorization",
        "content-length",
        "transfer-encoding",
    }
)
_RESPONSE_DROP_HEADERS = frozenset(
    {"content-length", "transfer-encoding", "connection", "content-encoding"}
)


def _is_loopback_host(host: str) -> bool:
    host = host.strip("[]")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _pump(client: socket.socket, remote: socket.socket) -> None:
    peers = {client: remote, remote: client}
    while True:
        readable, _, _ = select.select(list(peers), [], [], 60.0)
        if not readable:
            return
        for src in readable:
            data = src.recv(65536)
            if not data:
                return
            peers[src].sendall(data)


class _CorporateProxy:
    """Forward proxy standing in for a remote egress proxy.

    Forwards ``_MODEL_HOST`` to the mock LLM. Answers loopback destinations
    with 502, as a remote proxy does when ``127.0.0.1:<port>`` is refused on
    its own host, and refuses every other destination.
    """

    def __init__(self, model_upstream: str) -> None:
        upstream = urlsplit(model_upstream)
        self.model_upstream = (upstream.hostname or "127.0.0.1", upstream.port or 80)
        self.loopback_refusals: list[str] = []
        self.model_forwards: list[str] = []
        proxy = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:
                del args

            def _refuse(self, text: str) -> None:
                body = text.encode()
                self.send_response(HTTPStatus.BAD_GATEWAY)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _route(self, host: str, port: int, what: str) -> tuple[str, int] | None:
                if _is_loopback_host(host):
                    proxy.loopback_refusals.append(what)
                    self._refuse(
                        f"proxy: connect to {host}:{port} refused "
                        "(loopback resolves to the proxy host, not the runner)"
                    )
                    return None
                if host != _MODEL_HOST:
                    self._refuse(f"proxy: {host}:{port} is not reachable from the proxy")
                    return None
                proxy.model_forwards.append(what)
                return proxy.model_upstream

            def do_CONNECT(self) -> None:
                host, _, port = self.path.rpartition(":")
                target = self._route(host, int(port or 443), f"CONNECT {self.path}")
                if target is None:
                    return
                with socket.create_connection(target, timeout=10.0) as remote:
                    self.send_response(HTTPStatus.OK, "Connection Established")
                    self.end_headers()
                    self.close_connection = True
                    _pump(self.connection, remote)

            def _forward(self) -> None:
                url = urlsplit(self.path)
                if not url.scheme:
                    self.send_error(HTTPStatus.BAD_REQUEST, "not a proxy request")
                    return
                target = self._route(
                    url.hostname or "", url.port or 80, f"{self.command} {self.path}"
                )
                if target is None:
                    return
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                headers = {k: v for k, v in self.headers.items() if k.lower() not in _HOP_HEADERS}
                fwd = f"http://{target[0]}:{target[1]}{url.path or '/'}"
                if url.query:
                    fwd += f"?{url.query}"
                with httpx.Client(trust_env=False, timeout=120.0) as client:
                    upstream_resp = client.request(
                        self.command, fwd, content=body, headers=headers
                    )
                self.send_response(upstream_resp.status_code)
                for key, value in upstream_resp.headers.items():
                    if key.lower() not in _RESPONSE_DROP_HEADERS:
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(upstream_resp.content)))
                self.end_headers()
                self.wfile.write(upstream_resp.content)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _forward

        class _Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request: object, client_address: object) -> None:
                del request, client_address

        self._httpd = _Server(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[0], self._httpd.server_address[1]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


def _spawn_proxied_host_runner(
    base_url: str, proxy_url: str, model_base_url: str, tmp_path: Path
) -> tuple[subprocess.Popen[bytes], str, Path]:
    """Start a runner the way a proxied host does: proxy vars set, no loopback exemption."""
    from omnigent.runner.identity import token_bound_runner_id

    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)
    config_home = tmp_path / "proxied-host-config"
    config_home.mkdir()
    (config_home / "config.yaml").write_text(
        textwrap.dedent(f"""\
            providers:
              corp-claude:
                kind: key
                default: [anthropic]
                anthropic:
                  base_url: "{model_base_url}"
                  api_key: "mock-key"
                  models:
                    default: {_CLAUDE_MOCK_MODEL}
            """)
    )
    dropped = {"RUNNER_SERVER_URL", "OMNIGENT_REMOTE_AUTH_TOKEN", "OMNIGENT_CONFIG_HOME"}
    dropped |= {"NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy"}
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in dropped and not key.startswith(("OMNIGENT_RUNNER", "OMNIGENT_HOST"))
    }
    env.update(
        {
            "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
            "OMNIGENT_RUNNER_ID": runner_id,
            "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
            "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
            "RUNNER_SERVER_URL": base_url,
            "OMNIGENT_CONFIG_HOME": str(config_home),
            "HTTP_PROXY": proxy_url,
            "HTTPS_PROXY": proxy_url,
            "http_proxy": proxy_url,
            "https_proxy": proxy_url,
        }
    )
    log_path = tmp_path / "proxied-host-runner.log"
    with log_path.open("w") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )

    deadline = time.monotonic() + _RUNNER_ONLINE_TIMEOUT_S
    last = "not polled"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            last = f"runner exited with code {proc.returncode}"
            break
        try:
            status = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2.0)
            if status.status_code == 200 and status.json().get("online") is True:
                return proc, runner_id, log_path
            last = f"HTTP {status.status_code}: {status.text[:200]}"
        except httpx.HTTPError as exc:
            last = repr(exc)
        time.sleep(_POLL_S)
    proc.terminate()
    raise RuntimeError(
        f"proxied-host runner never came online (last: {last}); log:\n"
        f"{log_path.read_text()[-3000:]}"
    )


def _blocked_by_hook(pane: str) -> bool:
    flat = " ".join(pane.split()).lower()
    return "blocked by hook" in flat and "policy evaluation unavailable" in flat


def _relay_url(base_url: str, session_id: str) -> str:
    advert = _tmux_advert(base_url, session_id)
    if advert is None:
        return "<relay not advertised>"
    from omnigent.harnesses.claude_native.bridge import (
        BRIDGE_ID_LABEL_KEY,
        bridge_dir_for_bridge_id,
    )

    session = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0).json()
    bridge_id = (session.get("labels") or {}).get(BRIDGE_ID_LABEL_KEY) or session_id
    relay_file = bridge_dir_for_bridge_id(bridge_id) / "tool_relay.json"
    try:
        return str(json.loads(relay_file.read_text(encoding="utf-8")).get("url"))
    except (OSError, json.JSONDecodeError):
        return "<tool_relay.json unreadable>"


def _assistant_replied(base_url: str, session_id: str, token: str) -> bool:
    return any(
        item.get("role") == "assistant" and token in _item_text(item)
        for item in _ordered_message_items(base_url, session_id)
    )


def _wait_tui_ready(page: Page, base_url: str, session_id: str) -> str:
    """Wait until Claude Code has booted and its policy hooks are registered.

    The prompt must be typed only after the UserPromptSubmit gate is live;
    typing during boot races that registration and lets a prompt slip past it.
    """
    deadline = time.monotonic() + _TUI_READY_TIMEOUT_S
    pane = ""
    while time.monotonic() < deadline:
        pane = _pane_text(base_url, session_id)
        if _TUI_READY_MARKER in " ".join(pane.split()).lower():
            page.wait_for_timeout(1_500)
            return _pane_text(base_url, session_id)
        page.wait_for_timeout(1_000)
    raise AssertionError(f"Claude Code TUI never became ready; last pane:\n{pane}")


def _wait_turn_outcome(page: Page, base_url: str, session_id: str, token: str) -> tuple[str, str]:
    """Wait for the assistant's *token* in the transcript or the fail-closed block in the TUI."""
    deadline = time.monotonic() + _TURN_OUTCOME_TIMEOUT_S
    pane = ""
    while time.monotonic() < deadline:
        pane = _pane_text(base_url, session_id)
        if _blocked_by_hook(pane):
            return "blocked", pane
        if _assistant_replied(base_url, session_id, token):
            return "reply", pane
        page.wait_for_timeout(1_000)
    return "timeout", pane


@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_native_claude_prompt_survives_ambient_proxy(
    page: Page,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """A prompt in a Claude Code session on a proxied host gets its reply, not a hook block."""
    proxy = _CorporateProxy(mock_llm_server_url)
    model_base_url = f"http://{_MODEL_HOST}:{proxy.model_upstream[1]}"
    runner: subprocess.Popen[bytes] | None = None
    session_id: str | None = None
    try:
        probe = httpx.get(f"{model_base_url}/stats", proxy=proxy.url, timeout=10.0)
        assert probe.status_code == 200, f"proxy cannot reach the model endpoint: {probe}"

        runner, runner_id, _runner_log = _spawn_proxied_host_runner(
            live_server, proxy.url, model_base_url, tmp_path
        )
        session_id = _create_native_claude_session(live_server, runner_id)

        page.goto(f"{live_server}/c/{session_id}")
        _open_terminal_view(page)
        _wait_terminal_connected(page)
        _wait_tui_ready(page, live_server, session_id)

        reset_mock_llm(mock_llm_server_url)
        token = f"ast-{uuid.uuid4().hex[:8]}"
        set_fallback_mock_llm(mock_llm_server_url, "default", token)
        set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, token)
        _type_into_tui(page, _turn_prompt(1, f"usr-{uuid.uuid4().hex[:8]}", token))

        outcome, pane = _wait_turn_outcome(page, live_server, session_id, token)
        page.wait_for_timeout(3_000)
        if outcome == "blocked":
            raise AssertionError(
                "Bug reproduced: the UserPromptSubmit policy hook posted its loopback callback "
                f"(relay {_relay_url(live_server, session_id)}) through the ambient proxy, which "
                "cannot reach the runner's loopback, so the prompt was blocked fail-closed.\n"
                f"proxy refusals of loopback callbacks: {proxy.loopback_refusals!r}\n"
                f"proxy forwards of model traffic: {proxy.model_forwards!r}\n"
                f"TUI pane:\n{pane}"
            )
        assert outcome == "reply", (
            f"no reply and no hook block within {_TURN_OUTCOME_TIMEOUT_S:.0f}s; "
            f"proxy refusals={proxy.loopback_refusals!r} forwards={proxy.model_forwards!r}\n"
            f"TUI pane:\n{pane}"
        )
        _ensure_chat_view(page)
        expect(page.locator(_ASSISTANT, has_text=token).first).to_be_visible(timeout=30_000)
    finally:
        if session_id is not None:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if runner is not None and runner.poll() is None:
            runner.terminate()
            try:
                runner.wait(timeout=10)
            except subprocess.TimeoutExpired:
                runner.kill()
                runner.wait(timeout=5)
        proxy.close()
