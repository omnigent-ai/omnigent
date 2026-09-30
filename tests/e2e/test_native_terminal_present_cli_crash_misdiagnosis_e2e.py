"""Regression e2e: a present-but-crashed claude CLI is misdiagnosed as missing.

Reported failure: a ``claude-native`` session's required terminal exited with
status ``127`` and the runner surfaced the failure card

    Agent command not found
    The host couldn't find the agent's CLI on its PATH, so the terminal exited
    before the session could start.
    Try this: Install the harness on the host (e.g. run `omnigent setup`).

But the captured pane output proves the opposite: the ``claude`` CLI *was*
present and running -- it had self-updated ("Update installed - Restart to
update"), printed a resume hint, and only then died because a stray ``--model``
line hit the shell as ``command not found`` after a self-restart (exit 127). The
runner's terminal-exit classifier keys purely on ``exit_status == 127`` (see
``omnigent/runner/launch_failure.py`` ``missing_binary`` matcher), so it
misattributes a mid-session crash of a *present* CLI to a missing install and
tells the user to run ``omnigent setup``.

This test drives the real user path against a live server + runner:

1. Point the runner's ``claude`` command (``OMNIGENT_CLAUDE_PATH``) at a stub
   that emits the incident pane output, stays alive long enough to register as
   an *available* required terminal (so the exit takes the classified
   ``required_terminal_exited`` path, not ``native_terminal_start_failed``), and
   then exits ``127`` -- exactly like the real Claude Code did in the incident.
2. Create a ``claude-native`` session (the same terminal-first spec ``omnigent
   claude`` ships) and bind it to the runner.
3. POST the native-terminal ensure endpoint (what the SPA does when a user opens
   the session), which auto-launches the ``claude`` terminal.
4. Let the terminal exit 127 and read the persisted ``last_task_error`` the SPA
   renders as the failure card.

The assertion pins the *desired* behaviour: a claude terminal whose own pane
output proves the CLI was present and running must NOT be diagnosed as a missing
binary / "install the harness". It therefore FAILS on the current build (the
runner returns the misleading "Agent command not found" card) and will PASS once
the classifier stops treating every ``127`` as a missing CLI. It is not a
characterization test of the buggy behaviour.
"""

from __future__ import annotations

import io
import json
import os
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The reproduction needs to launch a managed tmux terminal for the claude-native
# session; without tmux the required terminal can never come up.
pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="repro needs tmux to launch the claude-native required terminal",
)

# Incident-like pane output. It proves the claude CLI was PRESENT and running:
# it self-updated, printed a resume hint, and only then died from a stray
# `--model` line hitting the shell as `command not found` (exit 127). This is a
# mid-session crash of a present CLI, NOT a missing-binary install gap.
_INCIDENT_PANE_LINES = (
    "● Unknown command: /restart",
    "  Press Ctrl-C again to exit    ✔ Update installed · Restart to update",
    "Resume this session with:",
    "claude --resume be28caff-2e85-47de-8f17-9346d116106b",
    "zsh:2: command not found: --model",
)


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _write_claude_stub(path: Path) -> None:
    """Write a fake ``claude`` that mimics the incident.

    The stub prints the incident pane header, then churns changing output on one
    line (faster than the 1s claude-native idle threshold, so the PTY watcher
    reads the session as ``running`` and the required terminal registers as
    *available*), then exits 127 -- reaching the classified
    ``required_terminal_exited`` path rather than ``native_terminal_start_failed``.
    """
    header = "\n".join(f"echo {shlex.quote(line)}" for line in _INCIDENT_PANE_LINES)
    path.write_text(
        "#!/bin/bash\n"
        f"{header}\n"
        # Keep the pane visibly active (< 1s idle threshold) so the terminal is
        # observed as an available required terminal, then die 127.
        "for i in $(seq 1 25); do printf '\\rworking %s ' \"$i\"; sleep 0.2; done\n"
        "exit 127\n"
    )
    path.chmod(0o755)


