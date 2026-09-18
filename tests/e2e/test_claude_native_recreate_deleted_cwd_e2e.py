"""E2E regression test: recreating the native Claude terminal must not depend
on the runner's process cwd when a valid workspace is configured.

Reproduces "Native Claude recreation depends on a removed process cwd". A
directly-spawned runner keeps the cwd it was launched from (only the
zygote-fork path chdirs). When that launch directory is later removed while the
runner stays alive -- e.g. the worktree the runner booted from is deleted -- any
code that reads ``Path.cwd()`` / ``os.getcwd()`` raises ``FileNotFoundError``
on Linux.

Native Claude terminal (re)creation reads the process cwd on several code
paths that fire during a launch, and at least one does so *eagerly even when a
valid workspace is available*:

* ``_auto_create_claude_terminal`` resolves the workspace as
  ``os.environ.get("OMNIGENT_RUNNER_WORKSPACE", str(Path.cwd()))`` -- the
  ``str(Path.cwd())`` default argument is evaluated before the ``.get()`` call
  runs, so it raises even though ``OMNIGENT_RUNNER_WORKSPACE`` names a valid,
  present directory; and the ensure/recreate route reaches this with
  ``session_init=None`` (no snapshot workspace to fall back on);
* ``load_effective_config`` -> ``load_local_config`` resolves
  ``Path.cwd() / <local config>`` unguarded; and
* ``build_native_relay_tool_schemas`` builds an ``OSEnvSpec`` with
  ``cwd=str(Path.cwd())`` outside its try guard.

So a native Claude terminal recreation triggered after the launch directory is
gone fails (HTTP 500 ``native_terminal_start_failed`` / "Native Claude terminal
failed to start") instead of launching in the configured workspace.

This drives the REAL user journey end to end against a real ``omnigent server``
subprocess and a real runner subprocess:

1. Launch the runner from a dedicated launch directory (its process cwd) with a
   *separate, valid* ``OMNIGENT_RUNNER_WORKSPACE`` -- the state a host/runner
   booted from a worktree is in.
2. Create a claude-native (``omnigent claude``) session, exactly like the CLI
   wrapper, bound to a real session-scoped agent that stays present.
3. Remove the runner's launch directory while the runner stays alive -- the
   reported precondition (the worktree the runner booted from is deleted).
4. Bind the session to the runner (what the web UI / a relaunch does), which
   triggers the Claude terminal (re)creation now that the launch cwd is gone.
5. Trigger the exact request the ``omnigent claude`` wrapper / native bootstrap
   sends to recreate/ensure the Claude terminal --
   ``POST /v1/sessions/<id>/resources/terminals`` with
   ``{"terminal": "claude", "session_key": "main", "ensure_native_terminal":
   true}``.

Nothing about the failure is hand-fabricated: the agent is valid, the workspace
is valid and present, and the only injected fault is the removed launch cwd --
the running server+runner produce the failure themselves. A control run with
the launch cwd intact returns HTTP 200 with the terminal running, isolating the
removed cwd as the sole cause.

Regression target (fail -> pass): today the ensure returns HTTP 500 with a
``FileNotFoundError`` from the cwd read; after the fix, native Claude terminal
recreation resolves the configured workspace and succeeds despite the removed
launch cwd.

Run::

    .venv/bin/python -m pytest \\
        tests/e2e/test_claude_native_recreate_deleted_cwd_e2e.py -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# CI shells can carry an egress proxy; every HTTP call targets 127.0.0.1, so
# bypass proxy autodetection entirely.
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
# The server route runs ensure_runner_connected before the recreate; give it a
# generous budget.
_ENSURE_TIMEOUT_S = 90.0

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

    Starts the spawned ``omnigent server`` / runner from a CLEAN omnigent
    context: any ``OMNIGENT*`` / ``RUNNER_SERVER_URL`` inherited from a parent
    runner (e.g. when this test itself runs inside an omnigent runner session)
    is stripped, then re-supplied only via *extra*. Otherwise a leaked
    ``OMNIGENT_RUNNER_ZYGOTE_HARNESS_FD`` / ``OMNIGENT_RUNNER_ID`` /
    ``OMNIGENT_DATA_DIR`` makes the fresh runner adopt a stale identity or
    redirect its logs, so it never registers.

    :param extra: Overrides/additions applied after the base env.
    :returns: Environment mapping for ``subprocess.Popen``.
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


def _create_claude_native_session(base_url: str) -> tuple[str, str]:
    """Create a claude-native wrapper session exactly like ``omnigent claude``.

    Reuses the production spec materializer and stamps the same wrapper /
    terminal-first labels the CLI writes, so the server treats the session as a
    native-terminal session and the ensure routes into the runner's Claude
    terminal (re)creation path.

    :param base_url: Spawned server base URL.
    :returns: ``(session_id, agent_id)`` for the new claude-native session and
        its session-scoped agent.
    """
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


def test_native_claude_recreate_survives_removed_launch_cwd(tmp_path: Path) -> None:
    """
    Recreating the native Claude terminal must succeed when the runner's launch
    directory (its process cwd) has been removed but ``OMNIGENT_RUNNER_WORKSPACE``
    still names a valid, present workspace.

    Journey: the runner is launched from a dedicated launch directory with a
    separate valid workspace; a claude-native session is created and bound; the
    launch directory is removed while the runner stays alive (the worktree it
    booted from is deleted); the Claude terminal is recreated/ensured (the
    ``omnigent claude`` wrapper / native bootstrap request).

    Correct behavior: the recreation resolves the configured workspace and
    succeeds (HTTP < 400). Today it fails with HTTP 500
    ``native_terminal_start_failed`` because the (re)creation path reads
    ``Path.cwd()`` -- most eagerly the ``str(Path.cwd())`` default argument in
    the ``OMNIGENT_RUNNER_WORKSPACE`` fallback, which is evaluated even though
    the env var is set -- and ``os.getcwd()`` raises ``FileNotFoundError`` for
    the removed directory.

    :param tmp_path: Per-test temp dir (server DB, stub claude, HOMEs, the
        deletable runner launch dir, and the valid workspace).
    """
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    db_path = tmp_path / "chat.db"
    database_uri = f"sqlite:///{db_path}"

    # The valid, present workspace the recreation must resolve to.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # The runner's process cwd -- removed later while the runner stays alive.
    launch_cwd = tmp_path / "runner-launch-cwd"
    launch_cwd.mkdir()
    runner_home = tmp_path / "home"
    runner_home.mkdir()
    server_home = tmp_path / "server-home"
    server_home.mkdir()
    # tmux's unix socket lives under TMPDIR and AF_UNIX socket paths cap at
    # ~108 chars, so root the runtime tmp at a short /tmp dir rather than the
    # long per-test tmp_path (CI's sharded basetemp overflows the limit).
    tmp_parent = "/tmp" if os.path.isdir("/tmp") and os.access("/tmp", os.W_OK) else None
    runtime_tmp = Path(tempfile.mkdtemp(prefix="oterm_", dir=tmp_parent))
    harness_tmp = tmp_path / "harness"
    harness_tmp.mkdir()
    runtime_env = {
        "TMPDIR": str(runtime_tmp),
        "OMNIGENT_HARNESS_TMP_PARENT": str(harness_tmp),
    }

    # Stub Claude CLI on PATH -- defense so the test never blocks on a real
    # (unauthenticated) Claude TUI; the cwd read fails before claude launches.
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
                    **runtime_env,
                    "HOME": str(server_home),
                    "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token,
                }
            ),
            stdout=server_log,
            stderr=subprocess.STDOUT,
        )
        _wait_http_ok(f"{base_url}/health", time.monotonic() + _HEALTH_TIMEOUT_S)

        # Launch the runner FROM the deletable launch dir (its process cwd) with
        # a SEPARATE, valid workspace. A directly-spawned runner keeps this cwd.
        runner_proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            cwd=str(launch_cwd),
            env=_localhost_env(
                {
                    **runtime_env,
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

        # A launched claude-native session bound to a real, present agent.
        session_id, _agent_id = _create_claude_native_session(base_url)

        # PRECONDITION (the reported state): remove the runner's launch dir
        # while the runner stays alive. os.getcwd()/Path.cwd() now raises for
        # the runner process. The configured workspace is untouched and valid.
        # This is applied BEFORE binding: binding immediately triggers the
        # terminal auto-create, so the launch cwd must already be gone for the
        # (re)creation to run against a removed cwd rather than caching a
        # terminal built while it still existed.
        shutil.rmtree(launch_cwd)

        # Bind the session to the runner (what the web UI / a relaunch does),
        # which triggers the Claude terminal (re)creation with the launch cwd
        # already removed.
        _http.patch(
            f"{base_url}/v1/sessions/{session_id}",
            json={"runner_id": runner_id},
            timeout=30.0,
        ).raise_for_status()

        # THE TRIGGER: recreate/ensure the native Claude terminal -- the exact
        # request the ``omnigent claude`` wrapper / native bootstrap sends.
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

        log_dir = runner_home / ".omnigent" / "logs" / "runner"

        def _runner_log_text() -> str:
            """Union the stdout capture and any rotated runner log files."""
            texts: list[str] = []
            with contextlib.suppress(OSError):
                texts.append((tmp_path / "runner.log").read_text())
            for candidate in log_dir.glob("runner-*.log"):
                try:
                    texts.append(candidate.read_text())
                except OSError:
                    continue
            return "\n".join(texts)

        # If the recreation failed, confirm it failed via the reported
        # removed-cwd read (a FileNotFoundError from os.getcwd()) rather than
        # incidental noise, so this test fails specifically on this defect. The
        # ensure response can beat the log flush, so poll briefly.
        if ensure.status_code >= 400:
            runner_log_text = ""
            fault_deadline = time.monotonic() + 15.0
            while time.monotonic() < fault_deadline:
                runner_log_text = _runner_log_text()
                if "FileNotFoundError" in runner_log_text and "getcwd" in runner_log_text:
                    break
                time.sleep(0.5)
            assert "FileNotFoundError" in runner_log_text and "getcwd" in runner_log_text, (
                f"recreation failed ({ensure.status_code}: {ensure.text}) but not via "
                f"the reported removed-cwd read; runner log tail:\n"
                f"{runner_log_text[-3000:]}"
            )

        # REGRESSION GUARD: recreation must succeed despite the removed launch
        # cwd, because OMNIGENT_RUNNER_WORKSPACE names a valid workspace.
        assert ensure.status_code < 400, (
            "native Claude terminal recreation must not depend on the runner's "
            "removed process cwd when OMNIGENT_RUNNER_WORKSPACE is valid; got "
            f"{ensure.status_code}: {ensure.text}\n"
            f"runner log tail:\n{_runner_log_text()[-3000:]}"
        )
    finally:
        _terminate(runner_proc)
        _terminate(server_proc)
        server_log.close()
        runner_log.close()
        shutil.rmtree(runtime_tmp, ignore_errors=True)
