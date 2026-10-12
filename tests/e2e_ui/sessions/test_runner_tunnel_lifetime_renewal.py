"""A runner behind an intermediary with an absolute WebSocket lifetime, mid-turn.

The runner process tunnels into the server through a loopback proxy that
severs each upgraded connection a fixed time after it was accepted without a
close frame (``tests/_helpers/ws_lifetime_proxy.py``), standing in for an
ingress or load balancer with a connection-lifetime cap. The runner's stored
login is near expiry, so its genuine credential-refresh path
(``POST {server_url}/oauth/token``) runs through the same proxy, which stalls
it briefly after the severance to model a slow identity provider.

Expected: the runner renews its tunnel before the boundary, so the in-flight
turn, the server and the session page never see the runner offline.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO
from urllib.parse import urlsplit

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent.runner.identity import token_bound_runner_id
from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests._helpers.ws_lifetime_proxy import LifetimeProxy
from tests.e2e_ui.conftest import configure_mock_llm, set_fallback_mock_llm
from tests.helpers.ui_configuration import prepared_repro_environment

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ARTIFACTS_ENV = "OMNI_11843_ARTIFACTS"

_PROXY_WS_LIFETIME_S = 25.0
_REFRESH_STALL_S = 30.0
_STALL_WINDOW_AFTER_ABORT_S = 12.0
_STORED_LOGIN_LIFETIME_S = 100.0
_TOOL_SLEEP_S = 30
_RUNNER_ONLINE_TIMEOUT_S = 60.0
_RUNNER_DISCONNECT_GRACE_S = 90.0
_TURN_TIMEOUT_S = _PROXY_WS_LIFETIME_S + _RUNNER_DISCONNECT_GRACE_S + 60.0
_LIVENESS_POLL_S = 0.5

_RENEWAL_INTERVAL_ENV = "OMNIGENT_RUNNER_TUNNEL_RENEWAL_INTERVAL_S"
_RENEWAL_INTERVAL_S = 10

_TUNNEL_PATH_FRAGMENT = "/tunnel"
_COMPOSER_NAME = "Message the agent"
_WORKING = '[data-testid="working-indicator"]'
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'

_AGENT_NAME = "tunnel_lifetime_probe"
_AGENT_YAML = """\
spec_version: 1
name: {name}
prompt: |
  You are a deterministic test assistant. When the user asks you to run the
  long job, call sys_os_shell exactly once with the command the user gives,
  wait for its result, then reply with one short sentence.

executor:
  model: {model}
  config:
    harness: openai-agents

