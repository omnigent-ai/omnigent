"""E2E regression test: a native terminal startup failure's persisted debug-log
diagnostic must correlate to the FAILED child session and carry a searchable
error id.

A shared runner is spawned for one *primary* (parent) session
(``OMNIGENT_RUNNER_PRIMARY_SESSION_ID``); further child sessions co-locate in
the same runner process. When a child session's native terminal fails to
start, the runner returns the structured error payload (with a correlation
``Error ID: err_...``) to the child; the persisted debug-log row for that
diagnostic must be attributed to the CHILD session (not the runner's healthy
primary session) and must expose the error id the user was shown in the row's
searchable ``attributes`` map. The same must hold on the expected
missing-agent lifecycle path (``session_agent_missing``).

Journey — all through the real product path (a real ``omnigent server``
subprocess, a real runner subprocess, the real ZeroBus debug-log sink):

1. Start the server plus a local capture endpoint implementing the ZeroBus
   ingest contract (OIDC token mint + JSON row insert) that the sink posts to.
2. Create a parent session; spawn the runner for it the way the host does
   (``OMNIGENT_RUNNER_PRIMARY_SESSION_ID=<parent>``) with the sink enabled.
3. Create a child session and bind it to the same runner.
4. Make the child's native Claude terminal fail to start:
   * a stub ``claude`` that exits immediately (generic startup failure), and
   * separately, the child's agent deleted from the store (the expected
     ``session_agent_missing`` lifecycle path).
5. The ensure request returns the structured error with its ``Error ID``; the
   debug-log row the sink then ships for that diagnostic must carry the
   CHILD's session id and ``attributes.error_id``.

A regression re-attributes the row to the parent session and drops the
searchable error id.
"""

from __future__ import annotations

import contextlib
import http.server
import io
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# CI shells can carry an egress proxy in the environment; every HTTP call in
# this test targets 127.0.0.1, so bypass proxy autodetection entirely.
_http = httpx.Client(trust_env=False)

# The runner imports ``omnigent_client`` / ``omnigent_ui_sdk``; in a worktree
# they resolve from sdks/, in an installed venv from site-packages.
_PYTHONPATH = os.pathsep.join(
    [
        str(_REPO_ROOT),
        str(_REPO_ROOT / "sdks" / "python-client"),
        str(_REPO_ROOT / "sdks" / "ui"),
        os.environ.get("PYTHONPATH", ""),
    ]
)

from omnigent.runner.identity import (  # noqa: E402
    OMNIGENT_INTERNAL_WS_ORIGIN,
    token_bound_runner_id,
)

_HEALTH_TIMEOUT_S = 120.0
_POLL_S = 1.0
_ENSURE_TIMEOUT_S = 120.0
# The sink flushes every ~2s on a daemon thread; give delivery slack.
_ROW_TIMEOUT_S = 45.0

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="claude-native terminals run inside tmux; tmux not installed",
)


