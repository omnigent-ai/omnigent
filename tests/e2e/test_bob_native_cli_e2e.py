"""End-to-end test: ``omnigent bob`` drives the native IBM Bob Shell TUI.

``bob-native`` is terminal-first: ``omnigent bob`` launches ``bob chat`` in a
runner-owned tmux pane, and each web-UI turn is pasted into that pane by
:class:`omnigent.inner.bob_native_executor.BobNativeExecutor`. Bob's reply is
not mirrored into the chat transcript, so this test reads it back from the pane.

Opt-in only (real Bob account, real cost): set ``OMNIGENT_E2E_BOB_NATIVE=1``,
have Bob Shell 2.x and ``tmux`` on ``PATH``, and sign in to Bob once with IBMid
(``bob chat``) so the runner-launched Bob reuses the stored login. Run it with
isolated Omnigent state so the spawned host daemon is disposable. The launch
directory is trusted explicitly with ``--trust`` so Bob's folder-trust dialog
does not block the headless pane.

    OMNIGENT_CONFIG_HOME=$(mktemp -d) OMNIGENT_DATA_DIR=$(mktemp -d) \
    OMNIGENT_E2E_BOB_NATIVE=1 \
    .venv/bin/python -m pytest tests/e2e/test_bob_native_cli_e2e.py --llm-api-key x -v
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest

from omnigent.entities.session_resources import terminal_resource_id
from tests.e2e._native_resume_helpers import (
    PtyHandle,
    cli_env,
    inject_user_message,
    omnigent_console_script,
    spawn_cli_background,
    wait_for_terminal_ready,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_BOB_NATIVE") != "1"
    or shutil.which("bob") is None
    or shutil.which("tmux") is None,
    reason=(
        "bob-native CLI e2e needs Bob Shell 2.x signed in with IBMid and tmux; "
        "set OMNIGENT_E2E_BOB_NATIVE=1 to run"
    ),
)

_CONV_ID_TIMEOUT = 120.0
_TERMINAL_READY_TIMEOUT = 90.0
_REPLY_TIMEOUT = 180.0
# The CLI prints ``Web UI: <server>/c/<session id>``; ids may lack a ``conv_`` prefix.
_WEB_UI_LINK_RE = re.compile(r"/c/([0-9A-Za-z_]+)")


def _wait_for_session_id(handle: PtyHandle, *, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    output = ""
    while time.monotonic() < deadline:
        output = handle.output()
        match = _WEB_UI_LINK_RE.search(output)
        if match:
            return match.group(1)
        time.sleep(0.5)
    raise AssertionError(f"no session link printed within {timeout}s:\n{output[-2000:]}")


def _bob_pane(client: httpx.Client, conversation_id: str) -> tuple[str, str]:
    """Return the ``(tmux_socket, tmux_target)`` of the session's Bob pane."""
    terminal_id = terminal_resource_id("bob", "main")
    resp = client.get(f"/v1/sessions/{conversation_id}/resources/terminals/{terminal_id}")
    resp.raise_for_status()
    metadata = resp.json()["metadata"]
    return metadata["tmux_socket"], metadata["tmux_target"]


def _capture(socket_path: str, target: str) -> str:
    proc = subprocess.run(
        ["tmux", "-S", socket_path, "capture-pane", "-p", "-S", "-200", "-t", target],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    return proc.stdout


def test_bob_native_cli_smoke(resume_test_server: str, tmp_path: Path) -> None:
    """A web-UI turn reaches the real Bob TUI and Bob's reply lands in its pane."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = f"BOB_{uuid.uuid4().hex[:8].upper()}"

    omni = str(omnigent_console_script())
    handle = spawn_cli_background(
        [omni, "bob", "--server", resume_test_server, "--", "--trust"],
        env=cli_env(),
        cwd=str(workspace),
    )
    socket_path = target = None
    try:
        conversation_id = _wait_for_session_id(handle, timeout=_CONV_ID_TIMEOUT)
        with httpx.Client(base_url=resume_test_server, timeout=30) as client:
            wait_for_terminal_ready(
                client,
                conversation_id=conversation_id,
                harness="bob",
                timeout=_TERMINAL_READY_TIMEOUT,
            )
            socket_path, target = _bob_pane(client, conversation_id)
            inject_user_message(
                client,
                conversation_id=conversation_id,
                text=f"Reply with ONLY this exact word and use no tools: {marker}",
            )
            deadline = time.monotonic() + _REPLY_TIMEOUT
            pane = ""
            while time.monotonic() < deadline:
                pane = _capture(socket_path, target)
                # The prompt echo contains the marker once; Bob's reply adds it again.
                if pane.count(marker) >= 2:
                    break
                time.sleep(1.0)
            assert pane.count(marker) >= 2, (
                f"Bob never replied with {marker!r}.\n\nPane tail:\n{pane[-2000:]}\n\n"
                f"CLI output tail:\n{handle.output()[-2000:]}"
            )
    finally:
        handle.terminate()
        if socket_path and target:
            subprocess.run(
                ["tmux", "-S", socket_path, "kill-session", "-t", target],
                capture_output=True,
                check=False,
                timeout=10,
            )
