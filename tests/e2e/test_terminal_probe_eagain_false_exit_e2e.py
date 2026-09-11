"""E2E regression test: tmux liveness-probe EAGAIN is misclassified as exit.

Reproduces a production incident: a Claude-native agent terminal was declared
``required_terminal_exited`` and the session failed, **even though the Claude
CLI never exited**. The host had temporarily exhausted its process/thread
task quota, so Omnigent could not *spawn* its tmux liveness probes:

    /bin/sh: Cannot fork
    BlockingIOError: [Errno 11] Resource temporarily unavailable

The runner's threaded idle watcher polls the pane every ~200 ms via
``tmux capture-pane`` (``TerminalInstance._tmux_output_sync`` ->
``subprocess.run``). Under fork exhaustion that spawn raises ``OSError`` which
``_tmux_output_sync`` converts to ``RuntimeError``; the watcher then confirms
the failure with ``tmux has-session`` — *another* subprocess spawn, which
fails with the same ``EAGAIN``. The confirmation therefore cannot tell "tmux
is gone" apart from "the OS could not fork a probe", so after
``_IDLE_EXIT_FAILURE_THRESHOLD`` (3) consecutive spawn failures (~0.6 s) the
watcher declares the terminal dead, the runner evicts the required terminal
and publishes ``session.status: failed`` with
``code=required_terminal_exited`` — a **false terminal-death
classification**. The last pane snapshot still showed Claude working, and no
real exit status was ever captured.

Journey (the reporter's, driven live here)
------------------------------------------
1. A Claude-native (native TUI) session is created and bound to a runner, so
   the runner auto-creates the required Claude terminal and it starts running
   (the pane redraws -> the session's status is ``running``, i.e. Claude is
   "working").
2. The host runs out of process/thread task quota, so tmux liveness probes
   can no longer be spawned (``EAGAIN`` on every ``capture-pane`` /
   ``has-session``) — while the already-running tmux server and the Claude
   pane stay alive.
3. Within ~1 s the session flips to ``failed`` and surfaces
   ``required_terminal_exited`` — even though the terminal never exited.

The fault is injected at the exact seam the incident hit: the tmux liveness
*probe* subprocess spawn. Only for the probe verbs (``capture-pane`` /
``list-panes`` / ``has-session``) does the underlying ``subprocess.run``
raise the ``BlockingIOError`` (``EAGAIN``) of a real fork failure; every
launch/attach tmux command and the underlying tmux server + Claude pane keep
running. The Claude CLI is a tiny
stub that keeps the pane redrawing (so the session stays ``running``) and
heart-beats a file, so the terminal is provably alive when Omnigent declares
it exited — the very definition of the misclassification. No Claude login is
needed.

Desired behaviour (asserted): a probe-spawn ``EAGAIN`` while the pane is
genuinely alive must be treated as *liveness unknown* (back off, do not fail
the terminal), so the session must **not** be reported as
``required_terminal_exited``. On the buggy build the session fails with
exactly that code, so this test FAILS with the observed error attached.

Run::

    .venv/bin/python -m pytest \\
        tests/e2e/test_terminal_probe_eagain_false_exit_e2e.py -v
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
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

# Plain server bootstrap (no store patching needed for this bug).
_SERVER_BOOTSTRAP = """
from omnigent.cli import main

main()
"""

# Runner bootstrap: inject the incident's fault at the tmux liveness-probe
# spawn seam. For probe verbs only, and only once the sentinel file exists,
# the ``subprocess.run`` under ``_tmux_output_sync`` raises the
# ``BlockingIOError`` (``EAGAIN``) of a real fork failure, so the production
# ``except OSError`` branch classifies it — the runner boots, the terminal
# launches, the pane runs, and the fault arms mid-run, modelling "the OS can
# no longer fork a liveness probe" (not "tmux died"). Every non-probe tmux
# command (launch, attach, kill) and the tmux server itself keep working.
_RUNNER_BOOTSTRAP = """
import errno
import os
import subprocess

import omnigent.inner.terminal as _term

_SENTINEL = os.environ["OMNI_EAGAIN_SENTINEL"]
_PROBE_VERBS = {"capture-pane", "list-panes", "has-session"}
_orig_tmux_output_sync = _term.TerminalInstance._tmux_output_sync
_real_run = subprocess.run


def _eagain_run(*args, **kwargs):
    raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")


def _patched_tmux_output_sync(self, *args):
    if args and args[0] in _PROBE_VERBS and os.path.exists(_SENTINEL):
        # Fail the probe at the real spawn seam: the OS refuses to fork the
        # tmux client, exactly like host task-quota exhaustion.
        _term.subprocess.run = _eagain_run
        try:
            return _orig_tmux_output_sync(self, *args)
        finally:
            _term.subprocess.run = _real_run
    return _orig_tmux_output_sync(self, *args)