os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none
"""


def _renewal_env() -> dict[str, str]:
    return {_RENEWAL_INTERVAL_ENV: str(_RENEWAL_INTERVAL_S)}


def artifacts_dir() -> Path:
    configured = os.environ.get(_ARTIFACTS_ENV)
    if configured:
        path = Path(configured) / f"run-{time.strftime('%Y%m%d-%H%M%S')}"
    else:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = _REPO_ROOT / ".omnigent" / "repro-artifacts" / "omni-11843" / f"run-{stamp}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _server_log_path() -> Path | None:
    env_file = _REPO_ROOT / ".omnigent" / "repro-env" / "environment.json"
    if not env_file.exists():
        return None
    data = json.loads(env_file.read_text())
    database = data.get("database")
    if not database:
        return None
    logs = Path(database).parent / "data" / "logs" / "server"
    candidates = sorted(logs.glob("server-*.log"), key=lambda p: p.stat().st_mtime)
    return candidates[-1] if candidates else None


@dataclass
class ProxiedRunner:
    runner_id: str
    proc: subprocess.Popen[bytes]
    data_dir: Path
    workspace: Path
    stdout_log: Path
    started_at: float
    stdout_handle: IO[str] | None = None

    def log_lines(self, *needles: str) -> list[str]:
        lines: list[str] = []
        for path in [self.stdout_log, *sorted((self.data_dir / "logs" / "runner").glob("*.log"))]:
            if not path.exists():
                continue
            for line in path.read_text(errors="replace").splitlines():
                if not needles or any(n in line for n in needles):
                    lines.append(line)
        return lines

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        if self.stdout_handle is not None and not self.stdout_handle.closed:
            self.stdout_handle.close()


def write_stored_login(data_dir: Path, server_url: str, *, lifetime_s: float) -> float:
    """Leave the file ``omnigent login`` writes, with a login-issued refresh grant."""
    expires_at = time.time() + lifetime_s
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "auth_tokens.json").write_text(
        json.dumps(
            {
                server_url.rstrip("/"): {
                    "token": "stored-login-session-token",
                    "expires_at": expires_at,
                    "refresh_token": "stored-login-refresh-grant",
                }
            }
        )
    )
    return expires_at


def runner_online(server_url: str, runner_id: str) -> bool | None:
    try:
        resp = httpx.get(f"{server_url}/v1/runners/{runner_id}/status", timeout=2.0)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    return bool(resp.json().get("online"))


def spawn_proxied_runner(
    proxy_url: str,
    server_url: str,
    mock_url: str,
    artifacts: Path,
    *,
    stored_login_lifetime_s: float | None,
) -> ProxiedRunner:
    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)
    data_dir = artifacts / "runner-data"
    workspace = artifacts / "runner-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    if stored_login_lifetime_s is not None:
        write_stored_login(data_dir, proxy_url, lifetime_s=stored_login_lifetime_s)
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("OMNIGENT_RUNNER_", "OMNIGENT_HOST_"))
        and k not in {"RUNNER_SERVER_URL", "OMNIGENT_REMOTE_AUTH_TOKEN"}
    }
    no_proxy = ",".join(filter(None, [os.environ.get("NO_PROXY", ""), "127.0.0.1", "localhost"]))
    env.update(
        {
            "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
            "OMNIGENT_RUNNER_ID": runner_id,
            "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
            "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
            "RUNNER_SERVER_URL": proxy_url,
            "OMNIGENT_DATA_DIR": str(data_dir),
            "OPENAI_BASE_URL": f"{mock_url}/v1",
            "OPENAI_API_KEY": "mock-key",
            "NO_PROXY": no_proxy,
            "no_proxy": no_proxy,
            **_renewal_env(),
        }
    )
    stdout_log = artifacts / "runner-stdout.log"
    handle = open(stdout_log, "w")  # noqa: SIM115 — closed with the process
    started_at = time.time()
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=env,
            cwd=workspace,
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
    except BaseException:
        handle.close()
        raise
    runner = ProxiedRunner(runner_id, proc, data_dir, workspace, stdout_log, started_at, handle)
    deadline = time.monotonic() + _RUNNER_ONLINE_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            handle.close()
            raise RuntimeError(
                f"proxied runner exited early ({proc.returncode}):\n"
                f"{stdout_log.read_text()[-3000:]}"
            )
        if runner_online(server_url, runner_id):
            return runner
        time.sleep(0.5)
    runner.stop()
    raise RuntimeError(
        f"proxied runner did not come online through {proxy_url} within "
        f"{_RUNNER_ONLINE_TIMEOUT_S:.0f}s:\n{stdout_log.read_text()[-3000:]}"
    )


def create_lifetime_session(
    server_url: str, mock_url: str, runner_id: str, marker: Path
) -> tuple[str, str, str]:
    model = f"tunnel-lifetime-{uuid.uuid4().hex[:8]}"
    command = f"sleep {_TOOL_SLEEP_S}; date +%s >> {marker}"
    configure_mock_llm(
        mock_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_long_job",
                        "name": "sys_os_shell",
                        "arguments": json.dumps({"command": command}),
                    }
                ]
            }
        ],
        key=model,
    )
    set_fallback_mock_llm(mock_url, model, "The long job finished.")
    yaml_text = _AGENT_YAML.format(name=_AGENT_NAME, model=model)
    bundle = bundle_files({"config.yaml": yaml_text.encode()})
    created = post_session_bundle(httpx.post, f"{server_url}/v1/sessions", bundle, timeout=30.0)
    created.raise_for_status()
    session_id = str(created.json()["session_id"])
    bind_session_runner(httpx.patch, server_url, session_id, runner_id, timeout=10.0)
    return session_id, model, command


class LivenessPoller(threading.Thread):
    """Record the runner's server-side online/offline transitions."""

    def __init__(self, server_url: str, runner_id: str) -> None:
        super().__init__(name="runner-liveness-poll", daemon=True)
        self._server_url = server_url
        self._runner_id = runner_id
        self._stop_event = threading.Event()
        self.transitions: list[tuple[float, bool | None]] = []

    def run(self) -> None:
        last: object = object()
        while not self._stop_event.is_set():
            online = runner_online(self._server_url, self._runner_id)
            if online is not last:
                self.transitions.append((time.time(), online))
                last = online
            self._stop_event.wait(_LIVENESS_POLL_S)

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=5)


