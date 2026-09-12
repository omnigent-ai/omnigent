"""E2E regression: big agent instructions must not kill the Claude launch.

The reported failure: a claude-native session's terminal auto-create dies with
``RuntimeError: tmux launch failed (rc=1): command too long`` inside
``_auto_create_claude_terminal`` -> ``launch_required_terminal`` ->
``TerminalInstance.launch``. The user-observable journey behind it: an agent on
the ``claude-native`` harness carries large author instructions (the spec's
``prompt:``, resolved to ``AgentSpec.instructions``); starting a session binds
it to a runner, the runner threads the instructions verbatim onto the Claude
CLI argv (``--append-system-prompt`` in ``augment_claude_args``), and
``TerminalInstance.launch`` packs the whole shell-quoted argv into ONE ``tmux
new-session`` client command. tmux's client->server protocol rejects any single
command over its ~16KB imsg cap, so the launch exits rc=1 "command too long",
the terminal never starts, and the session fails. ``TerminalInstance.send``
already chunks literal text for exactly this cap; ``launch`` has no such
handling.

This test drives the real journey end to end, exactly like
``test_claude_native_cold_resume_items_500_e2e.py``: a real ``omnigent server``
subprocess, a real runner subprocess, a real tmux, and a claude-native session
whose agent spec carries ~20KB instructions. The Claude CLI is a stub that
records its argv and parks, so no Claude login is needed - the failure under
test fires in tmux BEFORE the CLI would run.

Desired behavior (asserted): the Claude terminal launches despite the large
instructions, and the instructions still reach the CLI (in the recorded argv,
or in a file the argv references) rather than being silently dropped. On the
buggy build the terminal never launches - the stub records nothing and the
runner log shows "tmux launch failed (rc=1): command too long" - so this test
FAILS with that log tail in the failure message.

Run::

    .venv/bin/python -m pytest \
        tests/e2e/test_claude_native_big_instructions_launch_e2e.py -v
"""

from __future__ import annotations

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
import time
from pathlib import Path

import httpx
import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Every HTTP call in this test targets 127.0.0.1; CI shells can carry an
# egress proxy in the environment, so bypass proxy autodetection entirely.
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

_HEALTH_TIMEOUT_S = 120.0
_POLL_S = 1.0
# Terminal auto-create includes bridge prep + model-catalog probes against the
# parked stub (each holding a 20-30s budget) + tmux boot; generous for CI.
_ARGV_TIMEOUT_S = 180.0

# A marker planted inside the big instructions so delivery (not just launch)
# is assertable on the fixed build.
_PROMPT_MARKER = "OMNIGENT-BIG-PROMPT-DELIVERY-MARKER"

# tmux's client->server imsg cap is ~16KB for one command; 20K characters of
# instructions guarantee the composed ``new-session`` command exceeds it no
# matter how small the rest of the argv is.
_INSTRUCTIONS_SIZE = 20_000

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="claude-native terminals run inside tmux; tmux not installed",
)