_term.TerminalInstance._tmux_output_sync = _patched_tmux_output_sync

from omnigent.runner._entry import main

main()
"""

_HEALTH_TIMEOUT_S = 120.0
_POLL_S = 0.5
# Terminal auto-create includes bridge prep + tmux boot + a model-catalog
# probe; generous for CI.
_LAUNCH_TIMEOUT_S = 180.0
# After the fault arms, the watcher declares exit after 3 consecutive probe
# failures (~0.6 s at the 200 ms claude-native cadence); poll generously.
_FAILURE_OBSERVE_TIMEOUT_S = 30.0

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
    """Subprocess env with worktree imports and no proxy in the way.

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


def _create_claude_native_session(base_url: str) -> str:
    """Create a claude-native wrapper session exactly like ``omnigent claude``.

    Reuses the production spec materializer and stamps the same wrapper /
    terminal-first labels the CLI writes, so the runner's claude-native
    auto-bootstrap recognizes the session and auto-creates its terminal.

    :param base_url: Spawned server base URL.
    :returns: The new session/conversation id.
    """
    import io
    import tarfile
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
        # Non-config.yaml arcname routes through the omnigent compat
        # translator (the wrapper spec has no ``spec_version``).
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
        timeout=30.0,
    )
    create.raise_for_status()
    return str(create.json()["session_id"])