def _find_free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _localhost_env(extra: dict[str, str]) -> dict[str, str]:
    """Subprocess env with worktree imports, no proxy, and no leaked runner ctx.

    Any ``OMNIGENT*`` / ``RUNNER_SERVER_URL`` inherited from a parent runner is
    stripped, then re-supplied only via *extra*, so the spawned server/runner
    boot from a clean omnigent context.
    """
    env = {
        **os.environ,
        "PYTHONPATH": _PYTHONPATH,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(name, None)
    for name in list(env):
        if name.startswith("OMNIGENT") or name == "RUNNER_SERVER_URL":
            env.pop(name, None)
    env.update(extra)
    return env


def _terminate(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _wait_http_ok(url: str, deadline: float) -> None:
    last = "not polled"
    while time.monotonic() < deadline:
        try:
            if _http.get(url, timeout=2.0).status_code == 200:
                return
            last = "non-200"
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(_POLL_S)
    raise AssertionError(f"{url} never became healthy: {last}")


def _create_session_with_scoped_agent(base_url: str, name: str) -> tuple[str, str]:
    """Create a session bound to a fresh (session-scoped) agent.

    Uploads a minimal agent bundle via the multipart ``POST /v1/sessions``
    create path — exactly how a launched session is registered. The agent's
    harness does not matter: the Claude native adapter is selected by the
    ensured *terminal* name (``"claude"``).
    """
    yaml_text = "\n".join(
        [
            f"name: {name}",
            "description: Minimal fixture agent for log-correlation e2e.",
            "executor:",
            "  harness: openai-agents",
            "  model: gpt-5.4",
            "prompt: |",
            "  You are a terse smoke-test assistant.",
            "",
        ]
    )
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        # Non-config.yaml arcname routes through the omnigent compat translator.
        info = tarfile.TarInfo(f"{name}.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    create = _http.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": (f"{name}.tar.gz", buf.getvalue(), "application/gzip")},
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        timeout=30.0,
    )
    create.raise_for_status()
    body = create.json()
    return str(body["session_id"]), str(body["agent_id"])


class _ZeroBusCapture:
    """Local HTTP endpoint implementing the ZeroBus ingest contract.

    The runner's real debug-log sink mints a token from
    ``<workspace_url>/oidc/v1/token`` and POSTs serialized row batches to the
    insert URL; this server answers both and records every received row, so a
    test can assert on exactly what the sink persisted.
    """

    def __init__(self) -> None:
        self._rows: list[dict[str, object]] = []
        self._lock = threading.Lock()
        rows, lock = self._rows, self._lock

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if self.path.endswith("/oidc/v1/token"):
                    payload = json.dumps({"access_token": "e2e-token", "expires_in": 3600})
                elif "/tables/" in self.path:
                    with lock:
                        rows.extend(json.loads(body))
                    payload = "{}"
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                data = payload.encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args: object) -> None:
                return

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    @property
    def insert_url(self) -> str:
        return f"{self.base_url}/zerobus/v1/tables/main.omnigent.debug_logs/insert"

    def rows(self) -> list[dict[str, object]]:
        with self._lock:
            return list(self._rows)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@dataclass
class _Stack:
    base_url: str
    runner_id: str
    parent_session_id: str
    capture: _ZeroBusCapture
    tmp_path: Path

    def runner_log_tail(self) -> str:
        try:
            return (self.tmp_path / "runner.log").read_text()[-3000:]
        except OSError:
            return "<runner log unreadable>"