@dataclass
class Journey:
    artifacts: Path
    server_url: str
    proxy: LifetimeProxy
    runner: ProxiedRunner
    session_id: str
    model: str
    command: str
    marker: Path
    notes: dict[str, object] = field(default_factory=dict)


@pytest.fixture
def server_url(request: pytest.FixtureRequest) -> str:
    prepared = prepared_repro_environment()["OMNIGENT_REPRO_SERVER_URL"]
    return prepared or str(request.getfixturevalue("live_server"))


@pytest.fixture
def journey(server_url: str, mock_llm_server_url: str) -> Iterator[Journey]:
    artifacts = artifacts_dir()
    parsed = urlsplit(server_url)
    proxy = LifetimeProxy(
        parsed.hostname or "127.0.0.1",
        parsed.port or 80,
        ws_lifetime_s=_PROXY_WS_LIFETIME_S,
        max_aborts=1,
        stall_paths={"/oauth/token": _REFRESH_STALL_S},
        stall_window_after_abort_s=_STALL_WINDOW_AFTER_ABORT_S,
        event_log=artifacts / "proxy-events.jsonl",
    )
    proxy.start()
    runner: ProxiedRunner | None = None
    try:
        runner = spawn_proxied_runner(
            proxy.url,
            server_url,
            mock_llm_server_url,
            artifacts,
            stored_login_lifetime_s=_STORED_LOGIN_LIFETIME_S,
        )
        marker = artifacts / "tool-side-effect.txt"
        session_id, model, command = create_lifetime_session(
            server_url, mock_llm_server_url, runner.runner_id, marker
        )
    except BaseException:
        if runner is not None:
            runner.stop()
        proxy.stop()
        raise
    j = Journey(artifacts, server_url, proxy, runner, session_id, model, command, marker)
    try:
        yield j
    finally:
        with contextlib.suppress(httpx.HTTPError):
            httpx.delete(f"{server_url}/v1/sessions/{session_id}", timeout=10.0)
        runner.stop()
        proxy.stop()