def _claude_native_bundle() -> bytes:
    """Build the same terminal-first ``claude-native`` bundle ``omnigent claude`` ships."""
    from omnigent.harnesses.claude_native.main import _materialize_claude_agent_spec

    with tempfile.TemporaryDirectory() as tmp:
        spec_path = _materialize_claude_agent_spec(Path(tmp))
        yaml_text = spec_path.read_text()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        # Non-config.yaml arcname routes through the omnigent compat translator
        # (the spec carries no spec_version), preserving executor.harness +
        # terminals:, exactly like the e2e_ui native_claude fixture.
        info = tarfile.TarInfo("claude-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _LiveClaudeNativeStack:
    """A live ``omnigent server`` + runner whose ``claude`` command is the stub."""

    def __init__(self, work: Path) -> None:
        self._work = work
        self._server: subprocess.Popen[bytes] | None = None
        self._runner: subprocess.Popen[bytes] | None = None
        self.base_url = ""
        self.runner_id = ""

    def start(self) -> None:
        from omnigent.runner.identity import token_bound_runner_id

        stub = self._work / "claude"
        _write_claude_stub(stub)

        db_path = self._work / "omni.db"
        artifact_dir = self._work / "artifacts"
        artifact_dir.mkdir()

        port = _free_port()
        self.base_url = f"http://127.0.0.1:{port}"
        binding_token = secrets.token_urlsafe(32)
        self.runner_id = token_bound_runner_id(binding_token)

        pythonpath = f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"
        server_env = {
            **os.environ,
            "PYTHONPATH": pythonpath,
            "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token,
            "OPENAI_API_KEY": "mock-key",
            "ANTHROPIC_API_KEY": "",
        }
        server_log = open(self._work / "server.log", "w")  # noqa: SIM115
        self._server_log = server_log
        self._server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent",
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--database-uri",
                f"sqlite:///{db_path}",
                "--artifact-location",
                str(artifact_dir),
            ],
            env=server_env,
            stdout=server_log,
            stderr=subprocess.STDOUT,
        )

        runner_env = {
            **os.environ,
            "PYTHONPATH": pythonpath,
            "OMNIGENT_RUNNER_ID": self.runner_id,
            "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
            "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
            "RUNNER_SERVER_URL": self.base_url,
            # The lever: the claude CLI the native terminal launches is the stub.
            "OMNIGENT_CLAUDE_PATH": str(stub),
            "OPENAI_API_KEY": "mock-key",
            "ANTHROPIC_API_KEY": "",
        }
        runner_log = open(self._work / "runner.log", "w")  # noqa: SIM115
        self._runner_log = runner_log
        self._runner = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=runner_env,
            stdout=runner_log,
            stderr=subprocess.STDOUT,
        )

        self._await_online()

    def _online(self) -> bool:
        try:
            health = httpx.get(f"{self.base_url}/health", timeout=2)
            if health.status_code != 200:
                return False
            status = httpx.get(f"{self.base_url}/v1/runners/{self.runner_id}/status", timeout=2)
            return status.status_code == 200 and status.json().get("online") is True
        except httpx.HTTPError:
            return False

    def _await_online(self, timeout: float = 90.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            assert self._server is not None
            if self._server.poll() is not None:
                raise AssertionError(
                    "server exited early:\n" + (self._work / "server.log").read_text()[-3000:]
                )
            if self._online():
                return
            time.sleep(1)
        raise AssertionError(
            "server+runner did not come online in time.\n"
            f"server.log:\n{(self._work / 'server.log').read_text()[-2000:]}\n"
            f"runner.log:\n{(self._work / 'runner.log').read_text()[-2000:]}"
        )

    def create_claude_native_session(self) -> str:
        from omnigent._wrapper_labels import (
            CLAUDE_NATIVE_WRAPPER_VALUE,
            UI_MODE_LABEL_KEY,
            UI_MODE_TERMINAL_VALUE,
            WRAPPER_LABEL_KEY,
        )

        metadata = {
            "labels": {
                UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
                WRAPPER_LABEL_KEY: CLAUDE_NATIVE_WRAPPER_VALUE,
            }
        }
        create = httpx.post(
            f"{self.base_url}/v1/sessions",
            data={"metadata": json.dumps(metadata)},
            files={
                "bundle": (
                    "claude-native-ui.tar.gz",
                    _claude_native_bundle(),
                    "application/gzip",
                )
            },
            timeout=30.0,
        )
        create.raise_for_status()
        session_id = str(create.json()["session_id"])
        patch = httpx.patch(
            f"{self.base_url}/v1/sessions/{session_id}",
            json={"runner_id": self.runner_id},
            timeout=10.0,
        )
        patch.raise_for_status()
        return session_id

    def ensure_native_terminal(self, session_id: str) -> httpx.Response:
        """Auto-launch the claude terminal (what the SPA does on session open)."""
        return httpx.post(
            f"{self.base_url}/v1/sessions/{session_id}/resources/terminals",
            json={
                "terminal": "claude",
                "session_key": "main",
                "ensure_native_terminal": True,
            },
            timeout=90.0,
        )

    def await_terminal_exit_error(
        self, session_id: str, timeout: float = 150.0
    ) -> dict[str, object]:
        """Poll until the runner surfaces a terminal-exit failure for the session."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            info = httpx.get(f"{self.base_url}/v1/sessions/{session_id}", timeout=10)
            if info.status_code == 200:
                error = info.json().get("last_task_error")
                if isinstance(error, dict) and error.get("code"):
                    return error
            items = httpx.get(
                f"{self.base_url}/v1/sessions/{session_id}/items",
                params={"limit": 50, "order": "desc"},
                timeout=10,
            )
            if items.status_code == 200:
                for item in items.json().get("data", []):
                    error = item.get("error") if isinstance(item, dict) else None
                    if isinstance(error, dict) and error.get("code") == "required_terminal_exited":
                        return error
            time.sleep(2)
        raise AssertionError(
            "no terminal-exit failure surfaced in time.\n"
            f"runner.log:\n{(self._work / 'runner.log').read_text()[-3000:]}"
        )

    def stop(self) -> None:
        for proc in (self._runner, self._server):
            if proc is not None and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        for handle in (getattr(self, "_server_log", None), getattr(self, "_runner_log", None)):
            if handle is not None:
                handle.close()


@pytest.fixture
def live_claude_native_stack(tmp_path: Path) -> Iterator[_LiveClaudeNativeStack]:
    stack = _LiveClaudeNativeStack(tmp_path)
    try:
        stack.start()
        yield stack
    finally:
        stack.stop()


def test_present_claude_cli_crash_is_not_misdiagnosed_as_missing_binary(
    live_claude_native_stack: _LiveClaudeNativeStack,
) -> None:
    """A claude terminal that was present + crashed 127 must not say "install the harness".

    The runner used to classify every required-terminal exit with status 127 as
    "Agent command not found" and tell the user to run ``omnigent setup``, even
    when the terminal's own captured output proved the CLI was present and
    running. Assert the desired behaviour: a present CLI's crash is never
    diagnosed as a missing install.
    """
    stack = live_claude_native_stack
    session_id = stack.create_claude_native_session()

    ensure = stack.ensure_native_terminal(session_id)
    # The terminal must actually launch and register (200) so its later 127 exit
    # takes the classified ``required_terminal_exited`` path. A launch-time death
    # would instead surface ``native_terminal_start_failed`` (a different bug).
    assert ensure.status_code == 200, (
        f"ensure-terminal did not launch the claude terminal: "
        f"{ensure.status_code} {ensure.text[:500]}"
    )

    error = stack.await_terminal_exit_error(session_id)

    # We hit the required-terminal-exit classification path (not a launch
    # failure), which is where the misdiagnosis is produced.
    assert error.get("code") == "required_terminal_exited", (
        f"expected a classified required-terminal exit, got {error!r}"
    )

    title = str(error.get("title") or "")
    cause = str(error.get("cause") or "")
    remediation = str(error.get("remediation") or "")
    message = str(error.get("message") or "")
    haystack = " ".join((title, cause, remediation, message)).lower()

    # The claude CLI was present and running (the pane output the stub emits
    # proves it self-updated and restarted); its 127 exit came from a stray
    # `--model` line, not a missing binary. The runner must NOT tell the user to
    # install the harness.
    assert title != "Agent command not found", (
        "a claude terminal that was present and running (it self-updated then "
        "crashed 127 from a stray flag) is misdiagnosed as "
        f"'Agent command not found'. Full error: {error!r}"
    )
    assert "install the harness" not in haystack, (
        "the failure remediation tells the user to install a harness that was "
        f"already present and running. Full error: {error!r}"
    )
    assert "omnigent setup" not in haystack, (
        "the failure suggests `omnigent setup` for a claude CLI that was "
        f"already installed and running. Full error: {error!r}"
    )