@contextlib.contextmanager
def _native_stack(tmp_path: Path, claude_stub_script: str):
    """Spawn server + runner-for-a-parent-session with the debug-log sink live.

    Mirrors production wiring: the parent session exists first, the runner is
    spawned for it (``OMNIGENT_RUNNER_PRIMARY_SESSION_ID``), and the sink is
    enabled through the same ``OMNIGENT_DEBUG_LOG_*`` env contract the internal
    config CLI sets — pointed at the local ZeroBus capture endpoint.
    """
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    database_uri = f"sqlite:///{tmp_path / 'chat.db'}"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner_home = tmp_path / "home"
    runner_home.mkdir()

    # Stub Claude CLI on PATH so the launch path never blocks on a real
    # (unauthenticated) Claude TUI; the script decides how the launch behaves.
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    stub = stub_bin / "claude"
    stub.write_text(claude_stub_script)
    stub.chmod(0o755)

    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)

    capture = _ZeroBusCapture()
    server_log = (tmp_path / "server.log").open("w")
    runner_log = (tmp_path / "runner.log").open("w")
    server_proc: subprocess.Popen[bytes] | None = None
    runner_proc: subprocess.Popen[bytes] | None = None
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
                database_uri,
                "--artifact-location",
                str(tmp_path / "artifacts"),
            ],
            env=_localhost_env({"OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}),
            stdout=server_log,
            stderr=subprocess.STDOUT,
        )
        _wait_http_ok(f"{base_url}/health", time.monotonic() + _HEALTH_TIMEOUT_S)

        # The parent session exists BEFORE the runner spawns for it — the
        # order the host follows when it launches a runner for a session.
        parent_session_id, _parent_agent_id = _create_session_with_scoped_agent(
            base_url, "parent-primary-fixture"
        )

        runner_proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=_localhost_env(
                {
                    "OMNIGENT_RUNNER_ID": runner_id,
                    "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
                    "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
                    "RUNNER_SERVER_URL": base_url,
                    "OMNIGENT_RUNNER_WORKSPACE": str(workspace),
                    "OMNIGENT_RUNNER_PRIMARY_SESSION_ID": parent_session_id,
                    # Real sink, local ZeroBus-contract capture endpoint.
                    "OMNIGENT_DEBUG_LOG_CLIENT_ID": "e2e-client",
                    "OMNIGENT_DEBUG_LOG_CLIENT_SECRET": "e2e-secret",
                    "OMNIGENT_DEBUG_LOG_WORKSPACE_URL": capture.base_url,
                    "OMNIGENT_DEBUG_LOG_ENDPOINT": capture.insert_url,
                    # Hermetic HOME so provider config / caches resolve off a
                    # scratch dir, not the real HOME.
                    "HOME": str(runner_home),
                    # The stub shadows any real claude on PATH.
                    "PATH": f"{stub_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                }
            ),
            stdout=runner_log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        online = False
        while time.monotonic() < deadline:
            try:
                status = _http.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2.0)
                if status.status_code == 200 and status.json().get("online") is True:
                    online = True
                    break
            except httpx.HTTPError:
                pass
            time.sleep(_POLL_S)
        assert online, (
            f"runner never came online; log:\n{(tmp_path / 'runner.log').read_text()[-3000:]}"
        )

        _http.patch(
            f"{base_url}/v1/sessions/{parent_session_id}",
            json={"runner_id": runner_id},
            timeout=30.0,
        ).raise_for_status()

        yield _Stack(
            base_url=base_url,
            runner_id=runner_id,
            parent_session_id=parent_session_id,
            capture=capture,
            tmp_path=tmp_path,
        )
    finally:
        _terminate(runner_proc)
        _terminate(server_proc)
        capture.close()
        server_log.close()
        runner_log.close()


def _bind_session(stack: _Stack, session_id: str) -> None:
    _http.patch(
        f"{stack.base_url}/v1/sessions/{session_id}",
        json={"runner_id": stack.runner_id},
        timeout=30.0,
    ).raise_for_status()


def _ensure_claude_terminal(stack: _Stack, session_id: str) -> httpx.Response:
    """The exact request the ``omnigent claude`` wrapper / native bootstrap sends."""
    return _http.post(
        f"{stack.base_url}/v1/sessions/{session_id}/resources/terminals",
        json={
            "terminal": "claude",
            "session_key": "main",
            "ensure_native_terminal": True,
        },
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        timeout=_ENSURE_TIMEOUT_S,
    )


def _extract_error_id(error: dict[str, object]) -> str:
    error_id = error.get("error_id")
    if isinstance(error_id, str) and error_id.startswith("err_"):
        return error_id
    match = re.search(r"Error ID: (err_[0-9a-f]{32})", str(error.get("message", "")))
    assert match is not None, f"no error id in error payload: {error!r}"
    return match.group(1)


def _wait_for_diagnostic_row(stack: _Stack, error_id: str) -> dict[str, object]:
    """Return the persisted startup-diagnostic row naming *error_id*.

    The payload builder's log line embeds ``error_id=<id>`` in the message, so
    the id ties the row to the exact error payload the child session received.
    """
    deadline = time.monotonic() + _ROW_TIMEOUT_S
    while time.monotonic() < deadline:
        for row in stack.capture.rows():
            if f"error_id={error_id}" in str(row.get("message", "")):
                return row
        time.sleep(0.5)
    messages = [str(row.get("message", ""))[:160] for row in stack.capture.rows()]
    raise AssertionError(
        f"debug-log sink never shipped the startup diagnostic for {error_id!r}; "
        f"captured {len(messages)} row(s):\n" + "\n".join(messages[-40:]) + "\n"
        f"runner log tail:\n{stack.runner_log_tail()}"
    )


def _assert_row_correlates(row: dict[str, object], child_id: str, error_id: str) -> None:
    assert row.get("session_id") == child_id, (
        f"startup diagnostic row is attributed to session {row.get('session_id')!r}, "
        f"not the failed child session {child_id!r}; row: {row!r}"
    )
    attributes = row.get("attributes")
    assert isinstance(attributes, dict) and attributes.get("error_id") == error_id, (
        f"startup diagnostic row does not persist a searchable attributes.error_id "
        f"({error_id!r}); attributes: {attributes!r}"
    )


def test_native_terminal_start_failure_row_correlates_to_failed_child(
    tmp_path: Path,
) -> None:
    """A child session's generic native-terminal startup failure must persist a
    debug-log diagnostic carrying the CHILD's session id and a searchable
    ``attributes.error_id`` matching the error payload the child received."""
    with _native_stack(tmp_path, claude_stub_script="#!/bin/sh\nexit 7\n") as stack:
        child_id, _child_agent_id = _create_session_with_scoped_agent(
            stack.base_url, "failing-child-fixture"
        )
        assert child_id != stack.parent_session_id
        _bind_session(stack, child_id)

        ensure = _ensure_claude_terminal(stack, child_id)
        assert ensure.status_code == 500, (
            f"expected the startup failure to surface as HTTP 500, got "
            f"{ensure.status_code}: {ensure.text}\nrunner log:\n{stack.runner_log_tail()}"
        )
        error = ensure.json()["error"]
        assert error.get("code") == "native_terminal_start_failed", error
        error_id = _extract_error_id(error)

        row = _wait_for_diagnostic_row(stack, error_id)
        _assert_row_correlates(row, child_id, error_id)


def test_missing_agent_lifecycle_row_correlates_to_failed_child(
    tmp_path: Path,
) -> None:
    """The expected missing-agent lifecycle failure (agent deleted while the
    session persists) must likewise persist its diagnostic against the failed
    child session with a searchable ``attributes.error_id``."""
    with _native_stack(tmp_path, claude_stub_script="#!/bin/sh\nexec sleep 600\n") as stack:
        child_id, child_agent_id = _create_session_with_scoped_agent(
            stack.base_url, "missing-agent-child-fixture"
        )
        assert child_id != stack.parent_session_id

        # PRECONDITION (the reported lifecycle state): the child's agent is no
        # longer present in the store — deleted through the real store API.
        from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore

        database_uri = f"sqlite:///{stack.tmp_path / 'chat.db'}"
        assert SqlAlchemyAgentStore(database_uri).delete(child_agent_id) is True
        contents = _http.get(
            f"{stack.base_url}/v1/sessions/{child_id}/agent/contents",
            timeout=10.0,
        )
        assert contents.status_code == 404, contents.status_code

        _bind_session(stack, child_id)

        ensure = _ensure_claude_terminal(stack, child_id)
        assert ensure.status_code == 410, (
            f"expected HTTP 410 for the missing-agent lifecycle condition, got "
            f"{ensure.status_code}: {ensure.text}\nrunner log:\n{stack.runner_log_tail()}"
        )
        error = ensure.json()["error"]
        assert error.get("code") == "session_agent_missing", error
        error_id = _extract_error_id(error)

        row = _wait_for_diagnostic_row(stack, error_id)
        _assert_row_correlates(row, child_id, error_id)