def _big_instructions() -> str:
    """Author-style instructions text of ``_INSTRUCTIONS_SIZE`` characters.

    :returns: A playbook-shaped prompt with the delivery marker embedded.
    """
    paragraph = (
        "Follow the team playbook: review the diff, run the linters, check "
        "the migration plan, and summarize risks before approving. "
    )
    text = f"{_PROMPT_MARKER}\n" + paragraph * (_INSTRUCTIONS_SIZE // len(paragraph) + 1)
    return text[:_INSTRUCTIONS_SIZE]


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


def _create_big_prompt_claude_session(base_url: str) -> str:
    """Create a claude-native session whose agent carries ~20KB instructions.

    Reuses the production wrapper spec (``_materialize_claude_agent_spec``) so
    everything except the instructions size matches what ``omnigent claude`` /
    the web UI ships, then swaps ``prompt:`` for the oversized author text.
    The stock small prompt already rides the exact same channel
    (``--append-system-prompt``), so size is the only variable under test.

    :param base_url: Spawned server base URL.
    :returns: The new session/conversation id.
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
        raw = yaml.safe_load(_materialize_claude_agent_spec(Path(tmp)).read_text())
    raw["prompt"] = _big_instructions()
    yaml_text = yaml.safe_dump(raw, sort_keys=False)

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


def _runner_log_tail(runner_home: Path, fallback: Path) -> str:
    """The spawned runner's own log tail, for failure messages.

    The runner configures process logging under ``$HOME/.omnigent/logs/runner``
    (its hermetic HOME here), so its stdout capture stays empty; read the real
    log so an assertion failure carries the actual launch error (e.g. ``tmux
    launch failed (rc=1): command too long``).

    :param runner_home: The runner subprocess's hermetic ``$HOME``.
    :param fallback: The stdout-capture log to fall back to.
    :returns: Tail of the newest runner log file, or of *fallback*.
    """
    log_dir = runner_home / ".omnigent" / "logs" / "runner"
    logs = sorted(log_dir.glob("*.log")) if log_dir.exists() else []
    if logs:
        return logs[-1].read_text(errors="ignore")[-3000:]
    return fallback.read_text(errors="ignore")[-3000:] if fallback.exists() else ""


def _marker_delivered(argv: list[str]) -> bool:
    """Whether the instructions marker reached the CLI, directly or via a file.

    A fix may keep the text on the argv (e.g. exec-ing the launch through a
    script file so tmux never sees the long command) or move it into a file
    the argv references (e.g. the ``--settings`` sidecar). Accept both shapes.

    :param argv: The recorded stub-claude argv.
    :returns: ``True`` when the marker is found on the argv or inside any
        existing file an argv element points at.
    """
    if any(_PROMPT_MARKER in arg for arg in argv):
        return True
    for arg in argv:
        candidate = Path(arg)
        try:
            if candidate.is_file() and _PROMPT_MARKER in candidate.read_text(errors="ignore"):
                return True
        except OSError:
            continue
    return False


def test_big_instructions_claude_terminal_launches(tmp_path: Path) -> None:
    """
    A claude-native agent with ~20KB instructions must still get its terminal.

    Journey: an author packages a claude-native agent with a large system
    prompt; the user starts a session on it (bind to runner); the runner
    auto-creates the Claude Code terminal. Expected: the terminal launches and
    the instructions reach the CLI. Buggy behavior: the single ``tmux
    new-session`` command carrying the whole argv exceeds tmux's ~16KB
    per-command cap, tmux exits rc=1 "command too long", the terminal never
    launches, and the session fails.

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

    # Stub Claude CLI: records its argv (the launch decision under test) and
    # parks so the tmux pane stays alive. No Claude login needed. The runner
    # also runs headless ``claude -p "/model"`` catalog probes against the
    # stub, so every invocation APPENDS one unit-separator-joined record and
    # the assertion filters for the interactive terminal launch.
    argv_file = tmp_path / "claude_argv.txt"
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    stub = stub_bin / "claude"
    stub.write_text(
        "#!/bin/sh\n"
        f"{{ printf '%s\\037' \"$@\"; printf '\\n'; }} >> \"{argv_file}\"\n"
        "exec sleep 600\n"
    )
    stub.chmod(0o755)

    def _terminal_launch_argv() -> list[str] | None:
        """The interactive terminal launch's argv, once recorded.

        :returns: The argv of the first recorded non-headless invocation
            (the catalog probes all run ``-p "/model"``), or ``None``.
        """
        if not argv_file.exists():
            return None
        # Each stub invocation appends every arg with a \x1f terminator,
        # then a record-ending newline. Args themselves can contain
        # newlines (the big instructions do), so records split on the
        # terminator+newline pair, never on bare lines.
        for record in argv_file.read_text().split("\x1f\n"):
            argv = record.split("\x1f") if record else []
            if argv and "-p" not in argv:
                return argv
        return None

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
                "from omnigent.cli import main\nmain()\n",
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
                    # Hermetic HOME: bridge state and provider config resolve
                    # under ``$HOME`` - keep both off the real HOME.
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

        # The user's session on the big-instructions agent.
        session_id = _create_big_prompt_claude_session(base_url)

        # THE LAUNCH: bind the session to the runner (what starting the
        # session from the web UI does) -> the runner auto-creates the Claude
        # terminal, composing the CLI argv with --append-system-prompt.
        # The bind can block on the runner-side terminal bring-up (the model
        # catalog probe alone holds a 20-30s budget against the parked stub),
        # so give it the same generous budget as the argv wait.
        _http.patch(
            f"{base_url}/v1/sessions/{session_id}",
            json={"runner_id": runner_id},
            timeout=_ARGV_TIMEOUT_S,
        ).raise_for_status()

        deadline = time.monotonic() + _ARGV_TIMEOUT_S
        argv: list[str] | None = None
        while time.monotonic() < deadline:
            argv = _terminal_launch_argv()
            if argv is not None:
                break
            time.sleep(_POLL_S)

        # The bug: the terminal never launches at all - tmux rejects the
        # oversized new-session command before the CLI runs.
        assert argv is not None, (
            "claude terminal never launched for an agent with "
            f"{_INSTRUCTIONS_SIZE} chars of instructions - the tmux launch "
            "command exceeded tmux's per-command cap; runner log tail:\n"
            f"{_runner_log_tail(runner_home, tmp_path / 'runner.log')}"
        )

        # Guard the fix's other half: launching by silently dropping the
        # author's instructions would be a different data-loss bug.
        assert _marker_delivered(argv), (
            "claude terminal launched but the agent's instructions were "
            f"dropped - marker {_PROMPT_MARKER!r} not on the argv nor in any "
            f"file it references. launched argv: {argv}"
        )
    finally:
        _terminate(runner_proc)
        _terminate(server_proc)
        server_log.close()
        runner_log.close()