def _session_snapshot(base_url: str, session_id: str) -> dict:
    """Fetch the session snapshot (status + last_task_error), items skipped."""
    resp = _http.get(
        f"{base_url}/v1/sessions/{session_id}",
        params={"include_items": "false", "include_liveness": "false"},
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()


def test_probe_spawn_eagain_is_not_a_terminal_exit(tmp_path: Path) -> None:
    """A tmux probe-spawn EAGAIN must not be misread as terminal death.

    Journey (the reporter's): a running Claude-native terminal session's host
    exhausts its process/thread task quota, so tmux liveness probes can no
    longer be spawned (``EAGAIN``) — while the tmux server and Claude pane
    stay alive. The session must NOT be failed with
    ``required_terminal_exited``.

    Buggy behaviour: after 3 consecutive probe-spawn failures the watcher
    declares the required terminal dead and the runner publishes
    ``session.status: failed`` with ``code=required_terminal_exited``, even
    though Claude never exited — so this test FAILS with that code attached.

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

    # The sentinel that arms the fault. Absent at boot so the runner starts and
    # the terminal launches normally; the test `touch`es it mid-run to simulate
    # the host hitting fork exhaustion.
    sentinel = tmp_path / "eagain.armed"

    argv_file = tmp_path / "claude_argv.txt"
    heartbeat_file = tmp_path / "claude_heartbeat.txt"

    # Stub Claude CLI:
    #  * records its argv (so we can confirm the interactive terminal launched);
    #  * headless probes (``claude -p ...``, the model-catalog probe) return
    #    promptly so terminal bring-up isn't blocked;
    #  * the interactive terminal keeps redrawing the pane (so the session's
    #    PTY-derived status stays ``running`` — Claude is "working") and writes
    #    a heartbeat, so the terminal is provably alive. No Claude login needed.
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    stub = stub_bin / "claude"
    stub.write_text(
        "#!/bin/sh\n"
        f"{{ printf '%s\\037' \"$@\"; printf '\\n'; }} >> \"{argv_file}\"\n"
        'for a in "$@"; do\n'
        '  if [ "$a" = "-p" ]; then\n'
        "    printf '{}\\n'\n"
        "    exit 0\n"
        "  fi\n"
        "done\n"
        "i=0\n"
        "while :; do\n"
        "  i=$((i + 1))\n"
        "  printf 'claude working %s\\n' \"$i\"\n"
        f'  echo "$i" > "{heartbeat_file}"\n'
        "  sleep 0.2\n"
        "done\n"
    )
    stub.chmod(0o755)

    def _interactive_terminal_launched() -> bool:
        """True once a non-headless (no ``-p``) claude invocation was recorded."""
        if not argv_file.exists():
            return False
        for line in argv_file.read_text().splitlines():
            argv = line.split("\x1f")[:-1]
            if argv and "-p" not in argv:
                return True
        return False

    binding_token = secrets.token_urlsafe(32)
    from omnigent.runner.identity import token_bound_runner_id

    runner_id = token_bound_runner_id(binding_token)

    server_log = (tmp_path / "server.log").open("w")
    runner_log = (tmp_path / "runner.log").open("w")
    server_proc: subprocess.Popen[bytes] | None = None
    runner_proc: subprocess.Popen[bytes] | None = None
    try:
        server_proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _SERVER_BOOTSTRAP,
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
            [sys.executable, "-c", _RUNNER_BOOTSTRAP],
            env=_localhost_env(
                {
                    "OMNIGENT_RUNNER_ID": runner_id,
                    "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
                    "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
                    "RUNNER_SERVER_URL": base_url,
                    "OMNIGENT_RUNNER_WORKSPACE": str(workspace),
                    "OMNI_EAGAIN_SENTINEL": str(sentinel),
                    # Hermetic HOME: provider config resolves from
                    # ``$HOME/.omnigent``; keep it off the real HOME.
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

        # Create the claude-native session and bind it to the runner (the same
        # path the web UI / a daemon relaunch takes) -> the runner auto-creates
        # the required Claude terminal.
        session_id = _create_claude_native_session(base_url)
        _http.patch(
            f"{base_url}/v1/sessions/{session_id}",
            json={"runner_id": runner_id},
            timeout=_LAUNCH_TIMEOUT_S,
        ).raise_for_status()

        # The interactive Claude terminal launched (argv recorded).
        deadline = time.monotonic() + _LAUNCH_TIMEOUT_S
        while time.monotonic() < deadline:
            if _interactive_terminal_launched():
                break
            time.sleep(_POLL_S)
        assert _interactive_terminal_launched(), (
            "claude terminal never launched; runner log:\n"
            f"{(tmp_path / 'runner.log').read_text()[-3000:]}"
        )

        # The session reaches ``running`` (Claude is "working": the pane keeps
        # redrawing). This is load-bearing: a required-terminal exit while the
        # session is ``idle`` takes the clean-release path, while ``running``
        # is the incident's path (the pane still showed Claude working).
        deadline = time.monotonic() + _LAUNCH_TIMEOUT_S
        reached_running = False
        while time.monotonic() < deadline:
            if _session_snapshot(base_url, session_id).get("status") == "running":
                reached_running = True
                break
            time.sleep(_POLL_S)
        assert reached_running, (
            "claude-native session never reached 'running'; snapshot="
            f"{_session_snapshot(base_url, session_id).get('status')!r} runner log:\n"
            f"{(tmp_path / 'runner.log').read_text()[-3000:]}"
        )

        # Sanity: the inner Claude CLI is genuinely alive right now (heart-beating).
        assert heartbeat_file.exists(), "stub claude never wrote a heartbeat"
        beat_before = heartbeat_file.read_text()
        time.sleep(0.5)
        assert heartbeat_file.read_text() != beat_before, (
            "stub claude stopped heart-beating before the fault was armed — the "
            "terminal was not actually running, so the scenario is invalid"
        )

        # ARM THE FAULT: the host can no longer fork tmux liveness probes.
        # The tmux server + Claude pane keep running; only the *probes* fail.
        sentinel.write_text("armed")

        # Observe the outcome. Buggy build: within ~1 s the watcher declares the
        # required terminal dead and the session fails with
        # ``required_terminal_exited``. Fixed build: probe EAGAIN is treated as
        # liveness-unknown, so the session stays running and no such error
        # appears.
        deadline = time.monotonic() + _FAILURE_OBSERVE_TIMEOUT_S
        observed_error: dict | None = None
        observed_status: str | None = None
        while time.monotonic() < deadline:
            snap = _session_snapshot(base_url, session_id)
            observed_status = snap.get("status")
            err = snap.get("last_task_error")
            if observed_status == "failed" and isinstance(err, dict) and err:
                observed_error = err
                break
            time.sleep(0.25)

        runner_tail = (tmp_path / "runner.log").read_text()[-4000:]

        # DESIRED behaviour (regression guard): a probe-spawn EAGAIN while the
        # pane is genuinely alive must be treated as liveness-unknown, NOT as a
        # terminal exit. The Claude CLI never exited (we only failed liveness
        # probe *spawns*; we never signalled the stub), so classifying this as
        # ``required_terminal_exited`` is a false terminal-death.
        assert not (
            observed_error is not None and observed_error.get("code") == "required_terminal_exited"
        ), (
            "false terminal-death regression: a running Claude-native terminal "
            "was declared 'required_terminal_exited' merely because tmux liveness "
            "probes could not be spawned (EAGAIN) — the terminal never exited. "
            f"Observed status={observed_status!r} last_task_error={observed_error!r}. "
            f"runner log tail:\n{runner_tail}"
        )
    finally:
        _terminate(runner_proc)
        _terminate(server_proc)
        server_log.close()
        runner_log.close()
