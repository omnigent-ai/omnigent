"""E2E regression test: a deleted session agent surfaces a distinct,
client-safe error — not a generic native-terminal startup failure.

Guards the fault-attribution fix for "native Claude terminal startup and
ensure failures". When a claude-native session's bound agent is no longer
present in the agent store, ensuring the Claude terminal (or dispatching a
turn that ensures it) resolves the missing spec and fails. Previously that one
session-lifecycle condition was surfaced at three layers as a generic
``native_terminal_start_failed`` / ``Native Claude terminal failed to start``
startup defect, so it was counted against the terminal-startup error signal:

* the runner's ``_ensure_native_terminal`` -> ``_claude_ensure_build`` ->
  ``_resolve_session_spec_entry`` cannot resolve the agent (the server's
  ``GET /agent/contents`` 404s) and raises ``session spec resolver: agent
  '<hex>' for session '<id>' was not found``;
* the runner returned that as a generic native-terminal-start failure, which
  the server's ``create_session_terminal`` route re-raised verbatim; and
* inside a turn, ``_publish_status`` logged ``session turn failed for <id>:
  Native Claude terminal failed to start; ...``.

The agent-not-found cause is a genuine session-lifecycle event (the agent was
deleted or rebound out from under a live session), not a tmux/FileNotFound
infra fault or an "exited before available" teardown. The fix classifies it
with its own ``ErrorCode.SESSION_AGENT_MISSING`` (410 Gone, USER category):
the runner raises that code, and the native-terminal error builder detects it
and surfaces a distinct, client-safe message pointing the user at recreating
the agent / starting a new session — while the runner log still carries the
spec-resolver cause for operators. This test asserts that corrected
attribution end to end: the surfaced code/message reflect the lifecycle
condition, and the cleared ``Claude terminal ensure failed for session=``
startup-defect log line no longer fires.

This drives the REAL user journey end to end against a real ``omnigent
server`` subprocess and a real runner subprocess:

1. Create a claude-native-eligible session bound to a real (session-scoped)
   agent — the state a launched session is in.
2. Establish the reported precondition faithfully via the real store API:
   the agent the session is bound to is no longer present in the agent store
   (deleted/rotated/GC'd while the session persists).
3. Bind the session to the runner.
4. Trigger the exact request the ``omnigent claude`` wrapper / native
   bootstrap sends — ``POST /v1/sessions/<id>/resources/terminals`` with
   ``{"terminal": "claude", "session_key": "main", "ensure_native_terminal":
   true}`` — so the runner's genuine ensure path runs.

The genuine ensure path (``_ensure_native_terminal`` ->
``_claude_ensure_build`` -> ``_resolve_session_spec_entry`` -> the server's
``GET /agent/contents`` returning 404 because the agent is gone) then produces
the reported 500 body and the reported runner-log signature. Nothing about the
error body or the failure state is hand-fabricated: the store delete is the
precondition, and the running server+runner produce the failure themselves.

The Claude CLI is replaced with a tiny stub on PATH purely as defense — the
failure happens at spec resolution, before any ``claude`` process is launched.

Run::

    .venv/bin/python -m pytest \\
        tests/e2e/test_claude_native_terminal_missing_agent_e2e.py -v
"""

from __future__ import annotations

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
import time
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

# First-party sentinel Origin so the multipart create passes the
# require_trusted_origin guard regardless of which client issues it.
from omnigent.runner.identity import (  # noqa: E402
    OMNIGENT_INTERNAL_WS_ORIGIN,
    token_bound_runner_id,
)

_HEALTH_TIMEOUT_S = 120.0
_POLL_S = 1.0
# The ensure fails at spec resolution (one server round-trip), but the server
# route also runs ensure_runner_connected first; give it a generous budget.
_ENSURE_TIMEOUT_S = 90.0

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="claude-native terminals run inside tmux; tmux not installed",
)