def _send(page: Page, text: str) -> None:
    composer = page.get_by_role("textbox", name=_COMPOSER_NAME)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _wait_past_lifetime_boundary(
    proxy: LifetimeProxy, lifetime_s: float, timeout_s: float
) -> list[dict[str, object]] | None:
    """Wait for a replacement tunnel and for the first tunnel's lifetime to lapse.

    Make-before-break retires the old socket before the proxy can sever it, so a
    correct fix records no abort. Observe the UI across the cutover by waiting
    for the second upgrade accept and for the original connection's lifetime
    boundary to pass, rather than waiting for an abort that should never fire.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        accepts = [
            e for e in proxy.snapshot() if e.get("kind") == "accept" and e.get("upgrade") is True
        ]
        if len(accepts) >= 2:
            boundary = min(float(e["t"]) for e in accepts) + lifetime_s
            if time.time() >= boundary:
                return accepts
        time.sleep(0.25)
    return None


_UI_KEYWORDS = (
    "offline",
    "disconnect",
    "reconnect",
    "resume",
    "unavailable",
    "dropped",
    "working",
)


def _visible_ui_state(page: Page) -> dict[str, object]:
    body = page.locator("body").inner_text()
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    composer = page.get_by_role("textbox", name=_COMPOSER_NAME)
    return {
        "working_indicator": page.locator(_WORKING).count(),
        "composer_enabled": composer.is_enabled() if composer.count() else None,
        "composer_placeholder": composer.get_attribute("placeholder")
        if composer.count()
        else None,
        "keyword_lines": [ln for ln in lines if any(k in ln.lower() for k in _UI_KEYWORDS)][:20],
    }


def _session_snapshot(server_url: str, session_id: str) -> dict[str, object]:
    resp = httpx.get(f"{server_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json()


def _server_log_lines_since(path: Path | None, offset: int, session_id: str) -> list[str]:
    if path is None or not path.exists():
        return []
    with path.open("rb") as fh:
        fh.seek(offset)
        text = fh.read().decode(errors="replace")
    return [line for line in text.splitlines() if session_id in line and "Relay" in line]


def test_tunnel_is_renewed_before_the_proxy_lifetime(
    request: pytest.FixtureRequest, journey: Journey
) -> None:
    server_log = _server_log_path()
    log_offset = server_log.stat().st_size if server_log is not None and server_log.exists() else 0
    liveness = LivenessPoller(journey.server_url, journey.runner.runner_id)
    liveness.start()
    shots = journey.artifacts / "screenshots"
    shots.mkdir(exist_ok=True)

    page: Page = request.getfixturevalue("page")
    try:
        page.goto(f"{journey.server_url}/c/{journey.session_id}")
        expect(page.get_by_role("textbox", name=_COMPOSER_NAME)).to_be_visible(timeout=30_000)
        sent_at = time.time()
        _send(page, f"Run the long job: `{journey.command}`")
        expect(page.locator(_WORKING)).to_be_visible(timeout=30_000)
        page.wait_for_timeout(3_000)
        page.screenshot(path=str(shots / "01-tool-running.png"))

        # Sample the UI while the renewal cutover is happening, before the
        # original socket's lifetime boundary, so uninterrupted UI state is
        # observed during renewal and not only after the turn settles.
        page.wait_for_timeout(int((_RENEWAL_INTERVAL_S + 3) * 1000))
        page.screenshot(path=str(shots / "01b-during-renewal.png"))
        journey.notes["ui_during_renewal"] = _visible_ui_state(page)

        accepts = _wait_past_lifetime_boundary(
            journey.proxy, _PROXY_WS_LIFETIME_S, timeout_s=_PROXY_WS_LIFETIME_S + 15
        )
        journey.notes["tunnel_accepts"] = accepts
        page.wait_for_timeout(4_000)
        page.screenshot(path=str(shots / "02-after-boundary.png"))
        journey.notes["ui_after_boundary"] = _visible_ui_state(page)
        page.wait_for_timeout(4_000)
        journey.notes["ui_after_boundary_8s"] = _visible_ui_state(page)

        expect(page.locator(_WORKING)).to_have_count(0, timeout=int(_TURN_TIMEOUT_S * 1000))
        page.wait_for_timeout(1_500)
        page.screenshot(path=str(shots / "03-turn-settled.png"))
        journey.notes["ui_settled"] = _visible_ui_state(page)
        assistant_texts = page.locator(_ASSISTANT).all_inner_texts()
        error_texts = page.get_by_text("dropped unexpectedly").all_inner_texts()
        settled_at = time.time()
        back_online_deadline = time.monotonic() + 40
        while time.monotonic() < back_online_deadline:
            if runner_online(journey.server_url, journey.runner.runner_id):
                break
            time.sleep(0.5)
        journey.notes["runner_online_at_end"] = runner_online(
            journey.server_url, journey.runner.runner_id
        )
    finally:
        liveness.stop()

    snapshot = _session_snapshot(journey.server_url, journey.session_id)
    proxy_events = journey.proxy.snapshot()
    tunnel_accepts = [
        e for e in proxy_events if e.get("kind") == "accept" and e.get("upgrade") is True
    ]
    aborts = [e for e in proxy_events if e.get("kind") == "abort"]
    offline_spans = [t for t in liveness.transitions if t[1] is False]
    relay_lines = _server_log_lines_since(server_log, log_offset, journey.session_id)
    runner_lines = journey.runner.log_lines(
        "connected to", "tunnel disconnected", "Token refresh", "auth token"
    )
    marker_lines = journey.marker.read_text().splitlines() if journey.marker.exists() else []

    evidence = {
        "session_id": journey.session_id,
        "runner_id": journey.runner.runner_id,
        "sent_at": sent_at,
        "settled_at": settled_at,
        "notes": journey.notes,
        "proxy_events": proxy_events,
        "proxy_aborts": aborts,
        "liveness_transitions": liveness.transitions,
        "server_relay_lines": relay_lines,
        "runner_log_lines": runner_lines,
        "tool_side_effect_lines": marker_lines,
        "assistant_texts": assistant_texts,
        "error_texts": error_texts,
        "session_status": snapshot.get("status"),
        "last_task_error": snapshot.get("last_task_error") or snapshot.get("lastTaskError"),
    }
    (journey.artifacts / "evidence.json").write_text(json.dumps(evidence, indent=2, default=str))

    failures: list[str] = []
    first_accept_t = min((float(e["t"]) for e in tunnel_accepts), default=None)
    if first_accept_t is None:
        failures.append("the runner never established a tunnel through the intermediary")
    else:
        # Anchor on the connection-lifetime boundary, not on an observed sever:
        # make-before-break retires the old tunnel before the intermediary can
        # sever it, so the proxy may record no abort even for a correct fix.
        boundary = first_accept_t + _PROXY_WS_LIFETIME_S
        renewed_before = [e for e in tunnel_accepts if float(e["t"]) < boundary]
        if len(renewed_before) < 2:
            failures.append(
                "no replacement tunnel was opened before the intermediary's "
                f"{_PROXY_WS_LIFETIME_S:.0f}s connection lifetime elapsed"
            )
    ui_states = [
        journey.notes.get(k)
        for k in ("ui_during_renewal", "ui_after_boundary", "ui_after_boundary_8s", "ui_settled")
    ]
    disabled = [s for s in ui_states if isinstance(s, dict) and s.get("composer_enabled") is False]
    if disabled:
        failures.append(
            f"the composer was disabled (session shown offline) during the turn: {disabled}"
        )
    offline_ui = [
        s.get("keyword_lines")
        for s in ui_states
        if isinstance(s, dict)
        and any(
            "disconnect" in ln.lower() or "offline" in ln.lower()
            for ln in s.get("keyword_lines", [])
        )
    ]
    if offline_ui:
        failures.append(
            f"the session showed a disconnect/offline indicator during the turn: {offline_ui}"
        )
    if aborts:
        failures.append(
            "the intermediary severed a tunnel at its lifetime boundary; make-before-break "
            f"must retire the old socket first: aborts={[e.get('age_s') for e in aborts]}"
        )
    if offline_spans:
        failures.append(f"the runner was reported offline during the turn at {offline_spans}")
    if len(marker_lines) != 1:
        failures.append(f"tool side effect ran {len(marker_lines)} times, expected exactly once")
    if snapshot.get("status") == "failed" or error_texts:
        failures.append(f"the turn failed: status={snapshot.get('status')} errors={error_texts}")
    if not any("finished" in t for t in assistant_texts):
        failures.append(f"no assistant reply after the tool completed: {assistant_texts}")
    if failures:
        pytest.fail("\n".join(failures) + f"\nevidence: {journey.artifacts / 'evidence.json'}")