def _find_free_port() -> int:
    """Grab an ephemeral port for the spawned server."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _localhost_env(extra: dict[str, str]) -> dict[str, str]:
    """Subprocess env with worktree imports, no proxy, and no leaked runner ctx.

    Starts the spawned ``omnigent server`` / runner from a CLEAN omnigent
    context: any ``OMNIGENT*`` / ``RUNNER_SERVER_URL`` inherited from a parent
    runner (e.g. when this test itself runs inside an omnigent runner session)
    is stripped, then re-supplied only via *extra*. Without this, a leaked
    ``OMNIGENT_RUNNER_ZYGOTE_HARNESS_FD`` makes the fresh runner try to adopt a
    non-existent zygote FD, a leaked ``OMNIGENT_PROCESS_LOG_FILE`` /
    ``OMNIGENT_DATA_DIR`` redirects its logs away from this test's HOME, and a
    leaked ``OMNIGENT_RUNNER_ID`` collides with the test's identity — so the
    runner never registers and the log assertions find nothing.

    :param extra: Overrides/additions applied after the base env.
    :returns: Environment mapping for ``subprocess.Popen``.
    """
    env = {
        **os.environ,
        "PYTHONPATH": _PYTHONPATH,
        # CI shells often carry an egress proxy; localhost must bypass it.
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(name, None)
    # Strip a leaked parent-runner context so the spawned server/runner boot
    # from a clean slate; the test re-supplies exactly what it needs via extra.
    for name in list(env):
        if name.startswith("OMNIGENT") or name == "RUNNER_SERVER_URL":
            env.pop(name, None)
    env.update(extra)
    return env


def _terminate(proc: subprocess.Popen[bytes] | None) -> None:
    """Best-effort SIGTERM -> SIGKILL teardown for a spawned process."""
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _wait_http_ok(url: str, deadline: float) -> None:
    """Poll *url* until it returns 200 or *deadline* (monotonic) passes."""
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


def _create_session_with_scoped_agent(base_url: str) -> tuple[str, str]:
    """Create a session bound to a fresh (session-scoped) agent.

    Uploads a minimal ``openai-agents`` agent bundle via the multipart
    ``POST /v1/sessions`` create path — exactly how a launched session is
    registered — and returns both ids. The agent NAME does not matter here:
    the Claude native adapter is selected by the ensured *terminal* name
    (``"claude"``), so a plain agent still routes into ``_claude_ensure_build``.

    :param base_url: Spawned server base URL.
    :returns: ``(session_id, agent_id)`` for the created session and its
        session-scoped agent.
    """
    yaml_text = "\n".join(
        [
            "name: missing-agent-fixture",
            "description: Minimal agent whose bundle is later removed from the store.",
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
        info = tarfile.TarInfo("missing-agent-fixture.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    create = _http.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={
            "bundle": (
                "missing-agent-fixture.tar.gz",
                buf.getvalue(),
                "application/gzip",
            )
        },
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        timeout=30.0,
    )
    create.raise_for_status()
    body = create.json()
    return str(body["session_id"]), str(body["agent_id"])


def test_native_claude_terminal_ensure_fails_when_agent_missing(
    tmp_path: Path,
) -> None:
    """
    Ensuring the native Claude terminal must fail cleanly when the session's
    bound agent can no longer be resolved from the agent store.

    Journey: a claude-native-eligible session exists bound to an agent; the
    agent is no longer present in the store when the Claude terminal is
    ensured (the ``omnigent claude`` wrapper / native bootstrap request); the
    runner's ``_ensure_native_terminal`` -> ``_claude_ensure_build`` ->
    ``_resolve_session_spec_entry`` can't resolve the agent (the server
    ``GET /agent/contents`` 404s), so it raises ``session spec resolver:
    agent '<id>' ... was not found``.

    Corrected behavior: the runner classifies this as the session-lifecycle
    condition ``session_agent_missing`` (410 Gone) and returns a structured
    error whose client-safe message says the session's agent is no longer
    available and to recreate the agent / start a new session (with a
    correlation ``Error ID: err_...``) — NOT the generic
    ``native_terminal_start_failed`` / "Native Claude terminal failed to
    start" startup defect. The runner log still records the spec-resolver
    cause (``session spec resolver: agent ... was not found``) and the
    correlation id for operators, but the generic ``Claude terminal ensure
    failed for session=`` startup-defect line no longer fires — the ensure is
    logged as a skipped lifecycle event instead.

    :param tmp_path: Per-test temp dir (server DB, stub claude, runner HOME).
    """
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    db_path = tmp_path / "chat.db"
    database_uri = f"sqlite:///{db_path}"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner_home = tmp_path / "home"
    runner_home.mkdir()

    # Stub Claude CLI on PATH — pure defense. The ensure fails at spec
    # resolution before any ``claude`` process launches, but a stub guarantees
    # the test never blocks on a real (unauthenticated) Claude TUI even if the
    # code path ever changed.
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    stub = stub_bin / "claude"
    stub.write_text("#!/bin/sh\nexec sleep 600\n")
    stub.chmod(0o755)

    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)

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

        runner_proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=_localhost_env(
                {
                    "OMNIGENT_RUNNER_ID": runner_id,
                    "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
                    "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
                    "RUNNER_SERVER_URL": base_url,
                    "OMNIGENT_RUNNER_WORKSPACE": str(workspace),
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
                # The server/runner is still booting; transient connection
                # errors are expected while polling and simply retried.
                pass
            time.sleep(_POLL_S)
        assert online, (
            f"runner never came online; log:\n{(tmp_path / 'runner.log').read_text()[-3000:]}"
        )

        # A launched session bound to a real agent — the state a user is in.
        session_id, agent_id = _create_session_with_scoped_agent(base_url)

        # PRECONDITION (the reported state): the agent the session is bound to
        # is no longer present in the store. Apply it through the real store
        # API BEFORE binding the runner, so no successful spec resolution can
        # be cached first. This is a genuine precondition (agent
        # deleted/rotated/GC'd while the session persists), not a fabricated
        # end-state — the failure body itself is produced by the live path.
        from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore

        deleted = SqlAlchemyAgentStore(database_uri).delete(agent_id)
        assert deleted is True, f"expected to delete agent {agent_id!r} from the store"

        # Sanity: the server can no longer serve the agent bundle the runner's
        # spec resolver fetches — the 404 the resolver turns into "not found".
        contents = _http.get(
            f"{base_url}/v1/sessions/{session_id}/agent/contents",
            timeout=10.0,
        )
        assert contents.status_code == 404, (
            f"expected agent contents to 404 after delete, got {contents.status_code}"
        )

        # Bind the session to the runner (what the web UI / a relaunch does).
        _http.patch(
            f"{base_url}/v1/sessions/{session_id}",
            json={"runner_id": runner_id},
            timeout=30.0,
        ).raise_for_status()

        # THE TRIGGER: ensure the native Claude terminal — the exact request
        # the ``omnigent claude`` wrapper / native bootstrap sends. The
        # terminal NAME "claude" routes the runner into _claude_ensure_build,
        # which resolves the (now-missing) session spec and fails.
        ensure = _http.post(
            f"{base_url}/v1/sessions/{session_id}/resources/terminals",
            json={
                "terminal": "claude",
                "session_key": "main",
                "ensure_native_terminal": True,
            },
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
            timeout=_ENSURE_TIMEOUT_S,
        )

        # The ensure fails as a distinct session-lifecycle error, NOT a
        # generic terminal-startup fault. The code maps to 410 Gone (the
        # server re-derives the client status from the structured code).
        assert ensure.status_code == 410, (
            f"expected HTTP 410 for the missing-agent lifecycle condition, got "
            f"{ensure.status_code}: {ensure.text}\n"
            f"runner log:\n{(tmp_path / 'runner.log').read_text()[-3000:]}"
        )
        error = ensure.json()["error"]
        message = error["message"]
        assert error.get("code") == "session_agent_missing", (
            f"expected session_agent_missing code, got {error!r}"
        )
        # The corrected client-safe message names the lifecycle cause and the
        # remedy — it must NOT relabel this as a terminal-startup defect.
        assert "agent is no longer available" in message, message
        assert "Native Claude terminal failed to start" not in message, message
        # The message carries a correlation id for operators to cross-ref the
        # runner log.
        error_id_match = re.search(r" Error ID: (err_[0-9a-f]{32})\.$", message)
        assert error_id_match is not None, f"message has no error ID: {message!r}"
        error_id = error_id_match.group(1)

        # The runner log still carries the agent-not-found spec-resolver cause
        # (for operators) that the client-safe message deliberately withholds,
        # but the generic ``ensure failed`` startup-defect line is gone — the
        # ensure is logged as a SKIPPED lifecycle event. Poll briefly: the
        # ensure HTTP response can beat the log handler's flush to disk.
        log_dir = runner_home / ".omnigent" / "logs" / "runner"

        def _runner_log_text() -> str:
            """Union all sibling runner logs (robust to the rotated file name)."""
            texts: list[str] = []
            for candidate in log_dir.glob("runner-*.log"):
                try:
                    texts.append(candidate.read_text())
                except OSError:
                    continue
            return "\n".join(texts)

        runner_log_text = ""
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            runner_log_text = _runner_log_text()
            if "terminal ensure skipped" in runner_log_text:
                break
            time.sleep(0.5)

        # The lifecycle event is logged as a skip, not a startup defect.
        assert "terminal ensure skipped" in runner_log_text, (
            f"runner log missing the ensure-skipped lifecycle signature; tail:\n"
            f"{runner_log_text[-3000:]}"
        )
        # The cleared KPI defect signature must NOT fire for this condition.
        assert "Claude terminal ensure failed for session=" not in runner_log_text, (
            f"runner log still emits the generic terminal-startup defect line "
            f"for a missing-agent lifecycle event; tail:\n{runner_log_text[-3000:]}"
        )
        # The spec-resolver cause remains in the log for operators.
        assert "session spec resolver: agent" in runner_log_text, (
            f"runner log missing the spec-resolver cause; tail:\n"
            f"{runner_log_text[-3000:]}"
        )
        assert "was not found" in runner_log_text, (
            f"runner log missing the agent-not-found cause; tail:\n"
            f"{runner_log_text[-3000:]}"
        )
        assert error_id in runner_log_text, (
            f"runner log missing correlation id {error_id!r}; tail:\n"
            f"{runner_log_text[-3000:]}"
        )
        # The client-safe message must NOT leak the raw agent id / cause.
        assert "session spec resolver" not in message, message
    finally:
        _terminate(runner_proc)
        _terminate(server_proc)
        server_log.close()
        runner_log.close()


def _create_claude_native_session_with_agent(base_url: str) -> tuple[str, str]:
    """Create a claude-native wrapper session exactly like ``omnigent claude``.

    Reuses the production spec materializer and stamps the same wrapper /
    terminal-first labels the CLI writes, so the server treats the session as
    a native-terminal session (``_is_native_terminal_session`` is True) and a
    plain user message drives the native Claude terminal ensure at turn
    dispatch.

    :param base_url: Spawned server base URL.
    :returns: ``(session_id, agent_id)`` for the new claude-native session and
        its session-scoped agent.
    """
    import tempfile

    from omnigent._wrapper_labels import (
        CLAUDE_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )
    from omnigent.harnesses.claude_native.main import _materialize_claude_agent_spec

    with tempfile.TemporaryDirectory() as tmp:
        yaml_text = _materialize_claude_agent_spec(Path(tmp)).read_text()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        # Non-config.yaml arcname routes through the omnigent compat translator
        # (the wrapper spec carries no ``spec_version``).
        info = tarfile.TarInfo("claude-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    labels = {
        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY: CLAUDE_NATIVE_WRAPPER_VALUE,
    }
    create = _http.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({"labels": labels})},
        files={
            "bundle": (
                "claude-native-ui.tar.gz",
                buf.getvalue(),
                "application/gzip",
            )
        },
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        timeout=30.0,
    )
    create.raise_for_status()
    body = create.json()
    return str(body["session_id"]), str(body["agent_id"])


def test_native_claude_turn_fails_when_agent_missing(tmp_path: Path) -> None:
    """
    A user turn on a claude-native session must surface a clean terminal-start
    failure when the session's bound agent can no longer be resolved.

    Journey (the dominant, turn-dispatch surface): a claude-native
    (``omnigent claude``) session exists bound to an agent; the agent is no
    longer present in the store (deleted/rotated/GC'd while the session
    persists); the user sends a message. For a native-terminal session the
    server's turn dispatch ensures the Claude terminal before forwarding
    (``_ensure_native_terminal_ready`` -> the runner's ensure); with the agent
    gone the ensure fails, so the turn funnels through
    ``_persist_native_terminal_failure``, which:

    * persists a ``type=\"error\"`` turn item — the banner the web UI renders
      on the failed turn; and
    * logs ``session turn failed for <id>: <message>`` at ERROR
      (``_publish_status``).

    Corrected behavior: the persisted item and the logged message carry the
    distinct ``session_agent_missing`` code and the client-safe lifecycle
    message, NOT the generic ``native_terminal_start_failed`` / "Native Claude
    terminal failed to start" startup-defect banner — so a failed turn caused
    by a deleted agent is attributed to the lifecycle condition rather than
    counted against the native-terminal startup error signal.

    Nothing here is hand-fabricated: the store delete is the precondition; the
    running server+runner produce the persisted item, the failed status, and
    the log line themselves.

    :param tmp_path: Per-test temp dir (server DB, stub claude, HOMEs).
    """
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    db_path = tmp_path / "chat.db"
    database_uri = f"sqlite:///{db_path}"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner_home = tmp_path / "home"
    runner_home.mkdir()
    # A scratch HOME for the server so its file log — which carries the
    # ``session turn failed for`` line; the stderr mirror is off for a
    # non-interactive subprocess — lands somewhere the test can read.
    server_home = tmp_path / "server-home"
    server_home.mkdir()

    # Stub Claude CLI on PATH — pure defense; the ensure fails at spec
    # resolution before any ``claude`` process launches.
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    stub = stub_bin / "claude"
    stub.write_text("#!/bin/sh\nexec sleep 600\n")
    stub.chmod(0o755)

    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)

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
            env=_localhost_env(
                {
                    "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token,
                    "HOME": str(server_home),
                }
            ),
            stdout=server_log,
            stderr=subprocess.STDOUT,
        )
        _wait_http_ok(f"{base_url}/health", time.monotonic() + _HEALTH_TIMEOUT_S)

        runner_proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=_localhost_env(
                {
                    "OMNIGENT_RUNNER_ID": runner_id,
                    "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
                    "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
                    "RUNNER_SERVER_URL": base_url,
                    "OMNIGENT_RUNNER_WORKSPACE": str(workspace),
                    "HOME": str(runner_home),
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
                # Still booting; transient connection errors are retried.
                pass
            time.sleep(_POLL_S)
        assert online, (
            f"runner never came online; log:\n{(tmp_path / 'runner.log').read_text()[-3000:]}"
        )

        # A launched claude-native session bound to a real agent.
        session_id, agent_id = _create_claude_native_session_with_agent(base_url)

        # PRECONDITION (the reported state): the bound agent is gone from the
        # store before the terminal ever comes up, so the turn's ensure must
        # resolve the (now-missing) spec and fail. Applied via the real store
        # API — a genuine precondition, not a fabricated end-state.
        from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore

        deleted = SqlAlchemyAgentStore(database_uri).delete(agent_id)
        assert deleted is True, f"expected to delete agent {agent_id!r} from the store"

        contents = _http.get(
            f"{base_url}/v1/sessions/{session_id}/agent/contents",
            timeout=10.0,
        )
        assert contents.status_code == 404, (
            f"expected agent contents to 404 after delete, got {contents.status_code}"
        )

        # Bind the session to the runner (what the web UI / a relaunch does).
        _http.patch(
            f"{base_url}/v1/sessions/{session_id}",
            json={"runner_id": runner_id},
            timeout=30.0,
        ).raise_for_status()

        # THE TRIGGER: the user sends a message — the web-UI turn path. For a
        # native-terminal session the server dispatch ensures the Claude
        # terminal before forwarding; with the agent gone the ensure fails and
        # the turn is recorded as a native-terminal-start failure.
        send = _http.post(
            f"{base_url}/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                },
            },
            timeout=_ENSURE_TIMEOUT_S,
        )
        assert send.status_code < 500, (
            f"unexpected server error posting the turn: {send.status_code}: {send.text}\n"
            f"runner log:\n{(tmp_path / 'runner.log').read_text()[-2000:]}"
        )

        # The failed turn persists a ``type=\"error\"`` item — the banner the
        # web UI renders on the failed turn. It must carry the distinct
        # lifecycle code, not the generic startup-defect banner. Poll until it
        # lands (the dispatch awaits the ensure, but persistence/read can lag).
        def _error_items() -> list[dict]:
            resp = _http.get(
                f"{base_url}/v1/sessions/{session_id}/items",
                params={"limit": 50, "order": "desc"},
                timeout=10.0,
            )
            if resp.status_code != 200:
                return []
            return [it for it in resp.json().get("data", []) if it.get("type") == "error"]

        error_item: dict | None = None
        deadline = time.monotonic() + _ENSURE_TIMEOUT_S
        while time.monotonic() < deadline:
            for it in _error_items():
                blob = json.dumps(it)
                if (
                    "session_agent_missing" in blob
                    and "agent is no longer available" in blob
                ):
                    error_item = it
                    break
            if error_item is not None:
                break
            time.sleep(_POLL_S)

        assert error_item is not None, (
            "no session-agent-missing error turn item was persisted after the "
            "user turn; runner log:\n"
            f"{(tmp_path / 'runner.log').read_text()[-2000:]}\n"
            f"server log:\n{(tmp_path / 'server.log').read_text()[-2000:]}"
        )
        # ``/items`` flattens the item's typed data onto the top level
        # (``to_api_dict``), so the ``ErrorData`` fields ride there directly.
        assert error_item.get("code") == "session_agent_missing", error_item
        item_message = error_item.get("message") or ""
        assert "agent is no longer available" in item_message, error_item
        assert "Native Claude terminal failed to start" not in item_message, error_item

        # KPI log signature: the server logs the failed turn at ERROR with
        # the same message. Depending on the harness' data-dir
        # wiring the process log lands in the server's captured stdio and/or a
        # ``server-*.log`` under the data dir (``OMNIGENT_DATA_DIR`` or
        # ``$HOME/.omnigent``), so read the union and poll for the flush.
        server_stdio_log = tmp_path / "server.log"
        server_log_dirs = [server_home / ".omnigent" / "logs" / "server"]
        data_dir_env = os.environ.get("OMNIGENT_DATA_DIR")
        if data_dir_env:
            server_log_dirs.append(Path(data_dir_env).expanduser() / "logs" / "server")

        def _server_log_text() -> str:
            texts: list[str] = []
            try:
                texts.append(server_stdio_log.read_text())
            except OSError:
                pass
            for log_dir in server_log_dirs:
                for candidate in log_dir.glob("server-*.log"):
                    try:
                        texts.append(candidate.read_text())
                    except OSError:
                        continue
            return "\n".join(texts)

        needle = f"session turn failed for {session_id}"
        server_log_text = ""
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            server_log_text = _server_log_text()
            if needle in server_log_text:
                break
            time.sleep(0.5)
        assert needle in server_log_text, (
            f"server log missing the turn-failed signature {needle!r}; tail:\n"
            f"{server_log_text[-3000:]}"
        )
        # The turn-failed log now carries the lifecycle message, not the
        # generic terminal-startup defect message.
        assert "agent is no longer available" in server_log_text, (
            f"server log missing the lifecycle failure message; tail:\n"
            f"{server_log_text[-3000:]}"
        )
        assert "Native Claude terminal failed to start" not in server_log_text, (
            f"server log still emits the generic terminal-startup defect message "
            f"for a missing-agent lifecycle turn failure; tail:\n"
            f"{server_log_text[-3000:]}"
        )
    finally:
        _terminate(runner_proc)
        _terminate(server_proc)
        server_log.close()
        runner_log.close()
